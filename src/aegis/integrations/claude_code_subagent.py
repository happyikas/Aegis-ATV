"""v0.7.0 — Claude Code transcript ↔ Aegis audit chain cross-reference.

Claude Code stores per-session transcripts at
``~/.claude/projects/<encoded-path>/<session-uuid>.jsonl``. Each
``Task`` tool invocation spawns a subagent (Explore / Plan /
general-purpose / …) with its own context window. The transcript
captures the spawn metadata (subagent_type, description, timestamps)
but **doesn't** record what the firewall said about the subagent's
downstream tool calls.

Aegis audit chain (``~/.aegis/audit.jsonl``) has the verdicts but no
notion of "this call happened inside a Task spawn".

This module bridges the two. For each Task spawn, it:

1. Parses the ``tool_use`` record to get (subagent_type, description,
   spawn_ts, result_ts).
2. Finds Aegis audit records whose ``timestamp`` falls in the
   [spawn_ts, result_ts] window for the same session_id (matched via
   ``aid`` since hooks stamp ``aid = session_id``).
3. Tallies verdict counts + tool mix per subagent.

The output is a tree where each node carries Claude Code's "what was
spawned" + Aegis's "how it behaved". This is the synergy point Claude
Code's agent view doesn't surface on its own.

### Hot-path safety

Everything in this module is pure read-only over local files. Never
raises into the firewall path — used only by ``aegis subagent-graph``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskSpawn:
    """One Claude Code Task tool invocation = one subagent spawn."""

    tool_use_id: str
    subagent_type: str
    description: str
    parent_uuid: str
    """The uuid of the parent message that issued the Task call."""
    session_id: str
    spawn_ts_ns: int
    """ns since epoch — the assistant message timestamp."""
    result_ts_ns: int | None = None
    """ns since epoch — when the tool_result for this Task came back.
    ``None`` if the result is still pending or wasn't captured."""


@dataclass(frozen=True)
class VerdictMix:
    """Verdict tally derived from Aegis audit records in a window."""

    allow: int
    approval: int
    block: int
    tool_counts: dict[str, int]

    @property
    def total(self) -> int:
        return self.allow + self.approval + self.block


@dataclass(frozen=True)
class EnrichedSpawn:
    """Task spawn + its Aegis-observed verdict mix."""

    spawn: TaskSpawn
    verdicts: VerdictMix
    duration_ms: float | None
    """ms between spawn and result. ``None`` if result_ts unknown."""


@dataclass(frozen=True)
class SubagentGraph:
    """All Task spawns in one Claude Code session, enriched with
    Aegis verdicts."""

    session_id: str
    transcript_path: Path
    n_transcript_records: int
    n_audit_records_in_session: int
    spawns: tuple[EnrichedSpawn, ...]


# ──────────────────────────────────────────────────────────────────
# Transcript parsing
# ──────────────────────────────────────────────────────────────────


def _parse_iso8601_ns(ts: str) -> int:
    """Parse ISO-8601 timestamp to ns. Returns 0 on failure (defensive)."""
    try:
        # Claude Code uses RFC3339 with Z suffix.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        return 0


def parse_task_spawns(transcript_path: Path) -> list[TaskSpawn]:
    """Walk a Claude Code transcript and return every Task spawn.

    Defensive — malformed lines are silently skipped. Empty list if
    the file doesn't exist or has no Task invocations."""
    if not transcript_path.exists():
        return []

    # First pass: collect tool_use records for Task calls keyed by id.
    spawn_by_id: dict[str, TaskSpawn] = {}
    # Second pass: find matching tool_result records and stamp result_ts.

    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return []

    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "tool_use":
                continue
            if item.get("name") not in ("Task", "Agent"):
                continue
            inp = item.get("input") or {}
            if not isinstance(inp, dict):
                continue
            tool_use_id = item.get("id") or ""
            if not tool_use_id:
                continue
            spawn_by_id[tool_use_id] = TaskSpawn(
                tool_use_id=tool_use_id,
                subagent_type=str(inp.get("subagent_type", "unknown")),
                description=str(inp.get("description", ""))[:120],
                parent_uuid=str(rec.get("parentUuid", ""))[:36],
                session_id=str(rec.get("sessionId", ""))[:36],
                spawn_ts_ns=_parse_iso8601_ns(rec.get("timestamp", "")),
                result_ts_ns=None,
            )

    # Second pass: find tool_result records that close each spawn.
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        if rec.get("type") != "user":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "tool_result":
                continue
            tu_id = item.get("tool_use_id")
            if tu_id and tu_id in spawn_by_id:
                spawn = spawn_by_id[tu_id]
                spawn_by_id[tu_id] = TaskSpawn(
                    tool_use_id=spawn.tool_use_id,
                    subagent_type=spawn.subagent_type,
                    description=spawn.description,
                    parent_uuid=spawn.parent_uuid,
                    session_id=spawn.session_id,
                    spawn_ts_ns=spawn.spawn_ts_ns,
                    result_ts_ns=_parse_iso8601_ns(rec.get("timestamp", "")),
                )

    return sorted(spawn_by_id.values(), key=lambda s: s.spawn_ts_ns)


