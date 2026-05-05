"""Tests for ``aegis.burnin.trajectory_catalog`` — k-means clustering
of burn-in trajectories (PR-ι, Phase B Tier 2)."""

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
from aegis.burnin.trajectory_catalog import (
    EMBEDDING_DIM,
    EMBEDDING_HASH,
    TrajectoryCatalog,
    catalog_from_dict,
    catalog_to_dict,
    default_catalog,
    embed_trajectory,
    extract_catalog_from_sessions,
    load_catalog,
    load_catalog_or_default,
    render_nearest_clusters,
    save_catalog,
)

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _mk_temporal(
    *,
    n_history: int = 5,
    tool_names: list[str] | None = None,
    n_backtracks: int = 0,
    n_redundant: int = 0,
    n_errors: int = 0,
    n_failures: int = 0,
    cache_drop_pp: float = 0.0,
    token_velocity: float = 800.0,
    cumulative_traj: list[int] | None = None,
    hit_rate_traj: list[float] | None = None,
) -> TemporalContext:
    tools = tool_names or (["Read"] * n_history)
    if len(tools) != n_history:
        tools = (tools * n_history)[:n_history]

    snaps: list[ATVSnapshot] = []
    for i in range(n_history):
        rel = i - (n_history - 1)
        outcome = "failure" if i < n_failures else "success"
        backtrack = i < n_backtracks
        redundant = i < n_redundant
        is_error = i < n_errors
        snaps.append(ATVSnapshot(
            turn_index_rel=rel, ts_ns=0,
            tool_name=tools[i], args_excerpt="",
            decision="ALLOW", outcome=outcome,
            backtrack=backtrack, redundant=redundant, is_error=is_error,
        ))

    cum = tuple(cumulative_traj or [
        1000 * (i + 1) for i in range(n_history)
    ])
    hr = tuple(hit_rate_traj or [0.5] * n_history)
    return TemporalContext(
        history=tuple(snaps),
        window_size=n_history,
        cumulative_token_trajectory=cum,
        cache_hit_rate_trajectory=hr,
        n_backtracks=n_backtracks, n_redundant=n_redundant,
        n_errors=n_errors, n_failures=n_failures,
        cache_hit_rate_max_drop_pp=cache_drop_pp,
        token_velocity_per_turn=token_velocity,
        is_progress_stalled=False,
        distinct_tools_in_window=tuple(sorted(set(tools))),
    )


# ──────────────────────────────────────────────────────────────────────
# embed_trajectory
# ──────────────────────────────────────────────────────────────────────


class TestEmbed:
    def test_returns_fixed_size_vector(self) -> None:
        ctx = _mk_temporal()
        emb = embed_trajectory(ctx)
        assert emb.shape == (EMBEDDING_DIM,)
        assert emb.dtype == np.float32

    def test_empty_history_yields_zero_vector(self) -> None:
        ctx = TemporalContext(
            history=(), window_size=5,
            cumulative_token_trajectory=(),
            cache_hit_rate_trajectory=(),
            n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
            cache_hit_rate_max_drop_pp=0.0,
            token_velocity_per_turn=0.0,
            is_progress_stalled=False,
            distinct_tools_in_window=(),
        )
        emb = embed_trajectory(ctx)
        assert np.linalg.norm(emb) == 0.0

    def test_deterministic(self) -> None:
        ctx = _mk_temporal(n_backtracks=1, tool_names=["Edit"] * 5)
        a = embed_trajectory(ctx)
        b = embed_trajectory(ctx)
        np.testing.assert_array_equal(a, b)

    def test_different_traj_different_embedding(self) -> None:
        a = embed_trajectory(_mk_temporal(tool_names=["Read"] * 5))
        b = embed_trajectory(_mk_temporal(tool_names=["Edit"] * 5))
        # Tool name changes the BoW slots → different vector.
        assert not np.allclose(a, b)

    def test_inefficiency_signals_set_correct_slots(self) -> None:
        ctx = _mk_temporal(n_backtracks=2, n_redundant=1, n_errors=1)
        emb = embed_trajectory(ctx)
        # Slots 4 (backtrack), 5 (redundant), 6 (errors)
        assert emb[4] > 0.0
        assert emb[5] > 0.0
        assert emb[6] > 0.0

    def test_unknown_tool_routes_to_oov_slot(self) -> None:
        ctx = _mk_temporal(tool_names=["MyCustomMCP"] * 5)
        emb = embed_trajectory(ctx)
        # Slot 25 is OOV; its value should be 1.0 (5/5).
        assert emb[25] == pytest.approx(1.0)

    def test_features_bounded(self) -> None:
        ctx = _mk_temporal(
            cache_drop_pp=999.0,
            token_velocity=1_000_000,
            n_history=5, n_backtracks=5, n_redundant=5, n_errors=5,
        )
        emb = embed_trajectory(ctx)
        # All features clipped to [0, 1].
        assert float(emb.min()) >= 0.0
        assert float(emb.max()) <= 1.0


