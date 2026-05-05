"""Tests for ``aegis.burnin.action_embeddings`` — tool-action
embedding table (PR-κ, Phase B Tier 2)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from aegis.burnin.action_embeddings import (
    ACTION_EMBEDDING_DIM,
    TABLE_HASH,
    ActionTable,
    action_similarity,
    default_action_table,
    default_table,
    embed_action,
    embed_action_sequence,
    load_table,
    load_table_or_default,
    nearest_actions,
    save_table,
    table_from_dict,
    table_to_dict,
)

# ──────────────────────────────────────────────────────────────────────
# Schema invariants
# ──────────────────────────────────────────────────────────────────────


class TestSchema:
    def test_dim_is_16(self) -> None:
        assert ACTION_EMBEDDING_DIM == 16

    def test_default_table_usable(self) -> None:
        t = default_table()
        assert t.is_usable()
        assert t.embedding_dim == ACTION_EMBEDDING_DIM

    def test_default_covers_canonical_tools(self) -> None:
        t = default_table()
        names = set(t.embeddings.keys())
        for canonical in (
            "Read", "Edit", "Write", "MultiEdit", "Bash", "Grep",
            "Glob", "WebFetch", "WebSearch",
        ):
            assert canonical in names

    def test_all_vectors_correct_dim(self) -> None:
        t = default_table()
        for name, vec in t.embeddings.items():
            assert len(vec) == ACTION_EMBEDDING_DIM, (
                f"{name} has wrong dim"
            )

    def test_table_hash_set(self) -> None:
        t = default_table()
        assert len(t.table_hash) == 64
        assert t.table_hash == TABLE_HASH


# ──────────────────────────────────────────────────────────────────────
# embed_action
# ──────────────────────────────────────────────────────────────────────


class TestEmbedAction:
    def test_known_returns_nonzero(self) -> None:
        emb = embed_action("Read")
        assert emb.shape == (ACTION_EMBEDDING_DIM,)
        assert float(np.linalg.norm(emb)) > 0.0

    def test_unknown_returns_zero(self) -> None:
        emb = embed_action("CompletelyUnknownTool")
        assert float(np.linalg.norm(emb)) == 0.0

    def test_dtype_float32(self) -> None:
        emb = embed_action("Edit")
        assert emb.dtype == np.float32


# ──────────────────────────────────────────────────────────────────────
# Semantic structure — the heart of PR-κ
# ──────────────────────────────────────────────────────────────────────


class TestSemanticStructure:
    """The hand-tuned table must produce sensible cosine similarities.
    These are the invariants we'd want preserved if a learned table
    ever replaces the default."""

    def test_read_grep_similar_both_read_only(self) -> None:
        # Both pure exploration / read-only.
        sim = action_similarity("Read", "Grep")
        assert sim >= 0.5

    def test_edit_multiedit_similar_both_write(self) -> None:
        sim = action_similarity("Edit", "MultiEdit")
        assert sim >= 0.5

    def test_read_edit_dissimilar(self) -> None:
        # Read vs Edit are conceptually opposites.
        sim = action_similarity("Read", "Edit")
        # They share the file_targeted axis a bit, but the
        # read/write axis is very different.
        assert sim < 0.3

    def test_webfetch_websearch_similar_both_network(self) -> None:
        sim = action_similarity("WebFetch", "WebSearch")
        assert sim >= 0.6

    def test_unknown_similarity_is_zero(self) -> None:
        sim = action_similarity("Read", "TotallyMadeUpTool")
        assert sim == 0.0


# ──────────────────────────────────────────────────────────────────────
# nearest_actions
# ──────────────────────────────────────────────────────────────────────


class TestNearest:
    def test_returns_top_k_descending(self) -> None:
        results = nearest_actions("Read", k=3)
        assert len(results) == 3
        sims = [s for _, s in results]
        assert sims == sorted(sims, reverse=True)

    def test_excludes_self_by_default(self) -> None:
        results = nearest_actions("Read", k=5)
        names = [n for n, _ in results]
        assert "Read" not in names

    def test_includes_self_when_requested(self) -> None:
        results = nearest_actions("Read", k=5, exclude_self=False)
        names = [n for n, _ in results]
        assert "Read" in names

    def test_read_nearest_includes_grep_or_glob(self) -> None:
        # Read-only exploration tools should be among Read's nearest.
        results = nearest_actions("Read", k=3)
        names = {n for n, _ in results}
        assert "Grep" in names or "Glob" in names

    def test_query_by_embedding(self) -> None:
        emb = embed_action("Edit")
        results = nearest_actions(emb, k=3)
        # Edit is the query embedding itself; with no exclude_self
        # logic via name, the closest match should be Edit at 1.0.
        assert results[0][1] >= 0.99
        assert results[0][0] == "Edit"

    def test_query_dim_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            nearest_actions(np.zeros(10), k=3)

    def test_zero_query_returns_empty(self) -> None:
        # Zero vector → no informative similarity.
        results = nearest_actions(np.zeros(ACTION_EMBEDDING_DIM), k=3)
        assert results == []

    def test_unknown_name_returns_empty(self) -> None:
        results = nearest_actions("UnknownTool", k=3)
        assert results == []


# ──────────────────────────────────────────────────────────────────────
# embed_action_sequence
# ──────────────────────────────────────────────────────────────────────


class TestSequenceEmbed:
    def test_empty_returns_zero(self) -> None:
        emb = embed_action_sequence([])
        assert float(np.linalg.norm(emb)) == 0.0

    def test_single_tool_matches_lookup(self) -> None:
        single = embed_action_sequence(["Read"])
        direct = embed_action("Read")
        np.testing.assert_array_almost_equal(single, direct)

    def test_mean_pool_two_tools(self) -> None:
        emb = embed_action_sequence(["Read", "Edit"])
        expected = (embed_action("Read") + embed_action("Edit")) / 2
        np.testing.assert_array_almost_equal(emb, expected, decimal=5)

    def test_unknown_tool_drags_toward_zero(self) -> None:
        emb_known = embed_action_sequence(["Read", "Read"])
        emb_mixed = embed_action_sequence(["Read", "UnknownToolXYZ"])
        # Including an unknown halves the magnitude (mean
        # with a zero vector).
        assert float(np.linalg.norm(emb_mixed)) < float(
            np.linalg.norm(emb_known)
        )


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


class TestJSON:
    def test_round_trip(self, tmp_path: Path) -> None:
        t = default_table()
        path = tmp_path / "t.json"
        save_table(t, path)
        loaded = load_table(path)
        assert loaded.embedding_dim == t.embedding_dim
        assert set(loaded.embeddings.keys()) == set(t.embeddings.keys())

    def test_to_dict_serialisable(self) -> None:
        d = table_to_dict(default_table())
        json.dumps(d)

    def test_from_dict_tolerates_minimal(self) -> None:
        d = {"embedding_dim": ACTION_EMBEDDING_DIM, "embeddings": {}}
        t = table_from_dict(d)
        # Empty → not usable.
        assert not t.is_usable()


# ──────────────────────────────────────────────────────────────────────
# Default loader
# ──────────────────────────────────────────────────────────────────────


class TestLoadOrDefault:
    def test_falls_back_to_default(self, tmp_path: Path) -> None:
        t = load_table_or_default(tmp_path / "missing.json")
        assert t.is_usable()
        assert t.table_kind == "hand-tuned"

    def test_loads_valid_path(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        save_table(default_table(), path)
        t = load_table_or_default(path)
        assert t.is_usable()


# ──────────────────────────────────────────────────────────────────────
# Custom table
# ──────────────────────────────────────────────────────────────────────


class TestCustomTable:
    def test_user_can_pass_custom_table(self) -> None:
        custom_embs = {
            "FooTool": tuple([1.0] + [0.0] * (ACTION_EMBEDDING_DIM - 1)),
            "BarTool": tuple([1.0] + [0.0] * (ACTION_EMBEDDING_DIM - 1)),
        }
        custom = ActionTable(
            embedding_dim=ACTION_EMBEDDING_DIM,
            embeddings=custom_embs,
            table_kind="custom",
        )
        # FooTool and BarTool are identical → similarity 1.0.
        sim = action_similarity("FooTool", "BarTool", table=custom)
        assert sim == pytest.approx(1.0, abs=1e-5)


# ──────────────────────────────────────────────────────────────────────
# Module export surface
# ──────────────────────────────────────────────────────────────────────


class TestExports:
    def test_default_action_table_keys_unique(self) -> None:
        # The dict factory must produce non-empty unique keys.
        d = default_action_table()
        assert len(d) > 5
        assert len(set(d.keys())) == len(d)

    def test_default_action_table_is_a_dict(self) -> None:
        d = default_action_table()
        assert isinstance(d, dict)
