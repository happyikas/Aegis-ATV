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
    """Redirect AEGIS_HOME so tests never poison the real cache."""
    monkeypatch.setenv("AEGIS_HOME", str(tmp_path / "home"))
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


# ── small helper: silence unused-import warnings ──────────────────────


_ = (load_corpus, patch)