# ──────────────────────────────────────────────────────────────────────
# default_catalog
# ──────────────────────────────────────────────────────────────────────


class TestDefaultCatalog:
    def test_default_is_usable(self) -> None:
        cat = default_catalog()
        assert cat.is_usable()

    def test_has_8_clusters(self) -> None:
        cat = default_catalog()
        assert len(cat.clusters) == 8

    def test_centroids_correct_dim(self) -> None:
        cat = default_catalog()
        for c in cat.clusters:
            assert len(c.centroid) == EMBEDDING_DIM

    def test_cluster_labels_unique(self) -> None:
        cat = default_catalog()
        labels = [c.label for c in cat.clusters]
        assert len(set(labels)) == len(labels)

    def test_each_cluster_has_n_members(self) -> None:
        cat = default_catalog()
        for c in cat.clusters:
            assert c.n_members > 0


# ──────────────────────────────────────────────────────────────────────
# nearest()
# ──────────────────────────────────────────────────────────────────────


class TestNearest:
    def test_empty_catalog_returns_empty(self) -> None:
        empty = TrajectoryCatalog(
            version=1, embedding_dim=EMBEDDING_DIM,
            n_burnin_trajectories=0, extracted_at_ns=0,
            extracted_from="", clusters=(),
        )
        assert empty.nearest(np.zeros(EMBEDDING_DIM)) == []

    def test_returns_top_k_in_descending_order(self) -> None:
        cat = default_catalog()
        emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        emb[2] = 0.8           # cache_hit_rate slot
        emb[27] = 0.95         # success_ratio
        emb[9] = 0.30          # Read
        emb[10] = 0.30         # Edit
        emb[14] = 0.20         # Grep
        # → should be most similar to cluster_0 'linear-edit-flow'.
        matches = cat.nearest(emb, k=3)
        assert len(matches) == 3
        sims = [s for _, s in matches]
        assert sims == sorted(sims, reverse=True)

    def test_perfect_match_yields_high_cosine(self) -> None:
        cat = default_catalog()
        primary = cat.clusters[0]
        emb = np.asarray(primary.centroid, dtype=np.float32)
        matches = cat.nearest(emb, k=1)
        cluster, sim = matches[0]
        assert cluster.cluster_id == primary.cluster_id
        assert sim == pytest.approx(1.0, abs=1e-5)

    def test_zero_vector_safe(self) -> None:
        cat = default_catalog()
        matches = cat.nearest(np.zeros(EMBEDDING_DIM), k=2)
        # Doesn't crash; cosine with zero is 0.
        assert len(matches) == 2
        for _, sim in matches:
            assert sim == 0.0


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


