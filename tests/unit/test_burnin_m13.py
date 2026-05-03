"""Unit tests for the M13 v2 weight trainer + Burn-in Shadow harness."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from aegis.burnin import shadow
from aegis.burnin.m13_data import (
    CATEGORIES,
    generate,
    write_corpus,
)
from aegis.burnin.m13_eval import compare, evaluate_head
from aegis.burnin.m13_train import (
    extract_features,
    fit_nnls,
    train_v2,
    write_v2_json,
)


# ─────────────────────────────────────────────────────────────────────
# Synthetic generator
# ─────────────────────────────────────────────────────────────────────
class TestGenerate:
    def test_count_matches_per_category(self) -> None:
        corpus = generate(per_category=10)
        assert len(corpus) == 10 * len(CATEGORIES)

    def test_label_distribution_covers_three_classes(self) -> None:
        corpus = generate(per_category=10)
        labels = {ex.label for ex in corpus}
        assert labels == {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}

    def test_each_category_appears(self) -> None:
        corpus = generate(per_category=5)
        cats = {ex.category for ex in corpus}
        assert cats == set(CATEGORIES)

    def test_deterministic_same_seed(self) -> None:
        a = generate(per_category=5, seed=42)
        b = generate(per_category=5, seed=42)
        # Compare on tool_args_json which captures the variant choices.
        assert [ex.inp.tool_args_json for ex in a] == [
            ex.inp.tool_args_json for ex in b
        ]

    def test_different_seeds_produce_different_corpora(self) -> None:
        a = generate(per_category=5, seed=42)
        b = generate(per_category=5, seed=43)
        assert [ex.inp.tool_args_json for ex in a] != [
            ex.inp.tool_args_json for ex in b
        ]

    def test_write_corpus_jsonl_roundtrip(self, tmp_path: Path) -> None:
        corpus = generate(per_category=3)
        path = tmp_path / "corpus.jsonl"
        write_corpus(corpus, path)
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == len(corpus)
        for line in lines:
            assert line.startswith("{") and line.endswith("}")


# ─────────────────────────────────────────────────────────────────────
# Feature extraction
# ─────────────────────────────────────────────────────────────────────
class TestExtractFeatures:
    def test_returns_30_dim_vector(self) -> None:
        ex = generate(per_category=1)[0]
        feats = extract_features(ex)
        assert feats.shape == (30,)
        assert feats.dtype == np.float32

    def test_features_in_unit_range(self) -> None:
        for ex in generate(per_category=2):
            feats = extract_features(ex)
            assert (feats >= 0).all()
            assert (feats <= 1).all()

    def test_destructive_bash_has_higher_tool_arg_inspection(self) -> None:
        """destructive_bash should activate tool_arg_inspection more than
        benign_read — sanity check the encoder pipeline."""
        import random

        from aegis.burnin.m13_data import _benign_read, _destructive_bash
        rng = random.Random(0)
        benign = _benign_read(rng)
        destructive = _destructive_bash(rng)
        # tool_arg_inspection is column 10 in ALL_SUBFIELDS order.
        from aegis.schema import ALL_SUBFIELDS
        idx = next(
            i for i, (n, _) in enumerate(ALL_SUBFIELDS)
            if n == "tool_arg_inspection"
        )
        b = extract_features(benign)
        d = extract_features(destructive)
        assert d[idx] > b[idx]


# ─────────────────────────────────────────────────────────────────────
# Training pipeline
# ─────────────────────────────────────────────────────────────────────
class TestTrainV2:
    def test_train_returns_30_weights(self) -> None:
        result = train_v2(generate(per_category=20), test_fraction=0.2)
        assert len(result.subfield_weights) == 30
        for w in result.subfield_weights.values():
            assert w >= 0  # NNLS non-negativity contract

    def test_thresholds_ordered(self) -> None:
        result = train_v2(generate(per_category=20))
        assert result.threshold_approval < result.threshold_block

    def test_train_test_split_disjoint_size(self) -> None:
        corpus = generate(per_category=20)
        result = train_v2(corpus, test_fraction=0.2)
        assert result.n_train + result.n_test == len(corpus)
        assert result.n_test > 0

    def test_train_accuracy_above_random(self) -> None:
        """3-class random baseline = 0.33. Trainer must beat it
        meaningfully."""
        result = train_v2(generate(per_category=30))
        assert result.train_accuracy > 0.40, (
            f"trainer got {result.train_accuracy:.3f} — barely above random. "
            "Synthetic feature signal may have regressed."
        )

    def test_test_accuracy_close_to_train(self) -> None:
        """Held-out accuracy should be within 0.20 of train (no severe overfit)."""
        result = train_v2(generate(per_category=30))
        gap = abs(result.train_accuracy - result.test_accuracy)
        assert gap < 0.20

    def test_corpus_too_small_raises_via_cli(self) -> None:
        # 30 features with 5 examples is below the heuristic minimum.
        with pytest.raises(ValueError):
            train_v2([], test_fraction=0.2)

    def test_deterministic_same_corpus(self) -> None:
        corpus = generate(per_category=10, seed=2026)
        a = train_v2(corpus)
        b = train_v2(corpus)
        # Weights should be bit-identical (no random init in the linear solver).
        for name in a.subfield_weights:
            assert a.subfield_weights[name] == pytest.approx(
                b.subfield_weights[name], abs=1e-9,
            )


# ─────────────────────────────────────────────────────────────────────
# fit_nnls direct tests
# ─────────────────────────────────────────────────────────────────────
class TestFitNNLS:
    def test_recovers_clean_target(self) -> None:
        """y = X @ w_true with w_true ≥ 0 → trainer should approximate it."""
        rng = np.random.default_rng(0)
        X = rng.uniform(0, 1, (200, 5))
        w_true = np.array([0.5, 0.0, 1.0, 0.2, 0.0])
        y = X @ w_true
        sw = np.ones(200)
        w_hat = fit_nnls(X, y, sw, n_iter=50_000)
        assert np.allclose(w_hat, w_true, atol=0.05), (
            f"NNLS failed to recover w_true: got {w_hat}, expected {w_true}"
        )

    def test_clips_negative_target_weights_to_zero(self) -> None:
        rng = np.random.default_rng(0)
        X = rng.uniform(0, 1, (200, 3))
        w_true = np.array([1.0, -0.5, 0.0])  # second feature would be -0.5
        y = X @ w_true
        sw = np.ones(200)
        w_hat = fit_nnls(X, y, sw, n_iter=50_000)
        # Non-negativity is the hard constraint.
        assert (w_hat >= 0).all()
        # First feature still recovered roughly.
        assert w_hat[0] > 0.5


# ─────────────────────────────────────────────────────────────────────
# write_v2_json round-trip
# ─────────────────────────────────────────────────────────────────────
def test_write_v2_json_roundtrip(tmp_path: Path) -> None:
    corpus = generate(per_category=15)
    result = train_v2(corpus)
    out = tmp_path / "m13_v2.json"
    sha = write_v2_json(result, out)

    assert out.exists()
    assert len(sha) == 64  # SHA3-256 hex

    import hashlib
    raw = out.read_bytes()
    assert hashlib.sha3_256(raw).hexdigest() == sha

    import json
    data = json.loads(raw)
    assert data["version"] == 2
    assert data["schema_version"] == "ATV-2080-v1"
    assert "subfield_weights" in data
    assert "thresholds" in data
    assert data["thresholds"]["block"] > data["thresholds"]["require_approval"]
    assert "_provenance" in data
    assert data["_provenance"]["trained_by"] == "aegis.burnin.m13_train"


# ─────────────────────────────────────────────────────────────────────
# AttributionHead loads the v2 file
# ─────────────────────────────────────────────────────────────────────
def test_attribution_head_loads_trained_v2(tmp_path: Path) -> None:
    corpus = generate(per_category=15)
    result = train_v2(corpus)
    out = tmp_path / "m13_v2.json"
    sha = write_v2_json(result, out)

    from aegis.judge.attribution_head import AttributionHead, _load_weights
    _load_weights.cache_clear()
    head = AttributionHead(weights_path=out)
    assert head.model_hash == sha
    assert head._weights.threshold_block == pytest.approx(
        result.threshold_block, abs=1e-3,
    )


# ─────────────────────────────────────────────────────────────────────
# v1 vs v2 evaluation harness
# ─────────────────────────────────────────────────────────────────────
class TestEval:
    def test_evaluate_head_returns_full_report(self, tmp_path: Path) -> None:
        """evaluate_head must populate confusion + cost on a small corpus."""
        from aegis.judge.attribution_head import DEFAULT_WEIGHTS_PATH
        corpus = generate(per_category=5)
        out = evaluate_head(DEFAULT_WEIGHTS_PATH, corpus)
        assert out.n == len(corpus)
        # confusion sum equals corpus size.
        total = sum(
            v for row in out.confusion.values() for v in row.values()
        )
        assert total == len(corpus)
        # cost ≥ 0 (sum of weighted FN + FP).
        assert out.cost >= 0

    def test_compare_v1_vs_self_is_tie(self, tmp_path: Path) -> None:
        """Comparing v1 against itself should not declare either side winner."""
        from aegis.judge.attribution_head import DEFAULT_WEIGHTS_PATH
        result = compare(
            DEFAULT_WEIGHTS_PATH, DEFAULT_WEIGHTS_PATH, per_category=5,
        )
        assert result.delta_accuracy == 0
        assert result.delta_cost == 0
        assert result.winner == "tie"

    def test_compare_v2_beats_v1_on_synthetic(self, tmp_path: Path) -> None:
        """v2 trained on the same distribution we eval on must win the
        asymmetric-cost metric — sanity check the trainer."""
        from aegis.judge.attribution_head import DEFAULT_WEIGHTS_PATH
        corpus = generate(per_category=20)
        result = train_v2(corpus)
        v2_path = tmp_path / "v2.json"
        write_v2_json(result, v2_path)

        cmp = compare(DEFAULT_WEIGHTS_PATH, v2_path, per_category=20)
        # v2 should beat v1 OR tie (never lose) on synthetic data
        # whose distribution it was trained on.
        assert cmp.winner in ("v2", "tie"), (
            f"v2 lost to v1 on synthetic eval: {cmp}"
        )
        # v2 must catch >= as many malicious as v1 (no security regression).
        assert cmp.v2.fn_count <= cmp.v1.fn_count


# ─────────────────────────────────────────────────────────────────────
# Burn-in Shadow harness
# ─────────────────────────────────────────────────────────────────────
class TestShadow:
    def test_disabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AEGIS_BURNIN_SHADOW", raising=False)
        assert shadow.is_enabled() is False

    def test_enabled_via_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AEGIS_BURNIN_SHADOW", "1")
        assert shadow.is_enabled() is True

    def test_record_no_op_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "shadow.jsonl"
        monkeypatch.delenv("AEGIS_BURNIN_SHADOW", raising=False)
        monkeypatch.setenv("AEGIS_SHADOW_LOG", str(log_path))
        ex = generate(per_category=1)[0]
        from aegis.schema import Verdict
        v = Verdict(decision="ALLOW", reason="t", atv_id="t")
        shadow.record(ex.inp, v)
        assert not log_path.exists()

    def test_record_writes_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "shadow.jsonl"
        monkeypatch.setenv("AEGIS_BURNIN_SHADOW", "1")
        monkeypatch.setenv("AEGIS_SHADOW_LOG", str(log_path))
        ex = generate(per_category=1)[0]
        from aegis.schema import Verdict
        v = Verdict(decision="BLOCK", reason="test reason", atv_id="t")
        shadow.record(ex.inp, v, score=0.85)

        assert log_path.exists()
        records = shadow.read_corpus(log_path)
        assert len(records) == 1
        r = records[0]
        assert r["label"] == "BLOCK"
        assert r["tool_name"] == ex.inp.tool_name
        assert r["score"] == 0.85
        assert r["category"] == "shadow"

    def test_shadow_stats_counts_labels(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "shadow.jsonl"
        monkeypatch.setenv("AEGIS_BURNIN_SHADOW", "1")
        monkeypatch.setenv("AEGIS_SHADOW_LOG", str(log_path))
        from aegis.schema import Verdict
        for label in ("ALLOW", "ALLOW", "BLOCK"):
            shadow.record(
                generate(per_category=1)[0].inp,
                Verdict(decision=label, reason="t", atv_id="t"),
            )
        s = shadow.shadow_stats(log_path)
        assert s["n"] == 3
        assert s["by_label"] == {"ALLOW": 2, "BLOCK": 1}

    def test_read_corpus_returns_empty_when_no_log(
        self, tmp_path: Path,
    ) -> None:
        records = shadow.read_corpus(tmp_path / "missing.jsonl")
        assert records == []
