"""v0.6.0 — embedding-based semantic search over wiki entries.

v0.5.21 shipped TF-IDF as the pragmatic v1 ranker: pure-Python,
no ML deps, deterministic, dependency-free. v0.6.0 adds a
**parallel ranker** that uses the existing
:mod:`aegis.atv.embeddings` provider chain (BGE-local / OpenAI /
deterministic dummy) so semantically similar entries rank close
even without lexical overlap with the query.

### Design choices

* **Single shared provider.** `get_provider()` from
  `aegis.atv.embeddings` is the same one the ATV synthesiser
  uses. No new model dependency, no separate config knob —
  `AEGIS_EMBEDDING_PROVIDER` controls both.
* **Parallel, not replacement.** The CLI gets a new `--engine`
  flag; default stays `tfidf` so existing operators see no
  change. `--engine embedding` switches to this module.
* **mtime-keyed cache.** Same cache idiom as the TF-IDF index:
  rebuild when the wiki's `index.json` mtime moves.
* **Defensive fallback.** If the provider raises or returns a
  zero vector, the ranker drops the entry from results rather
  than poisoning the ranking with NaN cosines.
* **Document = same blob as TF-IDF.** Title (×3 boost) + summary
  + infobox keys/values + section bodies + tags + related URIs.
  Keeps the two engines comparable on equivalent content.

### Why even the `dummy` provider is useful here

The dummy provider produces a deterministic SHA3-expanded
768-D vector. It's not semantic, but it provides:
* Reproducible test fixtures.
* A baseline against which BGE's improvement can be measured.
* Working code paths in CI environments that lack `llama-cpp`.

Real semantic search requires `AEGIS_EMBEDDING_PROVIDER=bge-local`
+ the GGUF model installed via `uv sync --extra local-llm`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from aegis.atv.embeddings import EmbeddingProvider, get_provider
from aegis.knowledge.schema import KnowledgeEntry
from aegis.knowledge.search import SearchHit, _entry_document
from aegis.knowledge.store import (
    IndexEntry,
    entry_path,
    index_path,
    knowledge_dir,
    load_entry,
    load_index,
)

# ──────────────────────────────────────────────────────────────────
# Index dataclass
# ──────────────────────────────────────────────────────────────────


_DEFAULT_EMBED_DIM: int = 768
"""BGE-base output dimension. The same dim is used for dummy +
OpenAI fallback so the cosine math doesn't have to special-case
shapes. Operators on bge-large can override via
:func:`build_embedding_index`'s ``dim`` kwarg."""


@dataclass(frozen=True)
class _IndexedEmbedding:
    """One entry's L2-normalised embedding. Frozen — same
    immutability discipline as the TF-IDF index."""

    entry_id: str
    vec: np.ndarray
    """L2-normalised embedding vector, shape (dim,)."""


@dataclass(frozen=True)
class EmbeddingIndex:
    """Mtime-keyed corpus index of L2-normalised embeddings."""

    documents: tuple[_IndexedEmbedding, ...]
    provider_name: str
    """Human-readable provider tag — e.g. ``bge-local`` or
    ``dummy``. Useful for the CLI to surface "your ranking
    used <provider>"."""
    dim: int


# ──────────────────────────────────────────────────────────────────
# Index construction
# ──────────────────────────────────────────────────────────────────


def _safe_embed(
    provider: EmbeddingProvider, text: str, dim: int,
) -> np.ndarray | None:
    """Embed one document, returning ``None`` on failure or zero-
    vector output (which would produce NaN cosines)."""
    if not text:
        return None
    try:
        vec = provider.embed(text, dim)
    except Exception:  # noqa: BLE001 — ranker; never raise
        return None
    if vec is None or vec.size == 0:
        return None
    norm = float(np.linalg.norm(vec))
    if norm <= 0.0 or not math.isfinite(norm):
        return None
    return (vec / norm).astype(np.float32)


