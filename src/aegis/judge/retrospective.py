"""Retrospective advice — compare PreToolUse advisor prediction against
PostToolUse actual outcome (PR-ψ-retrospective, v2.7).

Background
----------

PreToolUse runs the advisor (gated, possibly sLLM) and stamps an
:class:`ActionAdvice` into the audit record. PostToolUse sees the
actual outcome (success / failure / timeout / partial). Comparing the
two gives a feedback signal that:

* the operator sees in real time ("the advisor flagged this and it
  failed → trust the next flag more");
* a future learning loop can use to recalibrate thresholds (Phase D).

This module provides the comparison + audit shape. It does NOT yet
feed back into calibration — that's deferred to Phase D so this
module remains a pure read-only forensic capture.

Match key
---------

The PreToolUse audit record stamps ``invocation_id`` (added in
v2.7). PostToolUse's event also carries ``invocation_id`` from
Claude Code. Walking the audit JSONL in reverse from the current
time and matching on ``invocation_id`` finds the predecessor in O(N)
worst case, O(few) typical (PreToolUse runs immediately before
PostToolUse).

Accuracy categories
-------------------

* ``accurate``       — advisor said ALLOW and tool succeeded; or
                       advisor said BLOCK / REQUIRE_APPROVAL and
                       firewall blocked (no actual outcome to compare).
* ``missed_signal``  — advisor said ALLOW with no recommendations,
                       but tool actually failed. Advisor missed
                       something it could have caught.
* ``false_alarm``    — advisor said REQUIRE_APPROVAL with high-priority
                       recommendations, but tool succeeded cleanly.
                       Advisor over-fired.
* ``not_applicable`` — no PreToolUse advice was emitted (gate
                       skipped or advisor disabled).
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Accuracy = Literal["accurate", "missed_signal", "false_alarm", "not_applicable"]
ToolStatus = Literal["success", "failure", "timeout", "partial"]


# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrospectiveAdvice:
    """One PreToolUse-vs-PostToolUse comparison record."""

    invocation_id: str
    tool_name: str

    predicted_decision: str       # ActionAdvice.decision (or "<no advice>")
    predicted_advisors: tuple[str, ...] = field(default_factory=tuple)
    predicted_priorities: tuple[str, ...] = field(default_factory=tuple)

    actual_status: str = "success"

    accuracy: Accuracy = "not_applicable"
    notes: str = ""
    produced_at_ns: int = 0


# ──────────────────────────────────────────────────────────────────────
# Audit walk + match
# ──────────────────────────────────────────────────────────────────────


def _stream_jsonl_lines(path: Path) -> list[str]:
    """Read all lines once. The audit file is bounded (rotation kicks
    in around 100 MB), and PostToolUse is off the firewall hot path,
    so a full read here is fine."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return fh.readlines()


def find_pretool_record(
    audit_path: Path,
    *,
    invocation_id: str,
    max_lookback: int = 200,
) -> dict[str, Any] | None:
    """Scan the audit (newest-first) for the most recent PreToolUse
    record with the given ``invocation_id``. Returns the record or
    ``None`` if not found within ``max_lookback`` lines.

    ``max_lookback`` bounds the scan — a sensible upper bound since
    PostToolUse runs immediately after PreToolUse, so the match is
    usually 1-2 records away. Capping protects against pathological
    audit files (e.g. operator pasted a huge log).
    """
    if not invocation_id:
        return None

    lines = _stream_jsonl_lines(audit_path)
    if not lines:
        return None

    # Iterate newest-first via deque (avoids materialising reversed
    # list when the file is large).
    tail: deque[str] = deque(lines[-max_lookback:])
    while tail:
        line = tail.pop().strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        # PostToolUse records also carry invocation_id; we want the
        # PreToolUse predecessor (which has ``decision`` + ``trace_id``
        # and no ``hook == "PostToolUse"`` marker).
        if rec.get("hook") == "PostToolUse":
            continue
        if rec.get("invocation_id") == invocation_id:
            return rec
    return None


# ──────────────────────────────────────────────────────────────────────
# Comparator
# ──────────────────────────────────────────────────────────────────────


_HIGH_PRIORITY = {"high"}


def _classify_accuracy(
    *,
    predicted_decision: str,
    predicted_priorities: tuple[str, ...],
    actual_status: str,
) -> tuple[Accuracy, str]:
    """Map (predicted, actual) → accuracy category + one-line note."""
    if predicted_decision == "<no advice>":
        return "not_applicable", "no PreToolUse advice emitted"

    if predicted_decision == "ALLOW":
        # Advisor passed it; if tool failed, that's a missed signal.
        if actual_status in ("failure", "timeout"):
            return (
                "missed_signal",
                f"predicted ALLOW; tool ended {actual_status}",
            )
        return "accurate", f"predicted ALLOW; tool {actual_status}"

    # Advisor escalated.
    if predicted_decision in ("REQUIRE_APPROVAL", "DEFER"):
        if actual_status == "success":
            # Did the advisor have HIGH-priority recommendations? Only
            # then is this a meaningful false alarm — a low/medium
            # advisory that turned out unnecessary is acceptable.
            if any(p in _HIGH_PRIORITY for p in predicted_priorities):
                return (
                    "false_alarm",
                    f"predicted {predicted_decision} with HIGH "
                    f"recommendations; tool succeeded",
                )
            return (
                "accurate",
                f"predicted {predicted_decision}; tool succeeded — "
                "low/medium advisory was reasonable",
            )
        return (
            "accurate",
            f"predicted {predicted_decision}; tool ended {actual_status}",
        )

    if predicted_decision == "BLOCK":
        # Tool was blocked — there's no actual outcome to compare
        # against the firewall verdict in this audit record. Any
        # post-tool record we see was emitted by something other
        # than the blocked call (or a downstream attempt).
        return (
            "accurate",
            "predicted BLOCK; firewall enforced — no actual outcome",
        )

    return "not_applicable", f"unknown predicted decision: {predicted_decision}"


