"""Performance dashboard — aggregate the local audit chain into a
single performance picture.

What this rolls up
------------------

Reads ``~/.aegis/audit.jsonl`` (or any audit JSONL) and computes:

* **Sessions** — number of distinct ``aid``s with a Stop-hook
  ``session_retrospective`` record (PR #46), plus the time window.

* **Cumulative cost & tokens** — sum across Stop records:
  ``cumulative_billed_dollars``, ``input_tokens_total``,
  ``output_tokens_total``, ``cache_read_tokens_total``,
  ``cache_creation_tokens_total``.

* **Cache efficiency** — weighted hit rate (Σ cache_read / Σ total
  input across sessions) plus a per-session mean for distributional
  context.

* **Inefficiency totals** — backtracks, redundant calls, tool
  errors (from Stop ``session_retrospective`` ratios × counts),
  compactions (PreCompact, PR #47), user retries (UserPromptSubmit
  ``user_retry.is_retry`` flag, PR #47).

* **Top inefficient tools** — for sessions whose audit log keeps
  PostToolUse records, group backtrack / redundant counts by the
  ``tool`` field.

What is NOT measured (yet)
--------------------------

**Advisor adoption rate** — whether the user honoured a placement /
eviction / cache_lint recommendation. Adopting a recommendation
shows up indirectly (e.g., a follow-up session has a higher
``cache_hit_rate`` after applying cache_lint suggestions), but that
correlation needs the closed-loop comparator (PR #52). The
dashboard surfaces that need with a "Run cache-lint --compare-with"
prompt; per-advisor invocation logging is a future PR.

Patent linkage
--------------
Same audit chain as Claim 27 (cost-divergence escalation) and
Claim 34 (closed-loop attestation) — the dashboard is the
operator-readable projection of the same signed metadata trail.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────
# Data shape
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ToolInefficiency:
    """Per-tool roll-up of PostToolUse signals."""

    tool: str
    n_backtracks: int = 0
    n_redundant: int = 0
    n_errors: int = 0
    n_calls: int = 0


@dataclass
class PerformanceSummary:
    """Operator-facing performance picture from the audit chain."""

    audit_path: str
    n_records_walked: int = 0

    # Sessions (from Stop hook records)
    n_sessions: int = 0
    earliest_session_ts_ns: int = 0
    latest_session_ts_ns: int = 0

    # Cumulative across sessions
    total_input_tokens: float = 0.0
    total_output_tokens: float = 0.0
    total_cache_read_tokens: float = 0.0
    total_cache_creation_tokens: float = 0.0
    cumulative_billed_dollars: float = 0.0

    # Cache efficiency
    weighted_cache_hit_rate: float = 0.0
    avg_session_cache_hit_rate: float = 0.0

    # Inefficiency totals
    n_backtracks: int = 0
    n_redundant: int = 0
    n_tool_errors: int = 0
    n_tool_success: int = 0
    n_tool_failure: int = 0

    # Per-session distribution
    avg_session_billed_dollars: float = 0.0
    sessions_with_inefficiency_signals: int = 0

    # Hook events
    n_compactions: int = 0
    n_user_retries: int = 0

    # Top inefficient tools (post_analysis-derived)
    top_inefficient_tools: list[ToolInefficiency] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# Audit-chain walk
# ──────────────────────────────────────────────────────────────────────


def _stream_records(path: Path) -> Iterable[dict[str, Any]]:
    """Yield each record from a JSONL audit file. Skips blanks
    and JSON-decode failures (matches the never-crash contract
    used by the cost / cache_lint walkers)."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _stop_record_sessions(
    path: Path,
) -> tuple[
    list[dict[str, Any]],   # session_retrospective dicts
    list[int],              # ts_ns of those Stop records
    int,                    # n_compactions
    int,                    # n_user_retries
    dict[str, ToolInefficiency],  # per-tool inefficiency from PostToolUse
    int,                    # records walked
]:
    """Single pass over the audit chain → all aggregates we need."""
    retrospectives: list[dict[str, Any]] = []
    stop_ts: list[int] = []
    n_compactions = 0
    n_user_retries = 0
    tool_stats: dict[str, ToolInefficiency] = {}
    n_walked = 0

    for rec in _stream_records(path):
        n_walked += 1
        hook = rec.get("hook", "")
        explain = rec.get("explain") or {}

        if hook == "Stop":
            retro = explain.get("session_retrospective")
            if isinstance(retro, dict):
                retrospectives.append(retro)
                ts = int(rec.get("ts_ns", 0) or 0)
                if ts > 0:
                    stop_ts.append(ts)
        elif hook == "PreCompact":
            comp = explain.get("compaction")
            if isinstance(comp, dict):
                n_compactions += 1
        elif hook == "UserPromptSubmit":
            ur = explain.get("user_retry")
            if isinstance(ur, dict) and ur.get("is_retry"):
                n_user_retries += 1
        elif hook == "PostToolUse":
            tool = str(rec.get("tool", "")) or "(unknown)"
            entry = tool_stats.setdefault(tool, ToolInefficiency(tool=tool))
            entry.n_calls += 1
            pa = explain.get("post_analysis") or {}
            if pa.get("backtrack"):
                entry.n_backtracks += 1
            if pa.get("redundant_of"):
                entry.n_redundant += 1
            cls = pa.get("classification") or {}
            if cls.get("is_error"):
                entry.n_errors += 1

    return (
        retrospectives, stop_ts,
        n_compactions, n_user_retries,
        tool_stats, n_walked,
    )