# ──────────────────────────────────────────────────────────────────
# Audit chain correlation
# ──────────────────────────────────────────────────────────────────


def _audit_record_aid(rec: dict[str, object]) -> str:
    """Pull the agent id from an audit record. Handles both flat and
    nested header schemas."""
    header = rec.get("header")
    if isinstance(header, dict):
        aid = header.get("aid")
        if aid:
            return str(aid)
    return str(rec.get("aid", ""))


def _audit_record_ts_ns(rec: dict[str, object]) -> int:
    """Pull ts_ns from an audit record. Falls back to ``timestamp_ns``
    (alt schema) and finally to parsing ``timestamp`` (ISO string)."""
    for key in ("ts_ns", "timestamp_ns"):
        ns = rec.get(key)
        if isinstance(ns, int):
            return ns
    ts = rec.get("timestamp", "")
    return _parse_iso8601_ns(ts if isinstance(ts, str) else "")


def correlate_audit(
    spawns: list[TaskSpawn],
    audit_path: Path,
    *,
    session_id: str,
) -> tuple[VerdictMix, list[EnrichedSpawn]]:
    """Walk the audit chain once. For each record, find which (if any)
    Task spawn window contains it and tally its verdict.

    Returns ``(session_totals, enriched_spawns)``. Even spawns with
    zero in-window audit records appear in the output (with empty
    VerdictMix) — that's a legitimate signal too."""
    # Pre-build sorted [spawn_ts, result_ts] index per spawn.
    windows: list[tuple[int, int, str]] = [
        (s.spawn_ts_ns, s.result_ts_ns or s.spawn_ts_ns, s.tool_use_id)
        for s in spawns
        if s.spawn_ts_ns > 0
    ]
    # Per-spawn buckets keyed by tool_use_id.
    per_spawn: dict[str, dict[str, int | dict[str, int]]] = {
        s.tool_use_id: {"allow": 0, "approval": 0, "block": 0, "tools": {}}
        for s in spawns
    }
    session_allow = 0
    session_approval = 0
    session_block = 0
    session_tools: dict[str, int] = {}
    n_session_audit = 0

    if not audit_path.exists():
        # No audit chain available — still return the spawn list so
        # the user can at least see which Task spawns happened, just
        # without verdict enrichment.
        empty_mix = VerdictMix(0, 0, 0, {})
        enriched_empty: list[EnrichedSpawn] = []
        for s in spawns:
            duration_ms: float | None = None
            if s.result_ts_ns is not None and s.spawn_ts_ns > 0:
                duration_ms = (s.result_ts_ns - s.spawn_ts_ns) / 1_000_000.0
            enriched_empty.append(EnrichedSpawn(
                spawn=s, verdicts=empty_mix, duration_ms=duration_ms,
            ))
        return empty_mix, enriched_empty

    try:
        with audit_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                aid = _audit_record_aid(rec)
                if aid != session_id:
                    continue
                n_session_audit += 1

                decision = str(rec.get("decision", "")).upper()
                tool = str(rec.get("tool", "?"))
                if decision == "ALLOW":
                    session_allow += 1
                elif decision == "REQUIRE_APPROVAL":
                    session_approval += 1
                elif decision == "BLOCK":
                    session_block += 1
                session_tools[tool] = session_tools.get(tool, 0) + 1

                ts = _audit_record_ts_ns(rec)
                if ts <= 0:
                    continue
                for start, end, tuid in windows:
                    if start <= ts <= end:
                        bucket = per_spawn[tuid]
                        if decision == "ALLOW":
                            bucket["allow"] = int(bucket["allow"]) + 1  # type: ignore[arg-type]
                        elif decision == "REQUIRE_APPROVAL":
                            bucket["approval"] = int(bucket["approval"]) + 1  # type: ignore[arg-type]
                        elif decision == "BLOCK":
                            bucket["block"] = int(bucket["block"]) + 1  # type: ignore[arg-type]
                        tools = bucket["tools"]
                        assert isinstance(tools, dict)
                        tools[tool] = tools.get(tool, 0) + 1
                        break  # an audit record belongs to at most one window
    except OSError:
        pass

    enriched: list[EnrichedSpawn] = []
    for s in spawns:
        b = per_spawn[s.tool_use_id]
        tools_dict = b["tools"]
        assert isinstance(tools_dict, dict)
        mix = VerdictMix(
            allow=int(b["allow"]),  # type: ignore[arg-type]
            approval=int(b["approval"]),  # type: ignore[arg-type]
            block=int(b["block"]),  # type: ignore[arg-type]
            tool_counts=dict(tools_dict),
        )
        spawn_dur: float | None = None
        if s.result_ts_ns is not None and s.spawn_ts_ns > 0:
            spawn_dur = (s.result_ts_ns - s.spawn_ts_ns) / 1_000_000.0
        enriched.append(
            EnrichedSpawn(spawn=s, verdicts=mix, duration_ms=spawn_dur),
        )

    return (
        VerdictMix(session_allow, session_approval, session_block, session_tools),
        enriched,
    )


