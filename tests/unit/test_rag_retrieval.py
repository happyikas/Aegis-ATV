"""Tests for ``aegis.judge.rag_retrieval``.

Three groups:

* ``TestIndex`` — embedding/cache plumbing (deterministic via dummy
  provider).
* ``TestRetrieve`` — top-k selection, empty corpus, k=0, fail-soft.
* ``TestLocalPhiIntegration`` — verifies ``local_phi._build_rag_block``
  pulls the corpus path even when case-memory is unavailable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from aegis.atv.embeddings import DummyEmbedding
from aegis.judge.rag_corpus import (
    RagChunk,
    RagCorpus,
    load_corpus,
    reset_corpus_cache,
)
from aegis.judge.rag_retrieval import (
    RagIndex,
    build_default_index,
    build_index,
    reset_index_cache,
    retrieve,
    retrieve_block,
)


@pytest.fixture(autouse=True)
def _isolate_aegis_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect AEGIS_HOME and reset RAG state so tests never poison the
    real cache or inherit a flag flipped by an earlier test."""
    from aegis.config import settings
    monkeypatch.setenv("AEGIS_HOME", str(tmp_path / "home"))
    object.__setattr__(settings, "aegis_rag_enabled", True)
    reset_corpus_cache()
    reset_index_cache()


def _make_corpus(*chunks: tuple[str, str, str]) -> RagCorpus:
    """Tiny helper to assemble an in-memory corpus from triples."""
    return RagCorpus(
        chunks=tuple(
            RagChunk(id=cid, category="rule", title=title, content=content)
            for cid, title, content in chunks
        ),
    )


# ── TestIndex ─────────────────────────────────────────────────────────


class TestIndex:
    def test_empty_corpus_yields_empty_index(self) -> None:
        corpus = RagCorpus(chunks=())
        index = build_index(corpus, DummyEmbedding(), dim=64)
        assert index.is_empty
        assert index.embeddings.shape == (0, 64)

    def test_dim_propagates(self) -> None:
        corpus = _make_corpus(("a", "A", "alpha"), ("b", "B", "beta"))
        index = build_index(corpus, DummyEmbedding(), dim=128)
        assert index.dim == 128
        assert index.embeddings.shape == (2, 128)

    def test_embeddings_are_l2_normalised(self) -> None:
        corpus = _make_corpus(*[(f"x{i}", f"T{i}", f"body{i}") for i in range(5)])
        index = build_index(corpus, DummyEmbedding(), dim=64)
        for row in index.embeddings:
            assert abs(float(np.linalg.norm(row)) - 1.0) < 1e-5

    def test_cache_round_trip(self, tmp_path: Path) -> None:
        corpus = _make_corpus(("a", "A", "alpha"), ("b", "B", "beta"))
        idx1 = build_index(corpus, DummyEmbedding(), dim=64, use_cache=True)
        cache_dir = Path(os.environ["AEGIS_HOME"]) / "rag_cache"
        cached = list(cache_dir.glob("corpus_*.npz"))
        assert len(cached) == 1, f"expected 1 cache file, got {cached}"

        # Second build should load from cache (same fingerprint)
        idx2 = build_index(corpus, DummyEmbedding(), dim=64, use_cache=True)
        np.testing.assert_array_equal(idx1.embeddings, idx2.embeddings)

    def test_cache_invalidates_on_content_change(self) -> None:
        c1 = _make_corpus(("a", "A", "alpha"))
        c2 = _make_corpus(("a", "A", "different content"))
        idx1 = build_index(c1, DummyEmbedding(), dim=64)
        idx2 = build_index(c2, DummyEmbedding(), dim=64)
        # Different content → different cache key → different embeddings
        assert not np.allclose(idx1.embeddings, idx2.embeddings)

    def test_use_cache_false_skips_disk(self) -> None:
        corpus = _make_corpus(("a", "A", "alpha"))
        build_index(corpus, DummyEmbedding(), dim=64, use_cache=False)
        cache_dir = Path(os.environ["AEGIS_HOME"]) / "rag_cache"
        assert not cache_dir.exists() or not list(cache_dir.glob("*.npz"))

    def test_default_index_loads_shipped_corpus(self) -> None:
        index = build_default_index()
        assert not index.is_empty
        assert index.embeddings.shape[0] >= 30  # ≥31 rule chunks