def build_performance_summary(audit_path: Path) -> PerformanceSummary:
    """Walk the audit chain and aggregate a :class:`PerformanceSummary`.

    Belt-and-braces: missing / unreadable audit file → empty summary
    rather than raising. Matches the contract used by every Aegis
    diagnostic walker (cache_lint, retrospective, etc.).
    """
    summary = PerformanceSummary(audit_path=str(audit_path))
    if not audit_path.is_file():
        return summary

    (
        retros, stop_ts,
        n_compactions, n_user_retries,
        tool_stats, n_walked,
    ) = _stop_record_sessions(audit_path)

    summary.n_records_walked = n_walked
    summary.n_sessions = len(retros)
    summary.n_compactions = n_compactions
    summary.n_user_retries = n_user_retries

    if stop_ts:
        summary.earliest_session_ts_ns = min(stop_ts)
        summary.latest_session_ts_ns = max(stop_ts)

    if not retros:
        # No Stop records — return summary with just the hook-event
        # counts. Per-tool stats from PostToolUse records may still
        # be useful even without retrospectives.
        summary.top_inefficient_tools = _top_inefficient(tool_stats, k=5)
        return summary

    # Cumulative sums.
    sum_input = sum_output = 0.0
    sum_cache_read = sum_cache_creation = 0.0
    sum_dollars = 0.0
    sum_session_hit_rates = 0.0
    sum_n_success = 0
    sum_n_failure = 0
    sum_n_backtracks = 0
    sum_n_redundant = 0
    sum_n_errors = 0
    sessions_with_signals = 0

    for r in retros:
        sum_input += float(r.get("input_tokens_total", 0) or 0)
        sum_output += float(r.get("output_tokens_total", 0) or 0)
        sum_cache_read += float(r.get("cache_read_tokens_total", 0) or 0)
        sum_cache_creation += float(
            r.get("cache_creation_tokens_total", 0) or 0
        )
        sum_dollars += float(r.get("cumulative_billed_dollars", 0) or 0)
        sum_session_hit_rates += float(r.get("cache_hit_rate", 0) or 0)
        sum_n_success += int(r.get("n_tool_success", 0) or 0)
        sum_n_failure += int(r.get("n_tool_failure", 0) or 0)
        sum_n_backtracks += int(r.get("n_backtracks", 0) or 0)
        sum_n_redundant += int(r.get("n_redundant", 0) or 0)
        sum_n_errors += int(r.get("n_is_error", 0) or 0)
        if any(
            int(r.get(k, 0) or 0) > 0
            for k in ("n_backtracks", "n_redundant", "n_is_error")
        ):
            sessions_with_signals += 1

    summary.total_input_tokens = sum_input
    summary.total_output_tokens = sum_output
    summary.total_cache_read_tokens = sum_cache_read
    summary.total_cache_creation_tokens = sum_cache_creation
    summary.cumulative_billed_dollars = sum_dollars
    summary.n_tool_success = sum_n_success
    summary.n_tool_failure = sum_n_failure
    summary.n_backtracks = sum_n_backtracks
    summary.n_redundant = sum_n_redundant
    summary.n_tool_errors = sum_n_errors
    summary.sessions_with_inefficiency_signals = sessions_with_signals
    summary.avg_session_billed_dollars = sum_dollars / len(retros)
    summary.avg_session_cache_hit_rate = sum_session_hit_rates / len(retros)

    # Weighted hit rate across sessions.
    total_input_with_cache = (
        sum_input + sum_cache_read + sum_cache_creation
    )
    if total_input_with_cache > 0:
        summary.weighted_cache_hit_rate = (
            sum_cache_read / total_input_with_cache
        )

    summary.top_inefficient_tools = _top_inefficient(tool_stats, k=5)
    return summary


def _top_inefficient(
    tool_stats: dict[str, ToolInefficiency], *, k: int,
) -> list[ToolInefficiency]:
    """Sort tools by total inefficiency events (backtrack + redundant +
    error), descending; return top-K with at least one signal."""
    flagged = [
        t for t in tool_stats.values()
        if (t.n_backtracks + t.n_redundant + t.n_errors) > 0
    ]
    flagged.sort(
        key=lambda t: -(t.n_backtracks + t.n_redundant + t.n_errors),
    )
    return flagged[:k]


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


def summary_to_dict(s: PerformanceSummary) -> dict[str, Any]:
    """Flat dict suitable for `aegis status --performance --json`."""
    return asdict(s)
