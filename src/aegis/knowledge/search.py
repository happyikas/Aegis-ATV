"""Semantic search over knowledge entries (v0.5.21).

v0.5.15 + v0.5.20 ship a structural retrieval surface
(:func:`aegis.knowledge.get_entries_for_agent` follows
``related[]`` cross-references) plus a filter-based catalog
walker (:func:`search_by_kind_or_tag`). Both require the caller
to know the right ``aid`` / ``kind`` / ``tag`` in advance.

v0.5.21 adds **content-based retrieval**: given a free-text query
like ``"cost-divergence on Bash"`` or ``"agents with frequent
BLOCKs"``, return the entries whose text best matches.

### Why TF-IDF rather than an LLM embedding

* **No model dependency.** TF-IDF runs in pure Python with the
  standard library — no scipy, no sklearn, no embedding service.
  Aegis has hot-path safety contracts that forbid heavy deps in
  the firewall pipeline; the same contract applies to operator
  diagnostics.
* **Deterministic + reproducible.** Same wiki + same query → same
  ranking. Useful for tests, audits, and CI.
* **Sufficient for wiki-scale corpora.** A 30-day wiki has
  ~50–200 entries; cosine similarity over TF-IDF vectors is
  more than precise enough to surface the relevant 5-10.
* **Composable with future embedding search.** A v0.6 PR can add
  a parallel cosine-on-ATV-embedding retriever and let the
  operator pick per query.

### Index lifecycle

* Built lazily on first search call from the on-disk wiki.
* Mtime-keyed cache same as
  :func:`aegis.knowledge.knowledge_context_for_advisor`; an
  ``aegis knowledge build`` invalidates by bumping the index
  file's mtime.
* No on-disk persistence — the index is small enough that
  rebuilding on the next process start is cheap.

### Ranking

Cosine similarity between query TF-IDF vector and entry TF-IDF
vector. Documents that score ≤ 0 (no shared terms) are dropped
silently — better an empty result than padding with irrelevant
entries."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from aegis.knowledge.schema import KnowledgeEntry
from aegis.knowledge.store import (
    IndexEntry,
    entry_path,
    index_path,
    knowledge_dir,
    load_entry,
    load_index,
)

# ──────────────────────────────────────────────────────────────────
# Tokeniser
# ──────────────────────────────────────────────────────────────────

# Tokens are runs of word characters, including ``:`` (so
# ``loop:Bash`` stays one token) and ``-`` (so ``rule-fired`` stays
# one token). Numbers are dropped — they're high-cardinality noise
# in this corpus (timestamps, counts).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9:_-]*")

_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on",
    "for", "is", "are", "was", "were", "by", "with", "at",
    "from", "as", "this", "that", "these", "those", "it",
    "its", "be", "has", "have", "had",
})


def _tokenise(text: str) -> list[str]:
    """Lower-case word tokens with stopwords filtered.

    Single-character tokens (``a``, ``i``) are also dropped — the
    corpus has none that carry meaning."""
    if not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if len(tok) <= 1:
            continue
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def _entry_document(entry: KnowledgeEntry) -> str:
    """Join an entry's searchable surface into one text blob.

    Order matters because TF gets weighted by occurrence count: we
    emit title + summary multiple times to boost their salience
    (they're the most signal-dense parts of the entry)."""
    parts: list[str] = [entry.title] * 3
    parts.append(entry.summary)
    parts.append(entry.summary)
    parts.extend(str(v) for v in entry.infobox.fields.values())
    parts.extend(str(k) for k in entry.infobox.fields)
    for section in entry.sections:
        parts.append(section.heading)
        parts.append(section.body)
    parts.extend(entry.tags)
    parts.extend(entry.related)
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────
# TF-IDF index
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _IndexedDoc:
    """One entry's TF-IDF vector. Frozen because the index is
    immutable once built — rebuilds replace, not mutate."""
    entry_id: str
    tf: dict[str, float]  # term-frequency (normalised by doc length)
    norm: float           # L2 norm of the TF-IDF vector


@dataclass(frozen=True)
class TfIdfIndex:
    """An immutable TF-IDF index over the knowledge corpus.

    Construction goes through :func:`build_tfidf_index` so callers
    don't have to materialise the IDF table themselves."""

    documents: tuple[_IndexedDoc, ...]
    idf: dict[str, float]
    n_documents: int


def build_tfidf_index(entries: list[KnowledgeEntry]) -> TfIdfIndex:
    """Build a TF-IDF index from a list of entries.

    Two-pass:
      1. Tokenise each entry, accumulate document-frequency.
      2. Compute IDF, derive each doc's L2 norm.

    O(N) in tokens both passes. Pure Python, no external deps."""
    n = len(entries)
    if n == 0:
        return TfIdfIndex(documents=(), idf={}, n_documents=0)

    # Pass 1 — token frequencies per doc + document frequencies.
    docs_tf: list[tuple[str, Counter[str]]] = []
    df: Counter[str] = Counter()
    for entry in entries:
        tokens = _tokenise(_entry_document(entry))
        tf = Counter(tokens)
        docs_tf.append((entry.entry_id, tf))
        for term in tf:
            df[term] += 1

    # IDF — smoothed log form: ``ln((1 + n) / (1 + df)) + 1``.
    idf: dict[str, float] = {
        t: math.log((1.0 + n) / (1.0 + cnt)) + 1.0
        for t, cnt in df.items()
    }

    # Pass 2 — TF-IDF vectors + L2 norms.
    indexed: list[_IndexedDoc] = []
    for entry_id, tf in docs_tf:
        if not tf:
            indexed.append(_IndexedDoc(entry_id=entry_id, tf={}, norm=0.0))
            continue
        total = sum(tf.values())
        # TF normalised by doc length, then multiplied by IDF.
        tfidf = {
            t: (c / total) * idf.get(t, 0.0)
            for t, c in tf.items()
        }
        norm = math.sqrt(sum(v * v for v in tfidf.values()))
        indexed.append(_IndexedDoc(
            entry_id=entry_id,
            tf=tfidf,
            norm=norm,
        ))
    return TfIdfIndex(
        documents=tuple(indexed),
        idf=idf,
        n_documents=n,
    )


def _query_vector(query: str, idf: dict[str, float]) -> dict[str, float]:
    """Build a TF-IDF vector for the query."""
    tokens = _tokenise(query)
    if not tokens:
        return {}
    tf = Counter(tokens)
    total = sum(tf.values())
    return {
        t: (c / total) * idf.get(t, 1.0)  # unseen terms get IDF=1
        for t, c in tf.items()
    }


def _cosine(a: dict[str, float], a_norm: float, b: dict[str, float]) -> float:
    """Cosine similarity between two sparse TF-IDF vectors.

    Returns 0.0 when either vector is zero. The query norm is
    re-computed inline since the same query isn't reused across
    indexed docs."""
    if a_norm == 0.0 or not b:
        return 0.0
    b_norm_sq = sum(v * v for v in b.values())
    if b_norm_sq == 0.0:
        return 0.0
    # Iterate the smaller dict for speed.
    if len(a) > len(b):
        smaller, larger = b, a
    else:
        smaller, larger = a, b
    dot = sum(v * larger.get(k, 0.0) for k, v in smaller.items())
    return dot / (a_norm * math.sqrt(b_norm_sq))


# ──────────────────────────────────────────────────────────────────
# Cached corpus + search
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchHit:
    """One result of :func:`search_entries`. The entry itself is
    lazy-loaded by the CLI on demand so a large catalog doesn't
    pay full-load cost per search."""

    entry_id: str
    kind: str
    title: str
    summary: str
    score: float


# In-memory cache: (knowledge_dir, index_mtime_ns) -> (index, by_id)
_CACHE: dict[
    tuple[str, int],
    tuple[TfIdfIndex, dict[str, IndexEntry]],
] = {}


def clear_search_cache() -> None:
    """Clear the in-memory TF-IDF cache. Tests + the CLI's
    ``--rebuild`` flag call this; production never needs to since
    the cache is mtime-keyed."""
    _CACHE.clear()


def _index_mtime_ns(root: Path) -> int:
    try:
        return index_path(root).stat().st_mtime_ns
    except (OSError, FileNotFoundError):
        return 0


def _load_corpus(root: Path) -> tuple[TfIdfIndex, dict[str, IndexEntry]]:
    """Load all entries from disk and build the TF-IDF index.

    Cached by `(root, index_mtime)` so a static wiki is built once
    per process. The cache value is `(index, by_id)` where `by_id`
    maps entry_id → IndexEntry for cheap metadata lookups on hit."""
    key = (str(root), _index_mtime_ns(root))
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

    index = build_tfidf_index(entries)
    _CACHE[key] = (index, by_id)
    return index, by_id


def search_entries(
    query: str,
    *,
    root: Path | None = None,
    k: int = 10,
    min_score: float = 0.0,
) -> list[SearchHit]:
    """Return the top-``k`` entries matching the free-text ``query``.

    ``root`` defaults to ``knowledge_dir()`` (override for tests).
    ``min_score`` filters near-zero matches — at the default 0.0
    only exactly-zero cosine matches are dropped (the typical case
    of "no shared terms"), so increase it to ~0.05 if you want
    only "clearly relevant" results.

    Returns an empty list on:
      * empty query
      * missing / empty wiki
      * query token set disjoint from the corpus vocabulary

    Never raises."""
    if not query or not query.strip():
        return []
    actual_root = root if root is not None else knowledge_dir()
    try:
        index, by_id = _load_corpus(actual_root)
    except Exception:  # noqa: BLE001 — diagnostic; never raise
        return []
    if index.n_documents == 0:
        return []

    qvec = _query_vector(query, index.idf)
    if not qvec:
        return []
    q_norm = math.sqrt(sum(v * v for v in qvec.values()))
    if q_norm == 0.0:
        return []

    scored: list[tuple[float, _IndexedDoc]] = []
    for doc in index.documents:
        if doc.norm == 0.0:
            continue
        score = _cosine(qvec, q_norm, doc.tf)
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
    "SearchHit",
    "TfIdfIndex",
    "build_tfidf_index",
    "clear_search_cache",
    "search_entries",
]
