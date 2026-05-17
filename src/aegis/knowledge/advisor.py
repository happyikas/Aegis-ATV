"""Bridge between the knowledge layer and sLLM advisors.

v0.5.15 shipped the knowledge wiki ground floor.
v0.5.16 (this module) is the thin adapter that turns
``aid`` → "agent's wiki context as a prompt-ready markdown
block", which the advisors splice into their LLM prompts.

The contract is:

* ``knowledge_context_for_advisor(aid)`` returns either a
  prompt-ready markdown string, or ``None`` if no knowledge
  exists for that agent (caller should fall back to the
  prompt-without-context path).
* The function **never raises** — advisors run on the hot path
  and an exception here would cascade into a tool-call failure.
* A small mtime-keyed cache amortises the JSON parsing cost
  across consecutive calls, since the wiki only rebuilds when
  the operator runs ``aegis knowledge build``.

Opt-in is via the env flag ``AEGIS_ADVISOR_USE_KNOWLEDGE=1``
(single switch covers both TripleAxisAdvisor and ActionAdvice).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aegis.knowledge.render import render_advisor_context
from aegis.knowledge.retrieve import get_entries_for_agent
from aegis.knowledge.schema import EntryKind, KnowledgeEntry
from aegis.knowledge.store import index_path, knowledge_dir

# ──────────────────────────────────────────────────────────────────
# Env flag
# ──────────────────────────────────────────────────────────────────


def advisor_knowledge_enabled() -> bool:
    """Return True iff the env flag is set.

    Default off — preserves v0.5.15 byte-identical advisor
    behaviour for existing deployments. Operators opt in via
    ``AEGIS_ADVISOR_USE_KNOWLEDGE=1``."""
    raw = os.environ.get("AEGIS_ADVISOR_USE_KNOWLEDGE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────────────────────────
# Cache — index-mtime keyed
# ──────────────────────────────────────────────────────────────────

# (aid, knowledge_dir, index_mtime_ns) -> rendered context
_CACHE: dict[tuple[str, str, int], str | None] = {}


def clear_advisor_cache() -> None:
    """Clear the in-memory context cache. Tests call this so a
    fresh wiki on disk isn't masked by a stale cached entry."""
    _CACHE.clear()


def _index_mtime_ns(root: Path) -> int:
    """Return the index file's mtime in nanoseconds. ``0`` when
    the index doesn't exist — that's a valid cache key (means
    "no wiki built yet") and forces a re-check next time."""
    try:
        return index_path(root).stat().st_mtime_ns
    except (OSError, FileNotFoundError):
        return 0


# ──────────────────────────────────────────────────────────────────
# Public entry — aid → prompt-ready context
# ──────────────────────────────────────────────────────────────────


_INTRO: str = (
    "Below is structured wiki-style knowledge about this agent's "
    "recent activity, derived from ContextMemory. Use it to "
    "ground your assessment: profile facts (cost, latency, "
    "stability) appear in each entry's infobox, and "
    "cross-references between entries use canonical `kind/slug` "
    "URIs."
)


def knowledge_context_for_advisor(
    aid: str | None,
    *,
    root: Path | None = None,
    max_related: int = 6,
    use_cache: bool = True,
) -> str | None:
    """Return prompt-ready markdown for ``aid``, or ``None``.

    Returns ``None`` (caller falls back to no-context prompt) when:

    * ``aid`` is empty or ``None``,
    * no wiki has been built for this agent yet,
    * the knowledge directory doesn't exist,
    * anything goes wrong while loading or rendering.

    Wrapped in a broad exception suppressor because the advisors
    sit on the firewall hot path — an exception here would turn
    into a blocked tool call. Logging is intentionally absent for
    the same reason; if operators want diagnostics they should
    invoke the underlying retrieval helpers directly.

    ``max_related=6`` is the v0.5.15 advisor budget — agent entry
    + 5 cross-refs (typically top 3 tools + top 2 patterns) fits
    in ~3,000 prompt tokens, comfortably below the 16k cap on
    smaller sLLMs.
    """
    if not aid:
        return None
    actual_root = root if root is not None else knowledge_dir()
    if use_cache:
        cache_key = (
            aid, str(actual_root), _index_mtime_ns(actual_root),
        )
        cached = _CACHE.get(cache_key)
        if cached is not None or cache_key in _CACHE:
            return cached
    try:
        entries = get_entries_for_agent(
            aid, root=actual_root, max_related=max_related,
        )
    except Exception:  # noqa: BLE001 — advisor hot path; never raise
        if use_cache:
            _CACHE[cache_key] = None
        return None
    if not entries:
        if use_cache:
            _CACHE[cache_key] = None
        return None
    try:
        md = render_advisor_context(entries, intro=_INTRO)
    except Exception:  # noqa: BLE001
        if use_cache:
            _CACHE[cache_key] = None
        return None
    if use_cache:
        _CACHE[cache_key] = md
    return md


