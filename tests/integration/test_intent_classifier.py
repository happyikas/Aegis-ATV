"""Tests for ``aegis.burnin.intent_classifier`` — task intent
classification (PR-η, Phase B Tier 2)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from aegis.atv.temporal import (
    ATVSnapshot,
    TemporalContext,
    serialize_temporal,
)
from aegis.burnin.intent_classifier import (
    CLASSIFIER_HASH,
    INTENTS,
    N_INTENTS,
    IntentPrediction,
    classifier_from_dict,
    classifier_to_dict,
    default_classifier,
    load_classifier,
    load_classifier_or_default,
    predict_intent,
    render_intent,
    save_classifier,
    train_from_labeled,
)
from aegis.burnin.trajectory_catalog import (
    EMBEDDING_DIM,
    embed_trajectory,
)

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _mk_traj(
    tools: list[str],
    *,
    n_backtracks: int = 0,
    n_errors: int = 0,
    n_redundant: int = 0,
    n_failures: int = 0,
) -> TemporalContext:
    snaps: list[ATVSnapshot] = []
    for i, t in enumerate(tools):
        rel = i - (len(tools) - 1)
        snaps.append(ATVSnapshot(
            turn_index_rel=rel, ts_ns=0,
            tool_name=t, args_excerpt="",
            decision="ALLOW",
            outcome="failure" if i < n_failures else "success",
            backtrack=(i < n_backtracks),
            redundant=(i < n_redundant),
            is_error=(i < n_errors),
        ))
    return TemporalContext(
        history=tuple(snaps),
        window_size=len(tools),
        cumulative_token_trajectory=tuple(
            1000 * (i + 1) for i in range(len(tools))
        ),
        cache_hit_rate_trajectory=tuple(0.5 for _ in tools),
        n_backtracks=n_backtracks, n_redundant=n_redundant,
        n_errors=n_errors, n_failures=n_failures,
        cache_hit_rate_max_drop_pp=0.0,
        token_velocity_per_turn=800.0,
        is_progress_stalled=False,
        distinct_tools_in_window=tuple(sorted(set(tools))),
    )


# ──────────────────────────────────────────────────────────────────────
# Schema invariants
# ──────────────────────────────────────────────────────────────────────


class TestSchema:
    def test_intent_count(self) -> None:
        assert N_INTENTS == 8
        assert len(INTENTS) == N_INTENTS

    def test_classifier_dimensions(self) -> None:
        cls = default_classifier()
        assert cls.is_usable()
        assert cls.embedding_dim == EMBEDDING_DIM
        assert len(cls.weights) == N_INTENTS
        assert all(len(row) == EMBEDDING_DIM for row in cls.weights)
        assert len(cls.bias) == N_INTENTS

    def test_default_hash_set(self) -> None:
        cls = default_classifier()
        assert cls.classifier_hash == CLASSIFIER_HASH
        assert len(cls.classifier_hash) == 64

    def test_intents_in_alphabetical_classification_order(self) -> None:
        # The order is fixed; all callers depend on it.
        # If this order changes, bump CLASSIFIER_HASH.
        assert INTENTS[0] == "debug"
        assert INTENTS[-1] == "general"


# ──────────────────────────────────────────────────────────────────────
# Predict
# ──────────────────────────────────────────────────────────────────────


class TestPredict:
    def test_softmax_sums_to_one(self) -> None:
        cls = default_classifier()
        emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        emb[10] = 1.0  # "Edit" slot
        pred = cls.predict(emb)
        total = sum(pred.full.values())
        assert total == pytest.approx(1.0, abs=1e-5)

    def test_primary_is_top_probability(self) -> None:
        cls = default_classifier()
        emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        emb[10] = 1.0  # "Edit" slot
        pred = cls.predict(emb)
        primary_prob = pred.full[pred.primary]
        # No other intent has higher prob.
        for name, prob in pred.full.items():
            if name != pred.primary:
                assert prob <= primary_prob

    def test_dimension_mismatch_raises(self) -> None:
        cls = default_classifier()
        with pytest.raises(ValueError, match="shape mismatch"):
            cls.predict(np.zeros(10, dtype=np.float32))


# ──────────────────────────────────────────────────────────────────────
# Default classifier behaviour
# ──────────────────────────────────────────────────────────────────────


class TestDefaultClassifier:
    """The hand-tuned default should produce sensible classifications
    for the canonical scenarios. Any failure here is a real-world
    regression, not a flake — fix the rules, don't loosen the tests."""

    def test_debug_pattern(self) -> None:
        ctx = _mk_traj(
            ["Read", "Edit", "Edit", "Bash", "Bash"],
            n_backtracks=1, n_errors=2,
        )
        pred = predict_intent(ctx)
        assert pred.primary == "debug"

    def test_explore_pattern(self) -> None:
        ctx = _mk_traj(["Grep", "Read", "Glob", "Read", "Grep"])
        pred = predict_intent(ctx)
        assert pred.primary == "explore"

    def test_edit_pattern(self) -> None:
        ctx = _mk_traj(["Read", "Edit", "Edit", "MultiEdit", "Read"])
        pred = predict_intent(ctx)
        assert pred.primary == "edit"

    def test_test_pattern(self) -> None:
        ctx = _mk_traj(["Bash", "Bash", "Read", "Bash", "Edit"])
        pred = predict_intent(ctx)
        assert pred.primary == "test"

    def test_review_pattern(self) -> None:
        ctx = _mk_traj(["Read", "Read", "Grep", "Read", "Read"])
        pred = predict_intent(ctx)
        assert pred.primary == "review"

    def test_create_pattern(self) -> None:
        ctx = _mk_traj(["Read", "Write", "Write", "Read", "Write"])
        pred = predict_intent(ctx)
        assert pred.primary == "create"


