"""Aggregate the local audit JSONL into a cost dashboard.

The audit chain already carries every cost signal we need (step335
trace's ``cum=`` / ``forecast=`` fields + the verdict reason for
escalations). This module parses those traces and rolls them up by
session, by tool, and over time — giving a real ``aegis cost summary``
in plugin mode without needing the D10 cost-tracker module.

Pure function — only reads the audit JSONL, never writes. Caller
formats the :class:`AuditCostSummary` dataclass however they want
(table, JSON, dashboard).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# step335 trace shape (from src/aegis/firewall/step335_cost.py:72):
#   "step335: ok (cum=0.0123, forecast=0.0456, ceiling=1.0000, burn=0.04)"
# `cum=` is the only field present in every shape (ok / approaching /
# REQUIRE_APPROVAL all carry it explicitly or via the reason). We
# parse it out with a regex so we don't depend on the exact format
# beyond the leading "cum=" token.
_CUM_RE = re.compile(r"cum=([\d.]+)")
_FORECAST_RE = re.compile(r"forecast(?:ed_cost_to_completion)?[ =]([\d.]+)")
_CEILING_RE = re.compile(r"ceiling[ =]([\d.]+)|>\s*budget\s+([\d.]+)")
_REASON_OVER = re.compile(r"cumulative_dollars\s+([\d.]+)\s+>\s+budget")


@dataclass
class PerToolStats:
    tool: str
    n_calls: int = 0
    n_block: int = 0
    n_approval: int = 0
    max_cumulative_dollars: float = 0.0


@dataclass
class PerSessionStats:
    aid: str
    n_calls: int = 0
    max_cumulative_dollars: float = 0.0
    n_escalations: int = 0
    first_seen_ns: int = 0
    last_seen_ns: int = 0


@dataclass
class AuditCostSummary:
    audit_path: Path
    n_records_total: int = 0
    n_pretool: int = 0
    n_posttool: int = 0
    n_allow: int = 0
    n_block: int = 0
    n_approval: int = 0
    max_cumulative_dollars: float = 0.0
    final_cumulative_dollars: float = 0.0       # most-recent cum= seen
    n_step335_escalations: int = 0
    n_m12_escalations: int = 0
    per_tool: list[PerToolStats] = field(default_factory=list)
    per_session: list[PerSessionStats] = field(default_factory=list)
    spike_events: list[dict[str, Any]] = field(default_factory=list)


def _parse_step335_cum(trace: str) -> float | None:
    """Pull `cum=X` out of step335 trace, or recover X from a 'cumulative_dollars X > budget Y' reason."""
    m = _CUM_RE.search(trace)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _parse_overrun_dollars(reason: str) -> float | None:
    m = _REASON_OVER.search(reason or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _is_m12_escalation(reason: str) -> bool:
    return "cost-divergence escalation" in (reason or "")


def _is_step335_escalation(reason: str, decision: str) -> bool:
    if decision == "ALLOW":
        return False
    return (
        "cumulative_dollars" in (reason or "")
        and "> budget" in (reason or "")
    ) or "forecasted_cost_to_completion" in (reason or "")


def summarize(audit_path: Path, *, spike_threshold: float = 0.10) -> AuditCostSummary:
    """Walk ``audit_path`` and aggregate. ``spike_threshold`` is the
    minimum dollar-jump (in $) between consecutive records on the same
    session that is recorded as a ``spike_event``."""
    summary = AuditCostSummary(audit_path=audit_path)
    if not audit_path.is_file():
        return summary

    per_tool: dict[str, PerToolStats] = {}
    per_session: dict[str, PerSessionStats] = {}
    last_cum_per_session: dict[str, float] = defaultdict(float)

    with audit_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            summary.n_records_total += 1

            hook = rec.get("hook")
            if hook == "PostToolUse":
                summary.n_posttool += 1
                continue
            # else: PreToolUse decision record
            summary.n_pretool += 1

            decision = str(rec.get("decision", ""))
            if decision == "ALLOW":
                summary.n_allow += 1
            elif decision == "BLOCK":
                summary.n_block += 1
            elif decision == "REQUIRE_APPROVAL":
                summary.n_approval += 1

            tool = str(rec.get("tool", "unknown"))
            aid = str(rec.get("aid", "unknown"))
            ts_ns = int(rec.get("ts_ns", 0) or 0)
            reason = str(rec.get("reason", ""))

            traces = rec.get("explain", {}).get("step_traces", {}) or {}
            s335 = traces.get("aegis.firewall.step335_cost.run", "")

            cum = _parse_step335_cum(s335)
            if cum is None:
                cum = _parse_overrun_dollars(reason)
            if cum is None:
                cum = last_cum_per_session.get(aid, 0.0)

            if cum > summary.max_cumulative_dollars:
                summary.max_cumulative_dollars = cum
            summary.final_cumulative_dollars = cum

            # Spike detection: jump within same session.
            prev = last_cum_per_session.get(aid, 0.0)
            if cum - prev >= spike_threshold:
                summary.spike_events.append(
                    {
                        "ts_ns": ts_ns,
                        "aid": aid,
                        "tool": tool,
                        "from_dollars": prev,
                        "to_dollars": cum,
                        "delta": cum - prev,
                    }
                )
            last_cum_per_session[aid] = cum

            # Per-tool roll-up.
            t_stats = per_tool.setdefault(tool, PerToolStats(tool=tool))
            t_stats.n_calls += 1
            if decision == "BLOCK":
                t_stats.n_block += 1
            elif decision == "REQUIRE_APPROVAL":
                t_stats.n_approval += 1
            if cum > t_stats.max_cumulative_dollars:
                t_stats.max_cumulative_dollars = cum

            # Per-session roll-up.
            s_stats = per_session.setdefault(aid, PerSessionStats(aid=aid))
            s_stats.n_calls += 1
            if cum > s_stats.max_cumulative_dollars:
                s_stats.max_cumulative_dollars = cum
            if s_stats.first_seen_ns == 0 or ts_ns < s_stats.first_seen_ns:
                s_stats.first_seen_ns = ts_ns
            if ts_ns > s_stats.last_seen_ns:
                s_stats.last_seen_ns = ts_ns

            if _is_step335_escalation(reason, decision):
                summary.n_step335_escalations += 1
                s_stats.n_escalations += 1
            if _is_m12_escalation(reason):
                summary.n_m12_escalations += 1
                s_stats.n_escalations += 1

    summary.per_tool = sorted(
        per_tool.values(),
        key=lambda s: s.max_cumulative_dollars,
        reverse=True,
    )
    summary.per_session = sorted(
        per_session.values(),
        key=lambda s: s.max_cumulative_dollars,
        reverse=True,
    )
    return summary
