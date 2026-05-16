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
from pathlib import Path

from aegis.knowledge.render import render_advisor_context
from aegis.knowledge.retrieve import get_entries_for_agent
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


__all__ = [
    "advisor_knowledge_enabled",
    "clear_advisor_cache",
    "knowledge_context_for_advisor",
]
