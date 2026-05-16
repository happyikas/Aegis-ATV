"""Schema for the ContextMemory knowledge layer (v0.5.15).

Until v0.5.14 the ``ContextMemory`` was an analytics-shaped
row-store: one JSONL line per ATV decision, optimised for
audit + replay but not for downstream LLM consumption. An sLLM
that wanted to give workflow advice had to scan thousands of
rows and reconstruct patterns itself.

v0.5.15 introduces a **derived semantic layer** on top of the
raw store — a wiki-shaped knowledge base where each entry is a
self-contained article about an *entity* (agent, tool, pattern)
rather than an event. The sLLM advisor can fetch a handful of
entries and have everything it needs to reason about agent
workflow: usage profile, stability profile, cost profile, with
cross-references to related entities.

The wiki shape was chosen specifically because LLMs parse it
well:

* **Lead section / summary** — always 1-2 sentences. Reliable
  even when the model truncates context.
* **Infobox** — structured key-value facts at the top.
  LLMs extract from infoboxes more reliably than from prose.
* **Sections** — ordered markdown headers + bodies. Conventional
  enough that the model can navigate without extra instructions.
* **Cross-references** — explicit ``related`` field with stable
  entry_id URIs. Lets the LLM follow links rather than hunt.
* **Tags + confidence + observation count** — metadata that lets
  the LLM weigh facts (5-sample vs 5,000-sample patterns are
  different stories).

The schema is **immutable + frozen** so a built entry can be
shared across threads / processes without copying. Re-building
on raw events produces a new entry rather than mutating the old
one — same atomic-write discipline as the autonomy trust table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

SCHEMA_VERSION: Final[int] = 1


# ──────────────────────────────────────────────────────────────────
# Entry kinds
# ──────────────────────────────────────────────────────────────────


class EntryKind(StrEnum):
    """The taxonomic kind of a knowledge entry.

    Stored as a string in JSON (``"agent"`` not ``0``) so the
    on-disk shape is human-readable and schema-version evolution
    can extend the enum without renumbering.

    Three kinds in v0.5.15. Future kinds reserved:
      SESSION   — one coherent stretch of work (multi-call)
      INCIDENT  — one BLOCK or notable outlier
      WORKFLOW  — a recurring multi-step procedure"""

    AGENT = "agent"
    TOOL = "tool"
    PATTERN = "pattern"


# ──────────────────────────────────────────────────────────────────
# InfoBox — structured key-value summary
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InfoBox:
    """Compact key-value pairs shown at the top of every entry.

    InfoBoxes are the **most parser-reliable** part of a wiki
    article — LLMs trained on Wikipedia-style content extract
    from infoboxes with much lower error than from prose. We
    use this for the headline numbers: counts, dates, totals,
    medians.

    Values are typed as ``str | int | float | bool`` so JSON
    serialisation is trivial; the renderer converts numerics to
    human-readable form (``1,247`` not ``1247``)."""

    fields: dict[str, str | int | float | bool] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────
# Section — one markdown chapter inside an entry
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """One markdown section.

    ``heading`` becomes a ``##`` header. ``body`` is the markdown
    content (already formatted — the renderer doesn't transform
    it, so the builder controls the prose).

    Sections are ordered (list, not dict) because the order
    matters for LLM consumption — the model gives more weight
    to earlier sections."""

    heading: str
    body: str


# ──────────────────────────────────────────────────────────────────
# KnowledgeEntry — one wiki article
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KnowledgeEntry:
    """A wiki-shaped article about one entity.

    Designed for ingestion by an sLLM advisor: every field is
    either compact (summary, infobox, tags) or skippable
    (sections — the model can read just the headings). The
    renderer composes these into a markdown document of typically
    ~300-600 tokens, small enough that 10-20 entries fit in a
    16k-context window.
    """

    # ── Identity ────────────────────────────────────────────────
    entry_id: str
    """Canonical URN. Format ``<kind>/<slug>`` — e.g. ``agent/foo``,
    ``tool/Bash``, ``pattern/loop:Bash``. Slugs are case-preserving
    but URL-safe (no whitespace). Used as the cross-reference
    target in :attr:`related`."""

    kind: EntryKind

    title: str
    """Display title — human-readable, not necessarily unique.
    The ``entry_id`` is the unique key."""

    # ── Lead section (always shown) ─────────────────────────────
    summary: str
    """1-2 sentence abstract. The renderer emits this immediately
    after the title so even truncated reads see it. Convention:
    state *what* the entity is, then *one* most-salient fact."""

    # ── Structured data ────────────────────────────────────────
    infobox: InfoBox = field(default_factory=InfoBox)
    """Headline key-value facts. Rendered as a markdown table."""

    sections: tuple[Section, ...] = field(default_factory=tuple)
    """Ordered detail sections. The builder is responsible for
    keeping each section compact (<200 tokens preferred)."""

    # ── Cross-references + tags ────────────────────────────────
    related: tuple[str, ...] = field(default_factory=tuple)
    """Other entry_ids this entry references. Ordered by salience
    (most-relevant first). The retrieval helper traverses this
    field to assemble a multi-entry context for the sLLM."""

    tags: tuple[str, ...] = field(default_factory=tuple)
    """Semantic tags for filter-based retrieval. Convention:
    short lowercase tokens. Examples: ``high-cost``, ``frequent``,
    ``unstable``, ``recent``."""

    # ── Provenance + confidence ────────────────────────────────
    ts_first_ns: int = 0
    """Nanosecond timestamp of the earliest observation that
    contributed to this entry. Used by the renderer to display
    the "first seen" date."""

    ts_last_ns: int = 0
    """Nanosecond timestamp of the most-recent observation. Used
    by the renderer + by the rebuilder's staleness check."""

    n_observations: int = 0
    """How many raw ContextMemory records contributed. The
    renderer surfaces this so the sLLM can weigh the entry
    appropriately (5-sample vs 5,000-sample facts differ)."""

    confidence: float = 1.0
    """[0.0, 1.0] — analyst-side confidence in the entry's facts.
    Computed by the builder from sample-size + signal-quality
    heuristics. Surfaced in the entry footer so the sLLM doesn't
    treat low-confidence entries with the same weight."""

    schema_version: int = SCHEMA_VERSION
    """Schema version of this entry; lets future readers detect
    a v0.5.15 entry vs a v0.6 entry and apply migrations."""

    # ── Helpers ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable shape. Used by :mod:`aegis.knowledge.store`."""
        return {
            "entry_id": self.entry_id,
            "kind": self.kind.value,
            "title": self.title,
            "summary": self.summary,
            "infobox": dict(self.infobox.fields),
            "sections": [
                {"heading": s.heading, "body": s.body} for s in self.sections
            ],
            "related": list(self.related),
            "tags": list(self.tags),
            "ts_first_ns": self.ts_first_ns,
            "ts_last_ns": self.ts_last_ns,
            "n_observations": self.n_observations,
            "confidence": self.confidence,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeEntry:
        """Inverse of :meth:`to_dict`. Defensive — missing /
        malformed fields fall back to safe defaults rather than
        raising, so the store layer never crashes on a partially-
        written file."""
        infobox_raw = payload.get("infobox", {}) or {}
        if not isinstance(infobox_raw, dict):
            infobox_raw = {}
        sections_raw = payload.get("sections", []) or []
        sections: list[Section] = []
        if isinstance(sections_raw, list):
            for s in sections_raw:
                if isinstance(s, dict):
                    sections.append(Section(
                        heading=str(s.get("heading", "")),
                        body=str(s.get("body", "")),
                    ))
        kind_raw = payload.get("kind", "agent")
        try:
            kind = EntryKind(kind_raw)
        except ValueError:
            kind = EntryKind.AGENT
        def _safe_int(v: Any, default: int = 0) -> int:
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        def _safe_float(v: Any, default: float = 0.0) -> float:
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        related_raw = payload.get("related", [])
        if not isinstance(related_raw, list):
            related_raw = []
        tags_raw = payload.get("tags", [])
        if not isinstance(tags_raw, list):
            tags_raw = []

        return cls(
            entry_id=str(payload.get("entry_id", "")),
            kind=kind,
            title=str(payload.get("title", "")),
            summary=str(payload.get("summary", "")),
            infobox=InfoBox(fields=dict(infobox_raw)),
            sections=tuple(sections),
            related=tuple(str(x) for x in related_raw),
            tags=tuple(str(x) for x in tags_raw),
            ts_first_ns=_safe_int(payload.get("ts_first_ns")),
            ts_last_ns=_safe_int(payload.get("ts_last_ns")),
            n_observations=_safe_int(payload.get("n_observations")),
            confidence=_safe_float(payload.get("confidence"), 1.0),
            schema_version=_safe_int(
                payload.get("schema_version"), SCHEMA_VERSION,
            ),
        )


# ──────────────────────────────────────────────────────────────────
# Entry-id helpers
# ──────────────────────────────────────────────────────────────────


def make_entry_id(kind: EntryKind, slug: str) -> str:
    """Compose a canonical entry_id. The slug is the entity's
    natural name (aid, tool name, reason_signature); we don't
    normalise it here so cross-refs are byte-stable."""
    return f"{kind.value}/{slug}"


def split_entry_id(entry_id: str) -> tuple[EntryKind, str]:
    """Inverse of :func:`make_entry_id`. Raises ``ValueError``
    on malformed input — the caller is expected to handle the
    defensive case (e.g. a stale cross-reference)."""
    if "/" not in entry_id:
        raise ValueError(f"entry_id missing kind prefix: {entry_id!r}")
    kind_str, slug = entry_id.split("/", 1)
    try:
        kind = EntryKind(kind_str)
    except ValueError as e:
        raise ValueError(f"unknown entry kind: {kind_str!r}") from e
    if not slug:
        raise ValueError(f"empty slug in entry_id: {entry_id!r}")
    return kind, slug


__all__ = [
    "SCHEMA_VERSION",
    "EntryKind",
    "InfoBox",
    "KnowledgeEntry",
    "Section",
    "make_entry_id",
    "split_entry_id",
]