# ── TestRetrieve ──────────────────────────────────────────────────────


class TestRetrieve:
    def test_empty_corpus_returns_empty(self) -> None:
        empty = RagIndex(
            corpus=RagCorpus(),
            embeddings=np.zeros((0, 64), dtype=np.float32),
            provider_name="(empty)", dim=64,
        )
        assert retrieve("anything", index=empty) == []

    def test_top_k_count(self) -> None:
        corpus = _make_corpus(*[(f"x{i}", f"T{i}", f"body{i}") for i in range(10)])
        index = build_index(corpus, DummyEmbedding(), dim=64)
        hits = retrieve("query text", k=3, index=index, provider=DummyEmbedding())
        assert len(hits) == 3

    def test_top_k_zero(self) -> None:
        corpus = _make_corpus(("a", "A", "alpha"), ("b", "B", "beta"))
        index = build_index(corpus, DummyEmbedding(), dim=64)
        assert retrieve("q", k=0, index=index, provider=DummyEmbedding()) == []

    def test_results_sorted_by_score(self) -> None:
        corpus = _make_corpus(*[(f"x{i}", f"T{i}", f"body{i}") for i in range(8)])
        index = build_index(corpus, DummyEmbedding(), dim=64)
        hits = retrieve("query", k=5, index=index, provider=DummyEmbedding())
        scores = [s for _c, s in hits]
        assert scores == sorted(scores, reverse=True), scores

    def test_dim_mismatch_returns_empty(self) -> None:
        corpus = _make_corpus(("a", "A", "alpha"))
        index = build_index(corpus, DummyEmbedding(), dim=64)
        # Manually call .search with a wrong-shape vector
        bad_q = np.ones((32,), dtype=np.float32)
        assert index.search(bad_q, k=3) == []

    def test_zero_query_vector_returns_empty(self) -> None:
        corpus = _make_corpus(("a", "A", "alpha"))
        index = build_index(corpus, DummyEmbedding(), dim=64)
        zero = np.zeros((64,), dtype=np.float32)
        assert index.search(zero, k=3) == []

    def test_retrieve_block_renders_chunks(self) -> None:
        corpus = _make_corpus(
            ("destructive-1", "Recursive purge", "purges directory trees"),
            ("destructive-2", "Force-push", "rewrites canonical history"),
            ("benign-1", "Read /tmp", "routine read of a temp file"),
        )
        index = build_index(corpus, DummyEmbedding(), dim=64)
        block = retrieve_block(
            "purge directory tree",
            k=2, index=index, provider=DummyEmbedding(),
        )
        assert "[rule]" in block
        # 2 chunks rendered
        assert block.count("[rule]") == 2

    def test_retrieve_block_failsoft(self) -> None:
        # Provoke failure: the index has wrong dim than what get_provider gives.
        corpus = _make_corpus(("a", "A", "alpha"))
        index = build_index(corpus, DummyEmbedding(), dim=64)

        class BadProvider:
            def embed(self, text: str, dim: int) -> np.ndarray:
                raise RuntimeError("simulated provider failure")

        block = retrieve_block(
            "x", k=3, index=index, provider=BadProvider(),
        )
        assert block == ""

    def test_default_corpus_retrieval_top1_for_known_concept(self) -> None:
        index = build_default_index()
        hits = retrieve(
            "force pushing to main branch is dangerous",
            k=3, index=index, provider=DummyEmbedding(),
        )
        # We can't make strong claims about *which* chunk wins under the
        # dummy provider's SHA3 hash (no semantics), but we should get
        # exactly k hits ordered by score.
        assert len(hits) == 3


# ── TestLocalPhiIntegration ───────────────────────────────────────────


