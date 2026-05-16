"""Autonomy outlier detection (v0.5.11).

When the autonomy bypass is engaged, a stamp lands in the ATV
record's ``step_traces``:

    "aegis.autonomy.step331.run": "step331: auto-approved by trust table
      tool=Bash signature=loop:Bash trust=0.92"

This module scans ContextMemory for records carrying that stamp
and surfaces any that look anomalous — auto-approvals whose
downstream outcome was a BLOCK, a rollback, or a session
collapse. The postmortem surface for `aegis doctor` + the
``aegis autonomy outliers`` CLI.

Goal: even though the operator no longer sees every approval
click, the system still surfaces *which* auto-bypasses turned out
to be mistakes — closing the trust feedback loop.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from aegis.context_memory.record import ContextMemoryRecord

# Marker the firewall step331 stamps into step_traces when it
# auto-bypasses. The presence of this prefix in a record's
# step_traces is the signal that this record was auto-approved.
AUTONOMY_BYPASS_PREFIX = "step331: auto-approved"


@dataclass(frozen=True)
class OutlierEvent:
    """One auto-approval that looks anomalous in retrospect."""

    trace_id: str
    aid: str
    ts_ns: int
    tool_name: str
    bypass_stamp: str               # full step_traces value
    followup_block_trace: str | None
    followup_block_reason: str | None
    severity: str                   # "alert" / "warn"
    explanation: str


def _bypass_stamp(record: ContextMemoryRecord) -> str | None:
    """If the record was auto-bypassed by step331, return the stamp
    text; otherwise ``None``."""
    for value in (record.step_traces or {}).values():
        if isinstance(value, str) and value.startswith(AUTONOMY_BYPASS_PREFIX):
            return value
    return None


def detect_outliers(
    records: Iterable[ContextMemoryRecord],
    *,
    block_lookahead: int = 10,
) -> list[OutlierEvent]:
    """Scan the window for auto-approved records that were followed
    by BLOCK / rollback within the next ``block_lookahead`` calls
    from the same aid.

    Returns the outlier events in chronological order, oldest
    first. The CLI renderer + the `aegis doctor` postmortem walk
    this list.
    """
    rec_list = list(records)
    # Index by aid, sorted chronologically — same shape the
    # learner uses.
    by_aid: dict[str, list[ContextMemoryRecord]] = defaultdict(list)
    for r in rec_list:
        by_aid[r.aid].append(r)
    for aid in by_aid:
        by_aid[aid].sort(key=lambda r: r.ts_ns)

    out: list[OutlierEvent] = []
    for r in rec_list:
        stamp = _bypass_stamp(r)
        if stamp is None:
            continue
        # Find this record's position in its aid timeline.
        timeline = by_aid[r.aid]
        idx = next(
            (i for i, t in enumerate(timeline) if t.trace_id == r.trace_id),
            None,
        )
        if idx is None:
            continue
        # Scan downstream window for BLOCK.
        followups = timeline[idx + 1 : idx + 1 + block_lookahead]
        block = next((t for t in followups if t.decision == "BLOCK"), None)
        if block is None:
            continue
        out.append(OutlierEvent(
            trace_id=r.trace_id,
            aid=r.aid,
            ts_ns=r.ts_ns,
            tool_name=r.tool_name,
            bypass_stamp=stamp,
            followup_block_trace=block.trace_id,
            followup_block_reason=block.reason,
            severity="alert",
            explanation=(
                f"Auto-approved {r.tool_name} call was followed by "
                f"a BLOCK within {block_lookahead} calls. The trust "
                "pattern may be stale or the situation diverged "
                "from burn-in."
            ),
        ))
    # Already in chronological order within each aid; sort the
    # combined list by ts_ns for the cross-aid view.
    out.sort(key=lambda e: e.ts_ns)
    return out


def render_outliers(events: list[OutlierEvent]) -> str:
    """Plain-text rendering for the CLI / doctor postmortem."""
    lines = [
        f"Autonomy outliers — {len(events)} event(s) found",
        "",
    ]
    if not events:
        lines.append(
            "  (no anomalies — every auto-approval was followed by clean execution)"
        )
        return "\n".join(lines)
    for i, e in enumerate(events, start=1):
        lines.append(f"  #{i}  [{e.severity}] {e.tool_name}  aid={e.aid}")
        lines.append(f"      trace:           {e.trace_id}")
        lines.append(f"      bypass:          {e.bypass_stamp}")
        lines.append(f"      followup_block:  {e.followup_block_trace}")
        if e.followup_block_reason:
            lines.append(
                f"      block_reason:    {e.followup_block_reason}"
            )
        lines.append(f"      explanation:     {e.explanation}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "AUTONOMY_BYPASS_PREFIX",
    "OutlierEvent",
    "detect_outliers",
    "render_outliers",
]