# ──────────────────────────────────────────────────────────────────────
# train_from_labeled
# ──────────────────────────────────────────────────────────────────────


class TestTraining:
    def _make_synthetic_dataset(self) -> tuple[np.ndarray, list[str]]:
        """Generate labeled trajectory embeddings for each intent."""
        embeddings: list[np.ndarray] = []
        labels: list[str] = []
        # 6 examples per intent
        for intent in INTENTS:
            # Build a TemporalContext exemplar for each
            if intent == "debug":
                ctx = _mk_traj(
                    ["Read", "Edit", "Bash", "Bash", "Bash"],
                    n_backtracks=1, n_errors=2,
                )
            elif intent == "explore":
                ctx = _mk_traj(["Grep", "Glob", "Read", "Grep", "Read"])
            elif intent == "edit":
                ctx = _mk_traj(
                    ["Read", "Edit", "Edit", "MultiEdit", "Read"]
                )
            elif intent == "test":
                ctx = _mk_traj(["Bash", "BashOutput", "Read", "Bash", "Read"])
            elif intent == "refactor":
                ctx = _mk_traj(
                    ["Read", "Grep", "MultiEdit", "Edit", "Read"]
                )
            elif intent == "review":
                ctx = _mk_traj(["Read", "Read", "Grep", "Read", "Read"])
            elif intent == "create":
                ctx = _mk_traj(["Read", "Write", "Write", "Read", "Write"])
            else:  # general
                ctx = _mk_traj(["Read", "Edit", "Bash", "Read", "Read"])
            for _ in range(6):
                embeddings.append(embed_trajectory(ctx))
                labels.append(intent)
        return np.stack(embeddings, axis=0), labels

    def test_trains_to_classify_synthetic_dataset(self) -> None:
        samples, y = self._make_synthetic_dataset()
        cls = train_from_labeled(samples, y, n_epochs=300)
        # Train accuracy should be high on the same synthetic data.
        n_correct = 0
        for emb, label in zip(samples, y, strict=True):
            pred = cls.predict(emb)
            if pred.primary == label:
                n_correct += 1
        # Allow some imperfection — floor 0.6 (much better than chance 0.125).
        assert n_correct / len(y) >= 0.6

    def test_classifier_kind_marks_trained(self) -> None:
        samples, y = self._make_synthetic_dataset()
        cls = train_from_labeled(samples, y, n_epochs=50)
        assert cls.classifier_kind == "trained-lr"

    def test_dimension_mismatch_in_input_raises(self) -> None:
        bad = np.zeros((5, 10), dtype=np.float32)
        with pytest.raises(ValueError, match="shape"):
            train_from_labeled(bad, ["debug"] * 5)

    def test_label_length_mismatch_raises(self) -> None:
        emb = np.zeros((3, EMBEDDING_DIM), dtype=np.float32)
        with pytest.raises(ValueError, match="disagree"):
            train_from_labeled(emb, ["debug"] * 5)

    def test_unknown_label_raises(self) -> None:
        emb = np.zeros((2, EMBEDDING_DIM), dtype=np.float32)
        with pytest.raises(ValueError, match="not in INTENTS"):
            train_from_labeled(emb, ["unknown_intent"] * 2)

    def test_deterministic_given_seed(self) -> None:
        emb = np.random.default_rng(0).normal(
            size=(20, EMBEDDING_DIM),
        ).astype(np.float32)
        labels = ["debug", "edit"] * 10
        a = train_from_labeled(emb, labels, seed=42, n_epochs=20)
        b = train_from_labeled(emb, labels, seed=42, n_epochs=20)
        # Same seed → identical weights.
        assert a.weights == b.weights


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


