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
_NS_PER_DAY = 86_400 * 1_000_000_000


def _cache_dir() -> Path:
    base = os.environ.get("AEGIS_HOME") or str(Path.home() / ".aegis")
    return Path(base) / "rag_cache"


def _half_life_for(category: str) -> int:
    """Per-category half-life in days. 0 = no decay."""
    from aegis.config import settings
    if category == "rule":
        return int(settings.aegis_rag_decay_rule_days)
    if category == "playbook":
        return int(settings.aegis_rag_decay_playbook_days)
    if category == "baseline":
        return int(settings.aegis_rag_decay_baseline_days)
    return 0


def time_decay_factor(chunk: RagChunk, anchor_ts_ns: int) -> float:
    """Score multiplier in (0, 1] for ``chunk`` viewed at ``anchor_ts_ns``.

    Returns 1.0 (no decay) when:
      * ``chunk.created_at`` is unset (back-compat),
      * the per-category half-life is 0 (rules by default),
      * ``anchor_ts_ns`` is at or before ``created_at`` (future-dated
        chunks treated as fresh — relevant for replay anchors).

    Otherwise returns ``0.5 ** (age_days / half_life_days)``. The
    multiplier reaches ~0.5 at one half-life and ~0.25 at two.
    """
    if chunk.created_at is None:
        return 1.0
    half_life_days = _half_life_for(chunk.category)
    if half_life_days <= 0:
        return 1.0
    from aegis.judge.rag_corpus import _parse_iso_to_ns
    created_ns = _parse_iso_to_ns(chunk.created_at, field_name="created_at")
    if created_ns is None or anchor_ts_ns <= created_ns:
        return 1.0
    age_days = (anchor_ts_ns - created_ns) / _NS_PER_DAY
    return float(0.5 ** (age_days / half_life_days))


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
        *, anchor_ts_ns: int | None = None,
        apply_decay: bool = True,
    ) -> list[tuple[RagChunk, float]]:
        """Return the top-k chunks by cosine similarity to ``query``.

        When ``anchor_ts_ns`` is supplied, chunks whose validity window
        does not cover that timestamp are skipped (PR ② of temporal
        RAG), and chunks with a ``created_at`` are re-ranked by
        :func:`time_decay_factor` against the per-category half-life
        from settings (PR ③). The walk continues down the post-decay
        score order until k valid chunks have been collected (or the
        corpus is exhausted), so callers always get the best k chunks
        available at that anchor time rather than a possibly-empty
        filtered slice. Set ``apply_decay=False`` to keep cosine
        scores untouched (used by tests + tools that want pure
        similarity).
        """
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

        if anchor_ts_ns is None:
            top = np.argsort(-scores)[: max(k, 0)]
            return [
                (self.corpus.chunks[int(i)], float(scores[int(i)])) for i in top
            ]

        # Apply per-chunk time-decay multiplier when requested + when
        # the chunk carries a created_at. Rules without configured
        # half-life keep their cosine score unchanged.
        if apply_decay:
            decayed = scores.copy()
            for i, chunk in enumerate(self.corpus.chunks):
                factor = time_decay_factor(chunk, anchor_ts_ns)
                decayed[i] = scores[i] * factor
            scores = decayed

        # Validity-aware top-k: walk argsort, skip invalid chunks.
        order = np.argsort(-scores)
        out: list[tuple[RagChunk, float]] = []
        kk = max(k, 0)
        for i in order:
            idx = int(i)
            chunk = self.corpus.chunks[idx]
            if chunk.is_valid_at(anchor_ts_ns):
                out.append((chunk, float(scores[idx])))
                if len(out) >= kk:
                    break
        return out


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
    anchor_ts_ns: int | None = None,
    apply_decay: bool = True,
) -> list[tuple[RagChunk, float]]:
    """Retrieve top-k chunks for ``query_text``.

    If ``index`` is omitted, uses :func:`build_default_index`. If
    ``provider`` is omitted, uses the active provider for query
    embedding too. When ``anchor_ts_ns`` is supplied, the result is
    filtered to chunks valid at that timestamp (PR ②) and re-ranked
    by per-category time-decay (PR ③). ``None`` keeps the historical
    behaviour (no time filter, no decay).
    """
    idx = index if index is not None else build_default_index()
    if idx.is_empty:
        return []
    if provider is None:
        from aegis.atv.embeddings import get_provider
        provider = get_provider()
    q_vec = _embed_text(query_text, provider, idx.dim)
    return idx.search(
        q_vec, k=k, anchor_ts_ns=anchor_ts_ns, apply_decay=apply_decay,
    )


