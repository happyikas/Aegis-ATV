"""Policy-corpus diff & log — derived from chunk timestamps.

Chunks already encode their own history through ``created_at``,
``valid_until``, and ``supersedes``. This module reads that metadata
and produces a temporal view of how the corpus evolved — without
needing a separate changelog file (single source of truth: the
chunks themselves).

Three operations:

  diff_since(corpus, since_ts_ns) -> PolicyDiff
    Triple of (added, retired, superseded) chunk lists between
    ``since`` and now.

  log_entries(corpus, *, limit) -> list[LogEntry]
    Time-ordered timeline of mutations: each chunk creation /
    retirement / supersession turns into one entry.

  show_chunk(corpus, chunk_id) -> ShownChunk
    A single chunk + its supersession chain (forward and backward
    via ``supersedes``).

CLI bindings live in ``tools/aegis_cli.py`` under
``aegis policy {diff,log,show}``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aegis.judge.rag_corpus import RagChunk, RagCorpus, _parse_iso_to_ns

_RELATIVE_DAYS_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_since(spec: str, *, now_ns: int | None = None) -> int:
    """Parse a ``--since`` argument into a nanosecond timestamp.

    Accepts:

      * Absolute date: ``2024-08-01`` (midnight UTC inclusive).
      * Absolute datetime: ``2024-08-01T00:00:00Z`` or ``+00:00``.
      * Relative span: ``7d`` / ``2w`` / ``3m`` / ``1y``
        (days / weeks / months=30d / years=365d).
      * Quarter: ``2024-Q1`` / ``2024-Q3`` (start of quarter, UTC).
      * ``all`` / ``epoch``  → 0 (entire history).

    Raises ``ValueError`` on malformed input with a hint at accepted
    forms.
    """
    s = spec.strip()
    if s.lower() in ("all", "epoch"):
        return 0

    if _ISO_DATE_RE.match(s):
        # Bare date → midnight UTC, inclusive.
        return _parse_iso_to_ns(
            s + "T00:00:00Z", field_name="--since",
        ) or 0

    if "T" in s:
        result = _parse_iso_to_ns(s, field_name="--since")
        if result is not None:
            return result

    if (m := _RELATIVE_DAYS_RE.match(s)):
        n = int(m.group(1))
        unit = m.group(2).lower()
        days = {"d": n, "w": n * 7, "m": n * 30, "y": n * 365}[unit]
        anchor = now_ns if now_ns is not None else time.time_ns()
        return anchor - days * 86_400 * 1_000_000_000

    if (m := _QUARTER_RE.match(s)):
        year = int(m.group(1))
        q = int(m.group(2))
        month = (q - 1) * 3 + 1
        dt = datetime(year, month, 1, tzinfo=UTC)
        return int(dt.timestamp() * 1_000_000_000)

    raise ValueError(
        f"unrecognised --since {spec!r}. Use ISO date "
        "(2024-08-01), datetime (2024-08-01T00:00:00Z), "
        "relative (7d / 2w / 3m / 1y), quarter (2024-Q1), "
        "or 'all'."
    )


@dataclass(frozen=True)
class PolicyDiff:
    since_ts_ns: int
    until_ts_ns: int
    added: tuple[RagChunk, ...]              # created_at ≥ since
    retired: tuple[RagChunk, ...]            # valid_until in window
    superseded: tuple[tuple[RagChunk, RagChunk], ...]
    # superseded: (predecessor, successor) pairs where successor was
    # added in window AND has a non-empty supersedes id we can resolve.

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.retired or self.superseded)


def diff_since(
    corpus: RagCorpus, since_ts_ns: int, *, until_ts_ns: int | None = None,
) -> PolicyDiff:
    """Return a :class:`PolicyDiff` for the window
    ``[since_ts_ns, until_ts_ns]`` (default until: now).

    The diff is computed from chunk timestamps alone — no changelog
    file is read — so it always matches the live corpus state.
    """
    until = until_ts_ns if until_ts_ns is not None else time.time_ns()

    by_id = {c.id: c for c in corpus.chunks}

    added: list[RagChunk] = []
    retired: list[RagChunk] = []
    superseded: list[tuple[RagChunk, RagChunk]] = []

    for c in corpus.chunks:
        # Added: created_at within (since, until]
        if c.created_at:
            created_ns = _parse_iso_to_ns(c.created_at, field_name="created_at")
            if created_ns is not None and since_ts_ns < created_ns <= until:
                added.append(c)

        # Retired: valid_until within (since, until]
        if c.valid_until:
            until_ns = _parse_iso_to_ns(c.valid_until, field_name="valid_until")
            if until_ns is not None and since_ts_ns < until_ns <= until:
                retired.append(c)

        # Supersession pair: this chunk supersedes another, and was
        # itself added in the window.
        if c.supersedes:
            predecessor = by_id.get(c.supersedes)
            if predecessor and c.created_at:
                created_ns = _parse_iso_to_ns(
                    c.created_at, field_name="created_at",
                )
                if (
                    created_ns is not None
                    and since_ts_ns < created_ns <= until
                ):
                    superseded.append((predecessor, c))

    return PolicyDiff(
        since_ts_ns=since_ts_ns,
        until_ts_ns=until,
        added=tuple(added),
        retired=tuple(retired),
        superseded=tuple(superseded),
    )


@dataclass(frozen=True)
class LogEntry:
    ts_ns: int
    iso: str
    kind: str                                # "added" / "retired" / "superseded"
    chunk_id: str
    detail: str = ""

    def __lt__(self, other: LogEntry) -> bool:
        return self.ts_ns < other.ts_ns


def log_entries(
    corpus: RagCorpus, *, limit: int = 0,
) -> list[LogEntry]:
    """Time-ordered timeline of corpus mutations. limit=0 → all."""
    entries: list[LogEntry] = []
    by_id = {c.id: c for c in corpus.chunks}

    for c in corpus.chunks:
        if c.created_at:
            ns = _parse_iso_to_ns(c.created_at, field_name="created_at")
            if ns is not None:
                detail = f"category={c.category}"
                if c.supersedes and c.supersedes in by_id:
                    detail += f"; supersedes {c.supersedes}"
                entries.append(LogEntry(
                    ts_ns=ns, iso=c.created_at,
                    kind="added", chunk_id=c.id, detail=detail,
                ))
        if c.valid_until:
            ns = _parse_iso_to_ns(c.valid_until, field_name="valid_until")
            if ns is not None:
                entries.append(LogEntry(
                    ts_ns=ns, iso=c.valid_until,
                    kind="retired", chunk_id=c.id,
                    detail=f"category={c.category}",
                ))

    entries.sort(reverse=True)  # newest-first for log display
    if limit > 0:
        entries = entries[:limit]
    return entries


@dataclass(frozen=True)
class ShownChunk:
    chunk: RagChunk
    predecessor: RagChunk | None             # via chunk.supersedes
    successor: RagChunk | None               # the chunk that supersedes us


def show_chunk(corpus: RagCorpus, chunk_id: str) -> ShownChunk | None:
    """Return the chunk + its predecessor / successor in the
    supersession chain. Returns ``None`` if the id is unknown."""
    target = corpus.by_id(chunk_id)
    if target is None:
        return None
    predecessor = None
    if target.supersedes:
        predecessor = corpus.by_id(target.supersedes)
    successor = next(
        (c for c in corpus.chunks if c.supersedes == chunk_id),
        None,
    )
    return ShownChunk(
        chunk=target,
        predecessor=predecessor,
        successor=successor,
    )


# ── Rendering ─────────────────────────────────────────────────────────


def _iso_window(since_ns: int, until_ns: int) -> str:
    if since_ns == 0:
        since_label = "epoch"
    else:
        since_label = datetime.fromtimestamp(
            since_ns / 1_000_000_000, tz=UTC,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_label = datetime.fromtimestamp(
        until_ns / 1_000_000_000, tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{since_label} → {until_label}"


def render_diff(diff: PolicyDiff) -> str:
    """Human-readable diff for the CLI."""
    out = [
        f"[policy diff] window: {_iso_window(diff.since_ts_ns, diff.until_ts_ns)}",
    ]
    if diff.is_empty:
        out.append("  (no policy mutations in this window)")
        return "\n".join(out)

    if diff.added:
        out.append(f"  added ({len(diff.added)}):")
        for c in diff.added:
            line = f"    + {c.id}  [{c.category}]  {c.created_at}"
            if c.supersedes:
                line += f"  (supersedes {c.supersedes})"
            out.append(line)
    if diff.retired:
        out.append(f"  retired ({len(diff.retired)}):")
        for c in diff.retired:
            out.append(
                f"    - {c.id}  [{c.category}]  valid_until={c.valid_until}"
            )
    if diff.superseded:
        out.append(f"  superseded ({len(diff.superseded)}):")
        for old, new in diff.superseded:
            out.append(f"    {old.id} → {new.id}")
    return "\n".join(out)


def render_log(entries: list[LogEntry]) -> str:
    if not entries:
        return "[policy log] (no entries — corpus has no timestamps)"
    out = [f"[policy log] {len(entries)} entries (newest first):"]
    for e in entries:
        glyph = {"added": "+", "retired": "-", "superseded": "~"}.get(e.kind, "?")
        out.append(f"  {glyph} {e.iso}  {e.kind:<10} {e.chunk_id}  {e.detail}")
    return "\n".join(out)


def render_show(shown: ShownChunk) -> str:
    """Per-chunk detail with predecessor + successor links."""
    c = shown.chunk
    lines = [
        f"[policy show] {c.id}",
        f"  category:    {c.category}",
        f"  title:       {c.title}",
        f"  decision:    {c.decision or '(none)'}",
        f"  policy_rule: {c.policy_rule or '(none)'}",
        f"  created_at:  {c.created_at or '(unset)'}",
        f"  valid_from:  {c.valid_from or '(unset)'}",
        f"  valid_until: {c.valid_until or '(open)'}",
        f"  tags:        {', '.join(c.tags) or '(none)'}",
    ]
    if shown.predecessor is not None:
        lines.append(
            f"  ← supersedes: {shown.predecessor.id} "
            f"(retired {shown.predecessor.valid_until or '(open)'})"
        )
    if shown.successor is not None:
        lines.append(
            f"  → superseded by: {shown.successor.id} "
            f"(active {shown.successor.valid_from or '(unset)'})"
        )
    lines.append("")
    lines.append("  content:")
    for content_line in c.content.splitlines() or [""]:
        lines.append(f"    {content_line}")
    return "\n".join(lines)


# Silence "unused import" linters for the type alias — exported above.
_ = timedelta


__all__ = (
    "LogEntry",
    "PolicyDiff",
    "ShownChunk",
    "diff_since",
    "log_entries",
    "parse_since",
    "render_diff",
    "render_log",
    "render_show",
    "show_chunk",
)