class TestJSON:
    def test_round_trip(self, tmp_path: Path) -> None:
        cls = default_classifier()
        path = tmp_path / "cls.json"
        save_classifier(cls, path)
        loaded = load_classifier(path)
        assert loaded.weights == cls.weights
        assert loaded.bias == cls.bias

    def test_to_dict_serialisable(self) -> None:
        d = classifier_to_dict(default_classifier())
        json.dumps(d)

    def test_from_dict_tolerates_minimal(self) -> None:
        d = {"weights": [], "bias": []}
        cls = classifier_from_dict(d)
        # Empty weights → not usable.
        assert not cls.is_usable()


# ──────────────────────────────────────────────────────────────────────
# load_or_default
# ──────────────────────────────────────────────────────────────────────


class TestLoadOrDefault:
    def test_falls_back_to_default(self, tmp_path: Path) -> None:
        cls = load_classifier_or_default(tmp_path / "missing.json")
        assert cls.is_usable()
        assert cls.classifier_kind == "hand-tuned"

    def test_loads_valid_path(self, tmp_path: Path) -> None:
        path = tmp_path / "cls.json"
        save_classifier(default_classifier(), path)
        cls = load_classifier_or_default(path)
        assert cls.is_usable()


# ──────────────────────────────────────────────────────────────────────
# Renderer + serializer integration
# ──────────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_renders_primary(self) -> None:
        pred = IntentPrediction(
            primary="debug", primary_confidence=0.78,
            secondary="edit", secondary_confidence=0.10,
            full={"debug": 0.78, "edit": 0.10},
        )
        text = render_intent(pred)
        assert "TASK INTENT" in text
        assert "debug" in text
        assert "78%" in text

    def test_omits_secondary_below_5pct(self) -> None:
        pred = IntentPrediction(
            primary="debug", primary_confidence=0.95,
            secondary="edit", secondary_confidence=0.02,
            full={"debug": 0.95, "edit": 0.02},
        )
        text = render_intent(pred)
        assert "secondary" not in text

    def test_omits_secondary_when_none(self) -> None:
        pred = IntentPrediction(
            primary="debug", primary_confidence=0.95,
            secondary=None, secondary_confidence=0.0,
            full={"debug": 0.95},
        )
        text = render_intent(pred)
        assert "secondary" not in text


class TestSerializerIntegration:
    def test_section_appears_when_classifier_supplied(self) -> None:
        ctx = _mk_traj(["Read", "Edit", "Edit", "Read", "Edit"])
        text = serialize_temporal(
            ctx, intent_classifier=default_classifier(),
        )
        assert "TASK INTENT" in text

    def test_no_classifier_no_section(self) -> None:
        ctx = _mk_traj(["Read"] * 5)
        text = serialize_temporal(ctx)
        assert "TASK INTENT" not in text

    def test_empty_context_no_section(self) -> None:
        empty = TemporalContext(
            history=(), window_size=5,
            cumulative_token_trajectory=(),
            cache_hit_rate_trajectory=(),
            n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
            cache_hit_rate_max_drop_pp=0.0,
            token_velocity_per_turn=0.0,
            is_progress_stalled=False,
            distinct_tools_in_window=(),
        )
        text = serialize_temporal(
            empty, intent_classifier=default_classifier(),
        )
        assert "TASK INTENT" not in text

    def test_all_4_layers_render_together(self) -> None:
        from aegis.burnin.anomaly import default_baseline
        from aegis.burnin.trajectory_catalog import default_catalog

        ctx = _mk_traj(
            ["Read", "Edit", "Edit", "Bash", "Bash"],
            n_backtracks=1, n_errors=1,
        )
        text = serialize_temporal(
            ctx,
            baseline=default_baseline(),
            catalog=default_catalog(),
            intent_classifier=default_classifier(),
        )
        assert "TEMPORAL TRAJECTORY" in text
        assert "ANOMALIES vs BURN-IN" in text
        assert "NEAREST BURN-IN PATTERN" in text
        assert "TASK INTENT" in text
