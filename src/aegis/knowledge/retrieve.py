"""Retrieval helpers for the ContextMemory knowledge layer.

The sLLM advisor doesn't load the whole knowledge base — it
fetches a relevant *subset* of entries scoped to the question
at hand. This module provides three retrieval modes:

1. :func:`get_entry` — fetch one entry by ID (e.g. ``agent/foo``).
2. :func:`get_entries_for_agent` — fetch an agent's entry plus
   the entries it cross-references (top tools + patterns). The
   bread-and-butter pre-pop for an advisor prompt.
3. :func:`search_by_kind_or_tag` — enumerate the catalog filtered
   by kind or tag. For ``aegis knowledge list`` and exploratory
   advisor queries.

The implementation is deliberately simple — no vector embedding,
no LLM-based search. v0.5.15 ships the structural retrieval
(by entry_id, by tag); semantic / embedding-based retrieval is
a v0.6 candidate.

The retrieval is **defensive everywhere**: a missing or stale
cross-reference (e.g. a tool entry references a pattern that no
longer exists) is silently skipped, not raised. The advisor
should never crash because the knowledge base is in a partially-
built state.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from aegis.knowledge.schema import EntryKind, KnowledgeEntry
from aegis.knowledge.store import (
    IndexEntry,
    load_entry,
    load_index,
)


def get_entry(
    entry_id: str,
    *,
    root: Path | None = None,
) -> KnowledgeEntry | None:
    """Fetch one entry by ID. ``None`` if absent."""
    return load_entry(entry_id, root=root)


def get_entries_by_id(
    entry_ids: Iterable[str],
    *,
    root: Path | None = None,
) -> list[KnowledgeEntry]:
    """Fetch multiple entries by ID. Missing entries are silently
    dropped; the caller gets only the entries that exist.

    Order matches the input ``entry_ids`` iteration order — the
    advisor relies on this so the most-relevant entry (typically
    the agent's own entry) comes first in the rendered context."""
    out: list[KnowledgeEntry] = []
    for entry_id in entry_ids:
        entry = load_entry(entry_id, root=root)
        if entry is not None:
            out.append(entry)
    return out


def get_entries_for_agent(
    aid: str,
    *,
    root: Path | None = None,
    max_related: int = 8,
) -> list[KnowledgeEntry]:
    """Assemble the typical advisor context for an agent.

    Returns the agent's own entry plus up to ``max_related``
    cross-referenced entries (tools, patterns). The agent entry
    is always first; the related entries follow in the order
    listed in :attr:`KnowledgeEntry.related` (which the builder
    orders by salience).

    Returns an empty list if the agent has no entry yet — the
    caller (advisor) should handle this as "not enough data
    for advice" rather than treating it as an error."""
    from aegis.knowledge.schema import make_entry_id
    primary_id = make_entry_id(EntryKind.AGENT, aid)
    primary = load_entry(primary_id, root=root)
    if primary is None:
        return []
    fetched_ids = {primary.entry_id}
    out: list[KnowledgeEntry] = [primary]
    for ref in primary.related[:max_related]:
        if ref in fetched_ids:
            continue
        entry = load_entry(ref, root=root)
        if entry is not None:
            out.append(entry)
            fetched_ids.add(ref)
    return out


def search_by_kind_or_tag(
    *,
    kind: EntryKind | None = None,
    tag: str | None = None,
    root: Path | None = None,
    limit: int | None = None,
) -> list[IndexEntry]:
    """Enumerate the catalog filtered by ``kind`` and/or ``tag``.

    Both filters are optional and stack:
      kind=AGENT                → all agent entries
      tag="high-cost"           → entries tagged high-cost
      kind=TOOL, tag="unstable" → unstable tools only
      neither                   → entire catalog

    Returns :class:`IndexEntry` objects (metadata only) for speed.
    Use :func:`get_entry` to load full bodies when needed.
    Sorted by ``n_observations`` descending — most-evidence
    entries first."""
    rows = load_index(root=root)
    if kind is not None:
        rows = [r for r in rows if r.kind == kind]
    if tag is not None:
        rows = [r for r in rows if tag in r.tags]
    rows.sort(key=lambda r: -r.n_observations)
    if limit is not None and limit > 0:
        rows = rows[:limit]
    return rows


__all__ = [
    "get_entries_by_id",
    "get_entries_for_agent",
    "get_entry",
    "search_by_kind_or_tag",
]