class TestLocalPhiIntegration:
    def test_build_rag_block_includes_corpus_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Build a synthetic corpus and point `load_default_corpus` at it.
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "rules.jsonl").write_text(
            json.dumps({
                "id": "rule-test",
                "category": "rule",
                "title": "Test rule for integration",
                "content": "This is a fixture rule used by the test.",
            }) + "\n",
            encoding="utf-8",
        )

        # Patch the loader to point at our tmp corpus.
        monkeypatch.setattr(
            "aegis.judge.rag_corpus.default_corpus_dir",
            lambda: corpus_dir,
        )
        reset_corpus_cache()
        reset_index_cache()

        from aegis.judge import local_phi
        block = local_phi._build_rag_block(
            atv=None, inp=None, summary="test summary",
        )
        # Corpus path should fire even though atv=None disables case-memory.
        assert "[rule] Test rule for integration" in block

    def test_build_rag_block_empty_summary_returns_no_corpus(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.judge import local_phi
        # Empty summary → corpus path skipped; case-memory needs bge-local.
        block = local_phi._build_rag_block(
            atv=None, inp=None, summary="",
        )
        assert "[rule]" not in block

    def test_build_rag_block_failsoft_on_corpus_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch retrieve_block to raise. The wrapper must swallow it.
        def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("simulated retrieval failure")

        monkeypatch.setattr(
            "aegis.judge.rag_retrieval.retrieve_block", _boom,
        )

        from aegis.judge import local_phi
        block = local_phi._build_rag_block(
            atv=None, inp=None, summary="test",
        )
        # Still doesn't crash — caller proceeds with no RAG.
        assert isinstance(block, str)


# ── TestAnchorTimestamp (PR ②) ────────────────────────────────────────


def _ns(iso: str) -> int:
    """ISO 8601 → nanoseconds (UTC)."""
    from datetime import datetime
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


def _supersession_corpus() -> RagCorpus:
    """Synthetic two-version pair: v0 valid 2024-01..2024-08, v1 valid
    2024-08 onwards, plus a timeless fixture."""
    return RagCorpus(chunks=(
        RagChunk(
            id="rule-x-v0", category="rule",
            title="X v0", content="old version of rule X",
            valid_from="2024-01-01T00:00:00Z",
            valid_until="2024-08-01T00:00:00Z",
        ),
        RagChunk(
            id="rule-x-v1", category="rule",
            title="X v1", content="current version of rule X",
            valid_from="2024-08-01T00:00:00Z",
            supersedes="rule-x-v0",
        ),
        RagChunk(
            id="rule-timeless", category="rule",
            title="timeless", content="never expires",
        ),
    ))


class TestAnchorTimestamp:
    def test_search_no_anchor_returns_all(self) -> None:
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        # Without anchor, search returns top-k across the full corpus
        # regardless of validity (back-compat).
        q = np.random.default_rng(0).standard_normal(64).astype(np.float32)
        hits = index.search(q, k=3)
        assert {c.id for c, _ in hits} == {"rule-x-v0", "rule-x-v1", "rule-timeless"}

    def test_search_anchor_pre_v1_returns_v0(self) -> None:
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        q = np.random.default_rng(0).standard_normal(64).astype(np.float32)
        hits = index.search(
            q, k=3, anchor_ts_ns=_ns("2024-06-01T00:00:00Z"),
        )
        ids = {c.id for c, _ in hits}
        assert "rule-x-v0" in ids
        assert "rule-x-v1" not in ids
        assert "rule-timeless" in ids

    def test_search_anchor_post_v1_returns_v1(self) -> None:
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        q = np.random.default_rng(0).standard_normal(64).astype(np.float32)
        hits = index.search(
            q, k=3, anchor_ts_ns=_ns("2025-01-01T00:00:00Z"),
        )
        ids = {c.id for c, _ in hits}
        assert "rule-x-v1" in ids
        assert "rule-x-v0" not in ids
        assert "rule-timeless" in ids

    def test_search_walks_past_invalid_to_fill_k(self) -> None:
        """If the highest-scoring chunk is invalid at the anchor, search
        must continue down the score order to fill k valid hits."""
        # Build a corpus where chunk 0 is "deprecated", chunks 1+ are live.
        chunks = [
            RagChunk(
                id="dead", category="rule", title="dead", content="dead",
                valid_until="2020-01-01T00:00:00Z",
            ),
        ] + [
            RagChunk(
                id=f"live-{i}", category="rule",
                title=f"live{i}", content=f"alive{i}",
            )
            for i in range(5)
        ]
        index = build_index(
            RagCorpus(chunks=tuple(chunks)),
            DummyEmbedding(), dim=64,
        )
        q = np.random.default_rng(0).standard_normal(64).astype(np.float32)
        hits = index.search(q, k=3, anchor_ts_ns=_ns("2024-06-01T00:00:00Z"))
        assert len(hits) == 3
        assert "dead" not in {c.id for c, _ in hits}

    def test_retrieve_passes_anchor_through(self) -> None:
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        from aegis.judge.rag_retrieval import retrieve
        hits = retrieve(
            "anything", k=3, index=index, provider=DummyEmbedding(),
            anchor_ts_ns=_ns("2024-06-01T00:00:00Z"),
        )
        assert "rule-x-v1" not in {c.id for c, _ in hits}

    def test_retrieve_block_default_anchor_is_now(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """retrieve_block with anchor_ts_ns=None should anchor at
        time.time_ns() — i.e. expired chunks are filtered out
        automatically in the live-judge path."""
        from aegis.config import settings
        from aegis.judge.rag_retrieval import retrieve_block
        object.__setattr__(settings, "aegis_rag_enabled", True)
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        block = retrieve_block(
            "anything", k=3, index=index, provider=DummyEmbedding(),
        )
        # rule-x-v0 has expired (2024-08), so live anchor should not
        # surface it. v1 (current) and timeless should appear.
        assert "X v0" not in block
        assert "X v1" in block or "timeless" in block

    def test_retrieve_block_explicit_anchor_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings
        from aegis.judge.rag_retrieval import retrieve_block
        object.__setattr__(settings, "aegis_rag_enabled", True)
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        # Replay anchor pre-v1 → v0 should surface, v1 should not.
        block = retrieve_block(
            "anything", k=3, index=index, provider=DummyEmbedding(),
            anchor_ts_ns=_ns("2024-06-01T00:00:00Z"),
        )
        assert "X v0" in block
        assert "X v1" not in block

    def test_retrieve_for_audit_record_uses_record_ts(self) -> None:
        from aegis.judge.rag_retrieval import retrieve_for_audit_record
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        rec = {
            "ts_ns": _ns("2024-06-01T00:00:00Z"),
            "tool": "Bash",
            "decision": "BLOCK",
        }
        hits = retrieve_for_audit_record(
            rec, "anything", k=3, index=index, provider=DummyEmbedding(),
        )
        assert "rule-x-v1" not in {c.id for c, _ in hits}

    def test_retrieve_for_audit_record_missing_ts_falls_back(self) -> None:
        """Old audit lines without ts_ns: anchor is None → behaves like
        retrieve() with no time filter (returns all)."""
        from aegis.judge.rag_retrieval import retrieve_for_audit_record
        index = build_index(
            _supersession_corpus(), DummyEmbedding(), dim=64,
        )
        rec = {"tool": "Bash"}  # no ts_ns
        hits = retrieve_for_audit_record(
            rec, "anything", k=3, index=index, provider=DummyEmbedding(),
        )
        # Both v0 and v1 are visible because anchor=None disables the filter.
        assert {c.id for c, _ in hits} == {
            "rule-x-v0", "rule-x-v1", "rule-timeless",
        }

    def test_disabled_rag_short_circuits_anchor(self) -> None:
        """When aegis_rag_enabled=False, retrieve_block returns "" even
        if an anchor is supplied."""
        from aegis.config import settings
        from aegis.judge.rag_retrieval import retrieve_block
        object.__setattr__(settings, "aegis_rag_enabled", False)
        block = retrieve_block(
            "anything", anchor_ts_ns=_ns("2024-06-01T00:00:00Z"),
        )
        assert block == ""

    def test_search_anchor_with_no_validity_returns_all(self) -> None:
        """Existing chunks (no validity fields) must remain visible
        when an anchor is supplied — they are 'always valid'."""
        # Use the shipped corpus. None of those 38 chunks have validity.
        from aegis.judge.rag_corpus import (
            load_default_corpus,
            reset_corpus_cache,
        )
        reset_corpus_cache()
        corpus = load_default_corpus()
        index = build_index(corpus, DummyEmbedding(), dim=64)
        q = np.random.default_rng(0).standard_normal(64).astype(np.float32)
        hits = index.search(
            q, k=5, anchor_ts_ns=_ns("2024-06-01T00:00:00Z"),
        )
        assert len(hits) == 5  # all 38 are timeless → all eligible


# ── small helper: silence unused-import warnings ──────────────────────


_ = (load_corpus, patch)