class TestJSONRoundTrip:
    def test_round_trip(self, tmp_path: Path) -> None:
        cat = default_catalog()
        path = tmp_path / "cat.json"
        save_catalog(cat, path)
        loaded = load_catalog(path)
        assert loaded.version == cat.version
        assert len(loaded.clusters) == len(cat.clusters)
        # Centroids match.
        for original, restored in zip(
            cat.clusters, loaded.clusters, strict=True,
        ):
            assert original.label == restored.label
            np.testing.assert_array_almost_equal(
                np.asarray(original.centroid),
                np.asarray(restored.centroid),
            )

    def test_to_dict_serialisable(self) -> None:
        d = catalog_to_dict(default_catalog())
        json.dumps(d)

    def test_from_dict_tolerates_missing_keys(self) -> None:
        d = {"version": 1, "embedding_dim": EMBEDDING_DIM}
        cat = catalog_from_dict(d)
        assert cat.version == 1
        assert len(cat.clusters) == 0


# ──────────────────────────────────────────────────────────────────────
# load_catalog_or_default
# ──────────────────────────────────────────────────────────────────────


class TestLoadOrDefault:
    def test_falls_back_to_default(self, tmp_path: Path) -> None:
        # Pass a non-existent path → loader falls back to
        # default_catalog().
        cat = load_catalog_or_default(tmp_path / "missing.json")
        assert cat.is_usable()
        assert len(cat.clusters) == 8

    def test_loads_valid_path(self, tmp_path: Path) -> None:
        path = tmp_path / "cat.json"
        save_catalog(default_catalog(), path)
        cat = load_catalog_or_default(path)
        assert cat.is_usable()


# ──────────────────────────────────────────────────────────────────────
# extract_catalog_from_sessions
# ──────────────────────────────────────────────────────────────────────


def _write_transcript(
    path: Path, turns: list[tuple[str, int, int, int, int]],
) -> None:
    with path.open("w") as fh:
        for tool, in_t, out_t, cr, cc in turns:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use", "name": tool,
                        "id": f"tu_{tool}_{in_t}", "input": {},
                    }],
                    "usage": {
                        "input_tokens": in_t, "output_tokens": out_t,
                        "cache_read_input_tokens": cr,
                        "cache_creation_input_tokens": cc,
                    },
                },
            }) + "\n")


