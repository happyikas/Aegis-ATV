"""Embedding-based retrieval over the RAG corpus (PR 2/3).

Builds dense embeddings for every chunk in
``policies/rag_corpus/`` and retrieves the top-k by cosine similarity
against a query string.

Design notes
------------

* **Provider-agnostic.** Uses ``aegis.atv.embeddings.get_provider()``,
  so the retrieval works with ``dummy`` (SHA3 — deterministic, useful
  for tests), ``openai``, and ``bge-local``. ``dummy`` will produce
  meaningless cosines but the wiring is identical.

* **Cache to disk.** Embedding 38 corpus chunks each invocation is
  wasteful for ``openai`` / ``bge-local``. We hash the corpus content +
  provider + dim into a cache key and cache to
  ``~/.aegis/rag_cache/<key>.npz`` (created on first use).

* **Loads lazily.** Calling :func:`build_default_index` is cheap when
  cache is warm. Cold path runs the embedder once per chunk; this is
  bounded (corpus is human-curated, ~50 chunks).

* **Fail-soft.** Any retrieval error returns an empty result. The
  judge prompt builder degrades to no-RAG rather than crashing the
  firewall.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from aegis.judge.rag_corpus import (
    RagChunk,
    RagCorpus,
    load_default_corpus,
    render_chunks_for_prompt,
)

if TYPE_CHECKING:
    from aegis.atv.embeddings import EmbeddingProvider


_DEFAULT_DIM = 768


def _cache_dir() -> Path:
    base = os.environ.get("AEGIS_HOME") or str(Path.home() / ".aegis")
    return Path(base) / "rag_cache"


def _corpus_fingerprint(corpus: RagCorpus, provider_name: str, dim: int) -> str:
    """Stable hash over (provider, dim, every chunk's id+content)."""
    h = hashlib.sha256()
    h.update(provider_name.encode("utf-8"))
    h.update(str(dim).encode("utf-8"))
    for c in corpus.chunks:
        h.update(b"\x00")
        h.update(c.id.encode("utf-8"))
        h.update(b"\x01")
        h.update(c.content.encode("utf-8"))
    return h.hexdigest()[:16]


def _embed_text(text: str, provider: EmbeddingProvider, dim: int) -> np.ndarray:
    vec = np.asarray(provider.embed(text, dim), dtype=np.float32)
    if vec.size == 0:
        return np.zeros((dim,), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec


def _embed_corpus(
    corpus: RagCorpus, provider: EmbeddingProvider, dim: int,
) -> np.ndarray:
    if corpus.is_empty:
        return np.zeros((0, dim), dtype=np.float32)
    rows = [
        _embed_text(_chunk_query_text(c), provider, dim)
        for c in corpus.chunks
    ]
    return np.stack(rows, axis=0)


def _chunk_query_text(chunk: RagChunk) -> str:
    """The text we embed for retrieval — title + tags + content prefix.

    Keeping this distinct from :py:meth:`RagChunk.render_for_prompt` so
    the retrieval text can prioritize keyword recall while the prompt
    text optimizes for the model's reading.
    """
    tags = " ".join(chunk.tags)
    return f"{chunk.title} {tags} {chunk.content}"[:1500]


def _load_cached(
    cache_path: Path, expected_shape: tuple[int, int],
) -> np.ndarray | None:
    if not cache_path.is_file():
        return None
    try:
        with np.load(cache_path) as data:
            arr = data["embeddings"]
    except (OSError, KeyError, ValueError):
        return None
    if arr.shape != expected_shape:
        return None
    return np.asarray(arr, dtype=np.float32)


def _save_cached(cache_path: Path, embeddings: np.ndarray) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        np.savez(cache_path, embeddings=embeddings)


@dataclass(frozen=True)
class RagIndex:
    corpus: RagCorpus
    embeddings: np.ndarray = field(repr=False)
    provider_name: str
    dim: int

    @property
    def is_empty(self) -> bool:
        return self.corpus.is_empty or self.embeddings.shape[0] == 0

    def search(
        self, query: np.ndarray, k: int = 3,
    ) -> list[tuple[RagChunk, float]]:
        if self.is_empty:
            return []
        q = np.asarray(query, dtype=np.float32)
        if q.shape != (self.dim,):
            return []
        norm = float(np.linalg.norm(q))
        if norm == 0:
            return []
        q = q / norm
        scores = self.embeddings @ q
        top = np.argsort(-scores)[: max(k, 0)]
        return [(self.corpus.chunks[int(i)], float(scores[int(i)])) for i in top]


def build_index(
    corpus: RagCorpus,
    provider: EmbeddingProvider | None = None,
    *,
    dim: int = _DEFAULT_DIM,
    use_cache: bool = True,
) -> RagIndex:
    """Build an index. Reads/writes the on-disk cache when ``use_cache``."""
    if corpus.is_empty:
        return RagIndex(
            corpus=corpus,
            embeddings=np.zeros((0, dim), dtype=np.float32),
            provider_name="(empty)",
            dim=dim,
        )

    if provider is None:
        from aegis.atv.embeddings import get_provider
        provider = get_provider()

    provider_name = type(provider).__name__
    fp = _corpus_fingerprint(corpus, provider_name, dim)
    cache_path = _cache_dir() / f"corpus_{fp}.npz"

    expected_shape = (len(corpus.chunks), dim)
    embeddings: np.ndarray | None = None
    if use_cache:
        embeddings = _load_cached(cache_path, expected_shape)

    if embeddings is None:
        embeddings = _embed_corpus(corpus, provider, dim)
        if use_cache:
            _save_cached(cache_path, embeddings)

    return RagIndex(
        corpus=corpus,
        embeddings=embeddings,
        provider_name=provider_name,
        dim=dim,
    )


@lru_cache(maxsize=1)
def build_default_index() -> RagIndex:
    """Cached convenience wrapper for the default corpus + provider."""
    return build_index(load_default_corpus())


def reset_index_cache() -> None:
    """Test helper — clear the in-process index cache."""
    build_default_index.cache_clear()


def retrieve(
    query_text: str,
    *,
    k: int = 3,
    index: RagIndex | None = None,
    provider: EmbeddingProvider | None = None,
) -> list[tuple[RagChunk, float]]:
    """Retrieve top-k chunks for ``query_text``.

    If ``index`` is omitted, uses :func:`build_default_index`. If
    ``provider`` is omitted, uses the active provider for query
    embedding too.
    """
    idx = index if index is not None else build_default_index()
    if idx.is_empty:
        return []
    if provider is None:
        from aegis.atv.embeddings import get_provider
        provider = get_provider()
    q_vec = _embed_text(query_text, provider, idx.dim)
    return idx.search(q_vec, k=k)


def retrieve_block(
    query_text: str,
    *,
    k: int | None = None,
    max_chars: int | None = None,
    index: RagIndex | None = None,
    provider: EmbeddingProvider | None = None,
) -> str:
    """Retrieve top-k chunks and render them into a prompt block.

    Defaults for ``k`` / ``max_chars`` come from
    ``aegis.config.settings`` (``aegis_rag_top_k`` / ``aegis_rag_max_chars``).
    When ``aegis_rag_enabled`` is False the function returns ``""``
    without doing any work.

    Returns ``""`` if retrieval fails for any reason. The judge prompt
    builder relies on this fail-soft contract.
    """
    try:
        from aegis.config import settings
        if not settings.aegis_rag_enabled:
            return ""
        effective_k = k if k is not None else settings.aegis_rag_top_k
        effective_max = (
            max_chars if max_chars is not None else settings.aegis_rag_max_chars
        )
        hits = retrieve(
            query_text, k=effective_k, index=index, provider=provider,
        )
    except Exception:  # noqa: BLE001 — RAG must never block the judge
        return ""
    if not hits:
        return ""
    chunks = [c for c, _score in hits]
    return render_chunks_for_prompt(chunks, max_chars=effective_max)


__all__: tuple[str, ...] = (
    "RagIndex",
    "build_index",
    "build_default_index",
    "reset_index_cache",
    "retrieve",
    "retrieve_block",
)