def retrieve_block(
    query_text: str,
    *,
    k: int | None = None,
    max_chars: int | None = None,
    index: RagIndex | None = None,
    provider: EmbeddingProvider | None = None,
    anchor_ts_ns: int | None = None,
) -> str:
    """Retrieve top-k chunks and render them into a prompt block.

    Defaults for ``k`` / ``max_chars`` come from
    ``aegis.config.settings`` (``aegis_rag_top_k`` / ``aegis_rag_max_chars``).
    When ``aegis_rag_enabled`` is False the function returns ``""``
    without doing any work.

    ``anchor_ts_ns`` semantics (PR ②):

    * ``None`` — anchor at ``time.time_ns()`` (live-judge default).
      Chunks whose validity window has expired are silently filtered
      out before top-k. New chunks scheduled for the future are also
      skipped. This is what production traffic should see.
    * any int — anchor at that exact timestamp. Used by audit replay
      so the judge sees the corpus state *as it was at the time of
      the recorded incident*, not the current state.

    Returns ``""`` if retrieval fails for any reason. The judge prompt
    builder relies on this fail-soft contract.
    """
    try:
        import time as _time

        from aegis.config import settings
        if not settings.aegis_rag_enabled:
            return ""
        effective_k = k if k is not None else settings.aegis_rag_top_k
        effective_max = (
            max_chars if max_chars is not None else settings.aegis_rag_max_chars
        )
        effective_anchor = (
            anchor_ts_ns if anchor_ts_ns is not None else _time.time_ns()
        )
        hits = retrieve(
            query_text, k=effective_k, index=index, provider=provider,
            anchor_ts_ns=effective_anchor,
        )
    except Exception:  # noqa: BLE001 — RAG must never block the judge
        return ""
    if not hits:
        return ""
    chunks = [c for c, _score in hits]
    return render_chunks_for_prompt(chunks, max_chars=effective_max)


def retrieve_for_audit_record(
    audit_rec: dict[str, object],
    query_text: str,
    *,
    k: int | None = None,
    index: RagIndex | None = None,
    provider: EmbeddingProvider | None = None,
) -> list[tuple[RagChunk, float]]:
    """Audit-replay convenience: anchor retrieval at the record's ``ts_ns``.

    Use when re-running a saved audit JSONL line through the judge —
    the corpus view will be filtered to chunks effective at the
    incident time, not the current time. If the record is missing
    ``ts_ns`` (older audits), falls back to live retrieval (no time
    filter). ``index`` and ``provider`` are pass-throughs for tests
    and bespoke flows; production code can leave both as ``None`` to
    use the default index built from the active embedding provider.
    """
    ts_raw = audit_rec.get("ts_ns")
    anchor: int | None = int(ts_raw) if isinstance(ts_raw, (int, float)) else None
    from aegis.config import settings
    effective_k = k if k is not None else settings.aegis_rag_top_k
    return retrieve(
        query_text, k=effective_k, anchor_ts_ns=anchor,
        index=index, provider=provider,
    )


__all__: tuple[str, ...] = (
    "RagIndex",
    "build_index",
    "build_default_index",
    "reset_index_cache",
    "retrieve",
    "retrieve_block",
    "retrieve_for_audit_record",
    "time_decay_factor",
)
