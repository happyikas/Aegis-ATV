"""LLM-ready rendering for knowledge entries (v0.5.15).

Two output paths:

* :func:`render_entry_markdown` — a single entry as a markdown
  document. Used by ``aegis knowledge show`` and as the building
  block of the advisor prompt.

* :func:`render_advisor_context` — a multi-entry composition for
  the sLLM advisor. Takes a list of entries (typically an agent
  entry + its top tools + patterns via :mod:`retrieve`) and emits
  a single document with horizontal-rule separators and a
  consistent header. The advisor passes this as a system /
  context block in its prompt.

Design choices:

* **Markdown over JSON.** LLMs parse markdown more reliably than
  raw JSON, especially when the prompt is large. We keep JSON
  for the store and machine-to-machine paths, but the LLM-facing
  surface is markdown.
* **InfoBox → table.** LLMs extract from markdown tables with
  near-perfect accuracy; from prose with much higher error.
  Every entry's headline facts go in the table at the top.
* **Footer with confidence.** The renderer always appends a
  footer with sample-size + confidence so the LLM can weigh
  the entry.
* **No HTML.** Pure CommonMark. Reliable across every model and
  every viewer.

Token-budget guidance: each entry renders to ~300-600 tokens.
An advisor context with 8 entries lands around 4-5k tokens, which
fits comfortably even in tight 16k-window models.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

from aegis.knowledge.schema import KnowledgeEntry


def _fmt_infobox(entry: KnowledgeEntry) -> str:
    """Render the InfoBox as a markdown table. Returns an empty
    string when the box is empty (so the renderer doesn't emit a
    bare table header)."""
    fields = entry.infobox.fields
    if not fields:
        return ""
    lines = [
        "| Field | Value |",
        "|-------|-------|",
    ]
    for k, v in fields.items():
        # Number formatting:
        # - ints get thousands separators
        # - floats round to 4-6 places depending on magnitude
        # - everything else stringifies as-is
        if isinstance(v, bool):
            rendered = "yes" if v else "no"
        elif isinstance(v, int):
            rendered = f"{v:,}"
        elif isinstance(v, float):
            rendered = f"{v:.6f}" if abs(v) < 0.01 else f"{v:.4f}"
        else:
            rendered = str(v)
        lines.append(f"| {k} | {rendered} |")
    return "\n".join(lines)


def _fmt_related(entry: KnowledgeEntry) -> str:
    """Render the cross-references list. Each link uses the
    canonical entry_id; the advisor follow-through code resolves
    these to entries via :mod:`retrieve`."""
    if not entry.related:
        return ""
    lines = ["## Related", ""]
    for ref in entry.related:
        lines.append(f"- `{ref}`")
    return "\n".join(lines)


def _fmt_footer(entry: KnowledgeEntry) -> str:
    """Footer with provenance + confidence. The advisor uses the
    confidence score to weigh facts (5-sample vs 5,000-sample
    entries should not be treated equally)."""
    confidence_pct = f"{entry.confidence * 100:.0f}%"
    return (
        f"---\n*Generated from {entry.n_observations:,} observations · "
        f"confidence {confidence_pct} · schema v{entry.schema_version}*"
    )


def render_entry_markdown(entry: KnowledgeEntry) -> str:
    """Render one entry as a standalone markdown document.

    Layout:
      # Title
      **Summary**: ...
      [InfoBox table]
      ## Section 1
      ...
      ## Related
      ---
      *Footer*

    The summary lands immediately after the title so even a
    severely-truncated read sees the most-important sentence."""
    parts: list[str] = [f"# {entry.title}"]
    if entry.summary:
        parts.append(f"**Summary**: {entry.summary}")
    if entry.tags:
        parts.append(
            "**Tags**: " + ", ".join(f"`{t}`" for t in entry.tags)
        )
    infobox = _fmt_infobox(entry)
    if infobox:
        parts.append("## Quick facts")
        parts.append(infobox)
    for section in entry.sections:
        if not section.body:
            continue
        parts.append(f"## {section.heading}")
        parts.append(section.body)
    related = _fmt_related(entry)
    if related:
        parts.append(related)
    parts.append(_fmt_footer(entry))
    return "\n\n".join(parts).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────────
# Advisor context — multi-entry composition
# ──────────────────────────────────────────────────────────────────


def render_advisor_context(
    entries: Iterable[KnowledgeEntry],
    *,
    intro: str = "",
) -> str:
    """Compose multiple entries into one prompt-ready document.

    The output starts with an optional intro paragraph the
    advisor controls ("the following knowledge is about agent
    foo's recent activity, use it to..."), then each entry
    separated by an ``---`` rule so the LLM can clearly identify
    entry boundaries.

    Suggested intro pattern::

        intro = (
            "Below is structured knowledge about the agent's recent "
            "activity. Use it to assess token efficiency, cache "
            "performance, and workflow stability. Cross-references "
            "between entries use `kind/slug` URIs."
        )
    """
    entry_list = list(entries)
    if not entry_list:
        return (
            "# Knowledge context\n\n"
            "_(no entries available — try `aegis knowledge build` to "
            "populate the wiki from ContextMemory)_\n"
        )
    parts: list[str] = []
    if intro:
        parts.append(intro)
    generated_at = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(
        f"# Aegis ContextMemory Knowledge — {len(entry_list)} entries"
    )
    parts.append(f"*Compiled {generated_at} for sLLM advisor consumption.*")
    for entry in entry_list:
        parts.append("---")
        parts.append(render_entry_markdown(entry).rstrip())
    return "\n\n".join(parts).rstrip() + "\n"


__all__ = [
    "render_advisor_context",
    "render_entry_markdown",
]