# ──────────────────────────────────────────────────────────────────
# v0.5.18 — diagnostic measurement
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContextMetrics:
    """Quantitative diagnostics for what the wiki contributes to
    an advisor's prompt.

    These metrics let an operator answer the most important
    second-order questions about the wiki integration:

    * **Reach** — how many agents have a wiki entry? (``has_agent_entry``)
    * **Density** — how many facts does each call surface? Aggregated
      across the agent's entry + cross-refs.
    * **Cost** — what's the prompt budget impact? (``estimated_tokens``)

    The metrics are intentionally *structural* — they count the
    facts the wiki contributes, not the LLM's downstream quality
    (which depends on the model and is measured separately)."""

    aid: str
    """Agent id this measurement is scoped to."""

    n_entries: int = 0
    """Total wiki entries that would be included in the advisor
    prompt for this aid (agent entry + cross-refs)."""

    n_agent_entries: int = 0
    """Number of AGENT entries in the bundle (0 or 1 in practice)."""

    n_tool_entries: int = 0
    """Number of TOOL cross-refs in the bundle."""

    n_pattern_entries: int = 0
    """Number of PATTERN cross-refs in the bundle."""

    n_infobox_fields: int = 0
    """Total key-value facts across all entries' infoboxes. This
    is the structured-fact density — the part LLMs parse most
    reliably."""

    n_cross_refs: int = 0
    """Total ``related[]`` entry-id URIs across all entries.
    Higher = richer linked-knowledge graph."""

    n_tags: int = 0
    """Total semantic tags across all entries."""

    n_observations: int = 0
    """Sum of ``n_observations`` across all entries — the total
    raw-event count that backs this wiki context."""

    markdown_chars: int = 0
    """Character count of the rendered prompt block."""

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate: ``chars / 4``. Good enough for
        prompt-budget planning; exact tokenisation depends on the
        model's BPE."""
        return self.markdown_chars // 4

    @property
    def has_agent_entry(self) -> bool:
        return self.n_agent_entries > 0


def _entries_to_metrics(
    aid: str,
    entries: list[KnowledgeEntry],
    markdown: str,
) -> ContextMetrics:
    """Aggregate a fetched entry bundle into a ``ContextMetrics``."""
    n_agent = sum(1 for e in entries if e.kind == EntryKind.AGENT)
    n_tool = sum(1 for e in entries if e.kind == EntryKind.TOOL)
    n_pat = sum(1 for e in entries if e.kind == EntryKind.PATTERN)
    n_info = sum(len(e.infobox.fields) for e in entries)
    n_refs = sum(len(e.related) for e in entries)
    n_tags = sum(len(e.tags) for e in entries)
    n_obs = sum(e.n_observations for e in entries)
    return ContextMetrics(
        aid=aid,
        n_entries=len(entries),
        n_agent_entries=n_agent,
        n_tool_entries=n_tool,
        n_pattern_entries=n_pat,
        n_infobox_fields=n_info,
        n_cross_refs=n_refs,
        n_tags=n_tags,
        n_observations=n_obs,
        markdown_chars=len(markdown),
    )


def measure_context(
    aid: str | None,
    *,
    root: Path | None = None,
    max_related: int = 6,
) -> ContextMetrics | None:
    """Return diagnostic metrics for the wiki context an advisor
    would receive for this aid, or ``None`` if no wiki entry
    exists.

    Useful for:

    * **Operator dashboards** — show "agents with wiki coverage:
      19 / 20, median token cost: 1,847 / call".
    * **Demo runs** — quantify the "before/after wiki" delta.
    * **Sanity checks** — confirm the wiki is non-trivial before
      enabling ``AEGIS_ADVISOR_USE_KNOWLEDGE=1`` in production.

    Never raises (same hot-path safety as
    :func:`knowledge_context_for_advisor`)."""
    if not aid:
        return None
    actual_root = root if root is not None else knowledge_dir()
    try:
        entries = get_entries_for_agent(
            aid, root=actual_root, max_related=max_related,
        )
    except Exception:  # noqa: BLE001 — diagnostic; never raise
        return None
    if not entries:
        return None
    try:
        md = render_advisor_context(entries, intro=_INTRO)
    except Exception:  # noqa: BLE001
        md = ""
    return _entries_to_metrics(aid, entries, md)


__all__ = [
    "ContextMetrics",
    "advisor_knowledge_enabled",
    "clear_advisor_cache",
    "knowledge_context_for_advisor",
    "measure_context",
]