# ──────────────────────────────────────────────────────────────────
# Top-level builder
# ──────────────────────────────────────────────────────────────────


def claude_projects_root() -> Path:
    """Standard Claude Code projects directory. Override via
    ``AEGIS_CLAUDE_PROJECTS_DIR`` if non-standard."""
    import os
    override = os.environ.get("AEGIS_CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def find_transcript_for_cwd(cwd: Path) -> list[Path]:
    """Return all transcript JSONLs for the project rooted at ``cwd``.

    Claude Code encodes the path by replacing ``/`` with ``-`` so
    ``/Users/x/foo`` → ``-Users-x-foo``."""
    encoded = str(cwd).replace("/", "-")
    proj_dir = claude_projects_root() / encoded
    if not proj_dir.exists():
        return []
    return sorted(proj_dir.glob("*.jsonl"))


def build_subagent_graph(
    transcript_path: Path,
    *,
    audit_path: Path,
    session_id: str | None = None,
) -> SubagentGraph:
    """End-to-end: parse transcript → correlate audit → return graph."""
    spawns = parse_task_spawns(transcript_path)
    sid = session_id or (spawns[0].session_id if spawns else "")
    n_records = 0
    if transcript_path.exists():
        try:
            with transcript_path.open() as f:
                n_records = sum(1 for line in f if line.strip())
        except OSError:
            pass

    _, enriched = correlate_audit(spawns, audit_path, session_id=sid)

    # Count session-scoped audit records once more for the header
    # (correlate_audit already iterated; we keep the data flow simple
    # and just expose it via the tuple).
    n_audit = 0
    if audit_path.exists():
        try:
            with audit_path.open() as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _audit_record_aid(rec) == sid:
                        n_audit += 1
        except OSError:
            pass

    return SubagentGraph(
        session_id=sid,
        transcript_path=transcript_path,
        n_transcript_records=n_records,
        n_audit_records_in_session=n_audit,
        spawns=tuple(enriched),
    )


# ──────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────


def _fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def render_tree(graph: SubagentGraph) -> str:
    """One-screen plain-text tree. Top line = session header.
    Children = Task spawns in chronological order."""
    lines: list[str] = []
    lines.append(
        f"Subagent graph — session {graph.session_id[:8]} "
        f"({graph.n_transcript_records} transcript · "
        f"{graph.n_audit_records_in_session} audit records)"
    )
    lines.append(f"  transcript: {graph.transcript_path.name}")
    if not graph.spawns:
        lines.append("  (no Task spawns in this session)")
        return "\n".join(lines)

    lines.append("")
    for i, e in enumerate(graph.spawns):
        is_last = i == len(graph.spawns) - 1
        branch = "└─" if is_last else "├─"
        s = e.spawn
        v = e.verdicts
        lines.append(
            f"  {branch} Task → {s.subagent_type}  "
            f'"{s.description}"  ({_fmt_duration(e.duration_ms)})'
        )
        inner = "     " if is_last else "  │  "
        lines.append(
            f"{inner}verdicts: ALLOW {v.allow} · APPROVAL {v.approval} · BLOCK {v.block}"
        )
        if v.tool_counts:
            top = sorted(v.tool_counts.items(), key=lambda kv: -kv[1])[:5]
            tools_str = ", ".join(f"{t}×{n}" for t, n in top)
            lines.append(f"{inner}tools:    {tools_str}")
    return "\n".join(lines)


__all__ = [
    "EnrichedSpawn",
    "SubagentGraph",
    "TaskSpawn",
    "VerdictMix",
    "build_subagent_graph",
    "claude_projects_root",
    "correlate_audit",
    "find_transcript_for_cwd",
    "parse_task_spawns",
    "render_tree",
]