class TestExtract:
    def test_too_few_sessions_returns_empty_catalog(
        self, tmp_path: Path,
    ) -> None:
        # Only 3 transcripts → below min 8 threshold.
        ts_paths = []
        sids = []
        for i in range(3):
            p = tmp_path / f"t{i}.jsonl"
            _write_transcript(p, [("Read", 100, 50, 0, 0)] * 3)
            ts_paths.append(p)
            sids.append(f"s-{i}")

        audit_path = tmp_path / "audit.jsonl"
        audit_path.write_text("")

        cat = extract_catalog_from_sessions(
            transcript_paths=ts_paths, audit_path=audit_path,
            session_ids=sids,
        )
        assert not cat.is_usable()
        assert len(cat.clusters) == 0

    def test_extracts_clusters_from_enough_sessions(
        self, tmp_path: Path,
    ) -> None:
        # 12 sessions of varying patterns → should produce non-empty
        # clusters.
        ts_paths = []
        sids = []
        # 6 "Edit-flow" sessions
        for i in range(6):
            p = tmp_path / f"edit{i}.jsonl"
            _write_transcript(p, [
                ("Read", 100, 50, 0, 0),
                ("Edit", 200, 100, 50, 0),
                ("Read", 100, 50, 100, 0),
            ])
            ts_paths.append(p)
            sids.append(f"edit-{i}")
        # 6 "Bash-test" sessions
        for i in range(6):
            p = tmp_path / f"bash{i}.jsonl"
            _write_transcript(p, [
                ("Bash", 200, 100, 0, 0),
                ("Bash", 200, 100, 100, 0),
                ("Read", 100, 50, 200, 0),
            ])
            ts_paths.append(p)
            sids.append(f"bash-{i}")

        audit_path = tmp_path / "audit.jsonl"
        audit_path.write_text("")

        cat = extract_catalog_from_sessions(
            transcript_paths=ts_paths, audit_path=audit_path,
            session_ids=sids, n_clusters=4, window_size=3,
        )
        assert cat.is_usable()
        assert cat.n_burnin_trajectories == 12
        assert len(cat.clusters) >= 2

    def test_session_ids_length_mismatch_raises(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(ValueError, match="same length"):
            extract_catalog_from_sessions(
                transcript_paths=[tmp_path / "a.jsonl"],
                audit_path=tmp_path / "audit.jsonl",
                session_ids=["a", "b"],
            )


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_unusable_catalog_returns_empty(self) -> None:
        cat = TrajectoryCatalog(
            version=1, embedding_dim=EMBEDDING_DIM,
            n_burnin_trajectories=0, extracted_at_ns=0,
            extracted_from="", clusters=(),
        )
        assert render_nearest_clusters(_mk_temporal(), cat) == ""

    def test_renders_primary_match(self) -> None:
        cat = default_catalog()
        ctx = _mk_temporal(
            tool_names=["Read", "Edit", "Read", "Edit", "Grep"],
            cache_drop_pp=0.0,
        )
        text = render_nearest_clusters(ctx, cat)
        assert "NEAREST BURN-IN PATTERN" in text
        assert "cluster #" in text
        assert "cosine" in text

    def test_renders_secondary_when_k_geq_2(self) -> None:
        cat = default_catalog()
        ctx = _mk_temporal()
        text = render_nearest_clusters(ctx, cat, k=2)
        # Should mention 'also similar' for the secondary match.
        assert "also similar" in text

    def test_empty_history_returns_empty(self) -> None:
        cat = default_catalog()
        empty_ctx = TemporalContext(
            history=(), window_size=5,
            cumulative_token_trajectory=(),
            cache_hit_rate_trajectory=(),
            n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
            cache_hit_rate_max_drop_pp=0.0,
            token_velocity_per_turn=0.0,
            is_progress_stalled=False,
            distinct_tools_in_window=(),
        )
        assert render_nearest_clusters(empty_ctx, cat) == ""


# ──────────────────────────────────────────────────────────────────────
# serialize_temporal integration
# ──────────────────────────────────────────────────────────────────────


class TestSerializerIntegration:
    def test_section_appears_when_catalog_supplied(self) -> None:
        ctx = _mk_temporal(tool_names=["Read"] * 5)
        cat = default_catalog()
        text = serialize_temporal(ctx, catalog=cat)
        assert "NEAREST BURN-IN PATTERN" in text

    def test_no_catalog_no_section(self) -> None:
        ctx = _mk_temporal()
        text = serialize_temporal(ctx)
        assert "NEAREST BURN-IN PATTERN" not in text

    def test_baseline_and_catalog_both_render(self) -> None:
        from aegis.burnin.anomaly import default_baseline

        ctx = _mk_temporal(
            n_backtracks=1, n_errors=1,
            cache_drop_pp=80.0,
            tool_names=["Edit"] * 5,
        )
        text = serialize_temporal(
            ctx,
            baseline=default_baseline(),
            catalog=default_catalog(),
        )
        # Both sections present.
        assert "ANOMALIES vs BURN-IN" in text
        assert "NEAREST BURN-IN PATTERN" in text


# ──────────────────────────────────────────────────────────────────────
# Hash + version invariants
# ──────────────────────────────────────────────────────────────────────


class TestHashInvariant:
    def test_embedding_hash_stable(self) -> None:
        # The hash should be 64 hex chars (sha3-256).
        assert len(EMBEDDING_HASH) == 64
        assert all(c in "0123456789abcdef" for c in EMBEDDING_HASH)

    def test_embedding_dim_is_32(self) -> None:
        assert EMBEDDING_DIM == 32