def evaluate_retrospective(
    *,
    invocation_id: str,
    tool_name: str,
    actual_status: ToolStatus,
    audit_path: Path,
) -> RetrospectiveAdvice | None:
    """Find the matching PreToolUse advice in the audit chain and emit
    a :class:`RetrospectiveAdvice` describing the comparison.

    Returns ``None`` when no PreToolUse record can be located (which
    is fine — happens when PreToolUse hook was bypassed). Never raises.
    """
    pre = find_pretool_record(
        audit_path, invocation_id=invocation_id,
    )

    predicted_decision = "<no advice>"
    predicted_advisors: tuple[str, ...] = ()
    predicted_priorities: tuple[str, ...] = ()

    if pre is not None:
        explain = pre.get("explain") or {}
        if isinstance(explain, dict):
            advice = explain.get("action_advice") or {}
            if isinstance(advice, dict):
                predicted_decision = str(advice.get("decision", "<no advice>"))
                recs = advice.get("recommended_advisors") or []
                if isinstance(recs, list):
                    advisors: list[str] = []
                    priorities: list[str] = []
                    for r in recs:
                        if isinstance(r, dict):
                            a = r.get("advisor")
                            p = r.get("priority")
                            if isinstance(a, str):
                                advisors.append(a)
                            if isinstance(p, str):
                                priorities.append(p)
                    predicted_advisors = tuple(advisors)
                    predicted_priorities = tuple(priorities)

    accuracy, notes = _classify_accuracy(
        predicted_decision=predicted_decision,
        predicted_priorities=predicted_priorities,
        actual_status=actual_status,
    )

    return RetrospectiveAdvice(
        invocation_id=invocation_id,
        tool_name=tool_name,
        predicted_decision=predicted_decision,
        predicted_advisors=predicted_advisors,
        predicted_priorities=predicted_priorities,
        actual_status=actual_status,
        accuracy=accuracy,
        notes=notes,
        produced_at_ns=time.time_ns(),
    )


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


def retrospective_to_dict(r: RetrospectiveAdvice) -> dict[str, Any]:
    d = asdict(r)
    d["predicted_advisors"] = list(r.predicted_advisors)
    d["predicted_priorities"] = list(r.predicted_priorities)
    return d


def retrospective_from_dict(d: dict[str, Any]) -> RetrospectiveAdvice:
    return RetrospectiveAdvice(
        invocation_id=str(d.get("invocation_id", "")),
        tool_name=str(d.get("tool_name", "")),
        predicted_decision=str(d.get("predicted_decision", "<no advice>")),
        predicted_advisors=tuple(
            str(x) for x in (d.get("predicted_advisors") or [])
        ),
        predicted_priorities=tuple(
            str(x) for x in (d.get("predicted_priorities") or [])
        ),
        actual_status=str(d.get("actual_status", "success")),
        accuracy=str(d.get("accuracy", "not_applicable")),  # type: ignore[arg-type]
        notes=str(d.get("notes", "")),
        produced_at_ns=int(d.get("produced_at_ns", 0)),
    )


def render_retrospective(r: RetrospectiveAdvice) -> str:
    """Operator-readable summary."""
    sigil = {
        "accurate":        "·",
        "missed_signal":   "⚠",
        "false_alarm":     "⚠",
        "not_applicable":  "·",
    }.get(r.accuracy, "?")
    lines = [
        f"Retrospective [{r.tool_name} @ {r.invocation_id[:12]}]",
        f"  {sigil} {r.accuracy:<16} — {r.notes}",
        f"  predicted_decision: {r.predicted_decision}",
        f"  actual_status:      {r.actual_status}",
    ]
    if r.predicted_advisors:
        pairs = list(zip(
            r.predicted_priorities, r.predicted_advisors, strict=False,
        ))
        if pairs:
            rendered = ", ".join(
                f"[{p or '?'}] {a}" for p, a in pairs
            )
            lines.append(f"  predicted_advisors: {rendered}")
        else:
            lines.append(
                f"  predicted_advisors: {', '.join(r.predicted_advisors)}"
            )
    return "\n".join(lines)


__all__ = [
    "Accuracy",
    "RetrospectiveAdvice",
    "ToolStatus",
    "evaluate_retrospective",
    "find_pretool_record",
    "render_retrospective",
    "retrospective_from_dict",
    "retrospective_to_dict",
]