def build_embedding_index(
    entries: list[KnowledgeEntry],
    *,
    provider: EmbeddingProvider | None = None,
    dim: int = _DEFAULT_EMBED_DIM,
) -> EmbeddingIndex:
    """Embed every entry's searchable text. Entries that fail
    to embed are silently dropped from the index."""
    actual_provider = provider if provider is not None else get_provider()
    documents: list[_IndexedEmbedding] = []
    for entry in entries:
        text = _entry_document(entry)
        vec = _safe_embed(actual_provider, text, dim)
        if vec is None:
            continue
        documents.append(_IndexedEmbedding(
            entry_id=entry.entry_id, vec=vec,
        ))
    return EmbeddingIndex(
        documents=tuple(documents),
        provider_name=type(actual_provider).__name__,
        dim=dim,
    )


# ──────────────────────────────────────────────────────────────────
# Mtime-keyed cache
# ──────────────────────────────────────────────────────────────────

# (knowledge_dir, index_mtime_ns, dim) -> (index, by_id)
_CACHE: dict[
    tuple[str, int, int],
    tuple[EmbeddingIndex, dict[str, IndexEntry]],
] = {}


def clear_embedding_cache() -> None:
    """Tests + the CLI `--rebuild` flag clear the cache so the
    next call rebuilds against the on-disk wiki."""
    _CACHE.clear()


def _index_mtime_ns(root: Path) -> int:
    try:
        return index_path(root).stat().st_mtime_ns
    except (OSError, FileNotFoundError):
        return 0


def _load_corpus(
    root: Path, dim: int,
) -> tuple[EmbeddingIndex, dict[str, IndexEntry]]:
    key = (str(root), _index_mtime_ns(root), dim)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    catalog = load_index(root=root)
    by_id: dict[str, IndexEntry] = {r.entry_id: r for r in catalog}

    entries: list[KnowledgeEntry] = []
    for row in catalog:
        path = entry_path(row.entry_id, root=root)
        if not path.exists():
            continue
        entry = load_entry(row.entry_id, root=root)
        if entry is not None:
            entries.append(entry)

    index = build_embedding_index(entries, dim=dim)
    _CACHE[key] = (index, by_id)
    return index, by_id


# ──────────────────────────────────────────────────────────────────
# Search
# ──────────────────────────────────────────────────────────────────


def search_entries_embedding(
    query: str,
    *,
    root: Path | None = None,
    k: int = 10,
    min_score: float = 0.0,
    dim: int = _DEFAULT_EMBED_DIM,
) -> list[SearchHit]:
    """Embedding-cosine ranking. Returns the top-``k`` entries.

    Same shape as :func:`aegis.knowledge.search.search_entries` so
    callers can swap engines transparently. Empty list on:
    empty query, missing wiki, query embed failure.

    Never raises."""
    if not query or not query.strip():
        return []
    actual_root = root if root is not None else knowledge_dir()
    try:
        index, by_id = _load_corpus(actual_root, dim)
    except Exception:  # noqa: BLE001
        return []
    if not index.documents:
        return []

    provider = get_provider()
    qvec = _safe_embed(provider, query, dim)
    if qvec is None:
        return []

    scored: list[tuple[float, _IndexedEmbedding]] = []
    for doc in index.documents:
        # Both vectors are L2-normalised, so cosine = dot product.
        score = float(np.dot(qvec, doc.vec))
        if not math.isfinite(score):
            continue
        if score <= min_score:
            continue
        scored.append((score, doc))

    scored.sort(key=lambda t: -t[0])
    hits: list[SearchHit] = []
    for score, doc in scored[:k]:
        meta = by_id.get(doc.entry_id)
        if meta is None:
            continue
        hits.append(SearchHit(
            entry_id=doc.entry_id,
            kind=meta.kind.value,
            title=meta.title,
            summary=meta.summary,
            score=score,
        ))
    return hits


__all__ = [
    "EmbeddingIndex",
    "build_embedding_index",
    "clear_embedding_cache",
    "search_entries_embedding",
]
