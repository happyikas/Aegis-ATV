"""Unit tests for the step340 RAG case memory.

Real-BGE retrieval is exercised in the matching integration test
``tests/integration/test_real_bge_e2e.py`` (covered by PR #22). The
unit tests here cover:

* CaseMemory shape contracts (dim / n / is_empty).
* npz round-trip.
* search() top-K + min_similarity threshold.
* Empty memory degrades to ``[]`` rather than crashing.
* build_from_corpus produces L2-normalised stored vectors.
* format_cases_for_prompt produces a bounded string.
* Default-memory caching + reset_memory_cache.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from aegis.judge.case_memory import (
    CaseMemory,
    RetrievedCase,
    format_cases_for_prompt,
    load_default_memory,
    reset_memory_cache,
)


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _toy_memory(*, dim: int = 8, n: int = 5) -> CaseMemory:
    """Tiny deterministic memory with known structure for retrieval tests."""
    rng = np.random.default_rng(0)
    embs = []
    for i in range(n):
        v = np.zeros(dim, dtype=np.float32)
        v[i % dim] = 1.0
        # Slight rotation to avoid exact duplicates
        v = v + 0.05 * rng.standard_normal(dim).astype(np.float32)
        embs.append(_unit(v))
    return CaseMemory(
        embeddings=np.stack(embs),
        texts=np.array([f"case {i}" for i in range(n)], dtype=object),
        labels=np.array(
            ["ALLOW" if i % 3 == 0 else "BLOCK" for i in range(n)], dtype=object,
        ),
        reasons=np.array([f"reason {i}" for i in range(n)], dtype=object),
        meta={"toy": True},
    )


# ─────────────────────────────────────────────────────────────────────
# Shape contracts
# ─────────────────────────────────────────────────────────────────────
class TestShape:
    def test_n_dim_match_inputs(self) -> None:
        m = _toy_memory(dim=4, n=3)
        assert m.n == 3
        assert m.dim == 4
        assert not m.is_empty

    def test_empty_memory_is_empty(self) -> None:
        m = CaseMemory.empty(dim=768)
        assert m.is_empty
        assert m.n == 0
        assert m.dim == 768

    def test_constructor_rejects_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            CaseMemory(
                embeddings=np.zeros((3, 4), dtype=np.float32),
                texts=np.array(["a", "b"], dtype=object),  # wrong length
                labels=np.array(["A", "B", "C"], dtype=object),
                reasons=np.array(["r", "r", "r"], dtype=object),
            )

    def test_constructor_rejects_1d_embeddings(self) -> None:
        with pytest.raises(ValueError, match="2-D"):
            CaseMemory(
                embeddings=np.zeros(8, dtype=np.float32),
                texts=np.array([], dtype=object),
                labels=np.array([], dtype=object),
                reasons=np.array([], dtype=object),
            )


# ─────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────
class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        m = _toy_memory(dim=4, n=3)
        path = tmp_path / "mem.npz"
        m.save(path)
        loaded = CaseMemory.load(path)
        assert loaded.n == m.n
        assert loaded.dim == m.dim
        assert np.array_equal(loaded.embeddings, m.embeddings)
        assert list(loaded.labels) == list(m.labels)
        assert list(loaded.texts) == list(m.texts)
        assert loaded.meta == m.meta

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        # CaseMemory.load doesn't synthesise a fallback — that's
        # load_default_memory's job. Direct load should propagate
        # the underlying ndarray load error.
        with pytest.raises((FileNotFoundError, OSError, ValueError)):
            CaseMemory.load(tmp_path / "ghost.npz")


# ─────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────
class TestSearch:
    def test_returns_at_most_k(self) -> None:
        m = _toy_memory(dim=4, n=5)
        q = m.embeddings[0]  # exact match in storage
        out = m.search(q, k=2, min_similarity=0.0)
        assert len(out) <= 2

    def test_top_neighbour_is_self_for_stored_query(self) -> None:
        m = _toy_memory(dim=4, n=5)
        q = m.embeddings[2]
        out = m.search(q, k=1, min_similarity=0.0)
        assert len(out) == 1
        assert out[0].text == "case 2"
        assert out[0].similarity > 0.99

    def test_min_similarity_filters_low(self) -> None:
        m = _toy_memory(dim=8, n=5)
        # Random orthogonal-ish vector
        q = np.zeros(8, dtype=np.float32)
        q[6] = 1.0  # not stored
        out = m.search(q, k=5, min_similarity=0.5)
        # All five stored vectors are roughly orthogonal to q;
        # min_sim should drop them all.
        for c in out:
            assert c.similarity >= 0.5

    def test_empty_memory_returns_empty(self) -> None:
        m = CaseMemory.empty(dim=768)
        q = np.zeros(768, dtype=np.float32)
        q[0] = 1.0
        assert m.search(q, k=3) == []

    def test_dim_mismatch_raises(self) -> None:
        m = _toy_memory(dim=4, n=3)
        with pytest.raises(ValueError, match="query shape"):
            m.search(np.zeros(8, dtype=np.float32), k=1)

    def test_results_are_retrievedcase_dataclasses(self) -> None:
        m = _toy_memory(dim=4, n=3)
        out = m.search(m.embeddings[0], k=1, min_similarity=0.0)
        assert len(out) == 1
        assert isinstance(out[0], RetrievedCase)
        assert out[0].text and out[0].label and out[0].reason
        assert -1.0 <= out[0].similarity <= 1.0 + 1e-3


# ─────────────────────────────────────────────────────────────────────
# build_from_corpus
# ─────────────────────────────────────────────────────────────────────
class _StubProvider:
    """In-test embedding provider — returns deterministic per-text vectors."""

    def __init__(self, dim: int = 768) -> None:
        self._dim = dim

    def embed(self, text: str, dim: int) -> np.ndarray:
        # Map text → deterministic vector via SHA3.
        import hashlib
        h = hashlib.sha3_256(text.encode("utf-8")).digest()
        out = np.frombuffer(h * (dim // len(h) + 1), dtype=np.uint8)[:dim].astype(np.float32)
        out = (out - 127.5) / 128.0
        return _unit(out)


class TestBuildFromCorpus:
    def test_builds_empty_when_corpus_empty(self) -> None:
        m = CaseMemory.build_from_corpus([], embed_provider=_StubProvider())
        assert m.is_empty
        assert m.dim == 768

    def test_stored_vectors_are_unit_norm(self) -> None:
        from aegis.burnin.m13_data import generate
        corpus = generate(per_category=2)
        m = CaseMemory.build_from_corpus(corpus, embed_provider=_StubProvider())
        norms = np.linalg.norm(m.embeddings, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3), (
            f"non-unit-norm rows: min={norms.min()}, max={norms.max()}"
        )

    def test_meta_records_source_and_n(self) -> None:
        from aegis.burnin.m13_data import generate
        corpus = generate(per_category=2)
        m = CaseMemory.build_from_corpus(
            corpus, embed_provider=_StubProvider(),
            meta={"source": "synthetic"},
        )
        assert m.meta.get("n") == len(corpus)
        assert m.meta.get("source") == "synthetic"
        assert "built_at" in m.meta

    def test_text_includes_tool_name_and_args(self) -> None:
        from aegis.burnin.m13_data import generate
        corpus = generate(per_category=1)
        m = CaseMemory.build_from_corpus(corpus, embed_provider=_StubProvider())
        for i in range(m.n):
            t = str(m.texts[i])
            assert "tool=" in t
            assert "args=" in t


# ─────────────────────────────────────────────────────────────────────
# format_cases_for_prompt
# ─────────────────────────────────────────────────────────────────────
class TestFormatPrompt:
    def test_empty_returns_empty_string(self) -> None:
        assert format_cases_for_prompt([]) == ""

    def test_renders_top_first(self) -> None:
        cases = [
            RetrievedCase(text="abc destructive", label="BLOCK",
                          reason="rm -rf", similarity=0.95),
            RetrievedCase(text="xyz benign", label="ALLOW",
                          reason="ls", similarity=0.40),
        ]
        out = format_cases_for_prompt(cases)
        assert out.startswith("Similar past cases")
        # First case (BLOCK / cos=0.95) appears before second case in the block.
        assert out.index("0.95") < out.index("0.40")

    def test_max_chars_caps_output(self) -> None:
        cases = [
            RetrievedCase(text="long " * 50, label="BLOCK",
                          reason="r", similarity=0.5)
            for _ in range(20)
        ]
        out = format_cases_for_prompt(cases, max_chars=400)
        assert len(out) <= 600   # header + a few cases, well under 20×line


# ─────────────────────────────────────────────────────────────────────
# Default-memory caching
# ─────────────────────────────────────────────────────────────────────
class TestDefaultMemoryCache:
    def test_returns_empty_when_default_path_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # Point the cached default at a non-existent path.
        from aegis.judge import case_memory as _cm
        monkeypatch.setattr(_cm, "DEFAULT_CASE_MEMORY_PATH", tmp_path / "nope.npz")
        reset_memory_cache()
        m = load_default_memory()
        assert m.is_empty

    def test_load_default_memory_is_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from aegis.judge import case_memory as _cm
        path = tmp_path / "mem.npz"
        _toy_memory(dim=4, n=2).save(path)
        monkeypatch.setattr(_cm, "DEFAULT_CASE_MEMORY_PATH", path)
        reset_memory_cache()
        a = load_default_memory()
        b = load_default_memory()
        # Cache hit returns the same object.
        assert a is b


# ─────────────────────────────────────────────────────────────────────
# RAG block builder integration
# ─────────────────────────────────────────────────────────────────────
class TestBuildRAGBlock:
    """Exercises ``aegis.judge.local_phi._build_case_memory_block`` —
    the case-memory leg of the RAG block. The combined ``_build_rag_block``
    composes this with the corpus retrieval path (PR 2 onwards); the
    case-memory path is what these tests pin down."""

    def test_returns_empty_when_provider_not_bge_local(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")
        from aegis.judge.local_phi import _build_case_memory_block
        # atv None / dummy provider: must return ""
        assert _build_case_memory_block(None) == ""

    def test_returns_empty_when_atv_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
        from aegis.judge.local_phi import _build_case_memory_block
        assert _build_case_memory_block(None) == ""

    def test_returns_empty_when_memory_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
        from aegis.judge import case_memory as _cm
        monkeypatch.setattr(_cm, "DEFAULT_CASE_MEMORY_PATH", tmp_path / "ghost.npz")
        reset_memory_cache()
        from aegis.judge.local_phi import _build_case_memory_block
        # Real ATV but empty memory: ""
        atv = np.zeros(2080, dtype=np.float32)
        assert _build_case_memory_block(atv) == ""

    def test_renders_block_when_memory_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
        from aegis.judge import case_memory as _cm
        path = tmp_path / "mem.npz"
        # Build a 768-D toy memory matching the agent_state slice.
        rng = np.random.default_rng(0)
        embs = np.stack([
            _unit(rng.standard_normal(768).astype(np.float32))
            for _ in range(5)
        ])
        CaseMemory(
            embeddings=embs,
            texts=np.array([f"case {i}" for i in range(5)], dtype=object),
            labels=np.array(["BLOCK"] * 5, dtype=object),
            reasons=np.array([f"r{i}" for i in range(5)], dtype=object),
        ).save(path)
        monkeypatch.setattr(_cm, "DEFAULT_CASE_MEMORY_PATH", path)
        reset_memory_cache()

        # ATV with a non-zero agent_state slice that overlaps memory.
        atv = np.zeros(2080, dtype=np.float32)
        atv[:768] = embs[2]    # exact match for stored case 2

        from aegis.judge.local_phi import _build_rag_block
        block = _build_rag_block(atv, None, "summary")
        assert "Similar past cases" in block
        assert "case 2" in block
