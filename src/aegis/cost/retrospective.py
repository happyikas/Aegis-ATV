"""Session-level retrospective analysis — Stop hook entry point.

When a Claude Code session ends, the Stop hook fires once. This module
walks the final transcript + the session's audit records and computes
the **6 efficiency KPIs** that PR #1 left as zero-fill in the
``CostEfficiencyMetrics`` 16-D slot, plus **3 inefficiency
ratios** derived from PR #45's PostToolUse content analysis.

What this fills
---------------

| ATV slot (CostEfficiencyMetrics) | Source |
|---------------------------------|--------|
| `cache_hit_rate`                | transcript usage (cache_read / total_input) |
| `reasoning_to_action_ratio`     | reasoning_tokens / output_tokens |
| `tokens_per_successful_tool_invocation` | cumulative_tokens / N_success |
| `tokens_per_byte_of_final_output` | cumulative_tokens / sum(text_bytes) |
| `context_utilization_ratio`     | max_seen_input_tokens / model_window |
| `budget_burn_rate`              | cumulative_dollars / elapsed_minutes |

Plus three retrospective ratios that can't be measured per-call:

* `backtrack_ratio` = N(Edit/MultiEdit reverts) / N(Edit calls)
* `redundancy_ratio` = N(redundant_call) / N(tool calls)
* `error_rate` = N(is_error responses) / N(PostToolUse records)

Output: one JSON record appended to the audit chain with
``hook == "Stop"`` and ``explain.session_retrospective`` carrying the
flattened :class:`SessionRetrospective`.

Why this is here, not in transcript_reader
-------------------------------------------

`transcript_reader.read_transcript_context()` is invoked on every
PreToolUse — it must be cheap (subset of fields needed at gate
time). This module is the **session retrospective** — invoked once
at Stop, can afford a more thorough walk and brings in audit-chain
data that the per-call adapter doesn't see.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aegis.atv.transcript_reader import read_transcript_context

# Anthropic context windows (tokens). Used for context_utilization_ratio.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-7": 200_000,
    "default": 200_000,
}


@dataclass
class SessionRetrospective:
    """Flat summary of one session — written once at Stop."""

    aid: str
    session_id: str
    model_for_cost: str

    # Counts
    n_turns: int = 0
    n_user_messages: int = 0
    n_assistant_messages: int = 0
    n_tool_calls: int = 0                  # from transcript (LLM-emitted)
    n_pretool_records: int = 0             # from audit
    n_posttool_records: int = 0            # from audit
    n_tool_success: int = 0
    n_tool_failure: int = 0
    n_tool_timeout: int = 0
    n_tool_partial: int = 0

    # ATV efficiency slots (matches CostEfficiencyMetrics layout)
    cache_hit_rate: float = 0.0
    reasoning_to_action_ratio: float = 0.0
    tokens_per_successful_tool_invocation: float = 0.0
    tokens_per_byte_of_final_output: float = 0.0
    context_utilization_ratio: float = 0.0
    budget_burn_rate_dollars_per_min: float = 0.0

    # Inefficiency ratios (from PR #45 post_analysis)
    backtrack_ratio: float = 0.0           # backtracks / edit_calls
    redundancy_ratio: float = 0.0          # redundant_of != None / total
    error_rate: float = 0.0                # is_error count / posttool count

    # Counters (denominators of the ratios — useful for trust)
    n_edit_calls: int = 0
    n_backtracks: int = 0
    n_redundant: int = 0
    n_is_error: int = 0

    # Token totals
    input_tokens_total: float = 0.0
    output_tokens_total: float = 0.0
    reasoning_tokens_total: float = 0.0
    cache_read_tokens_total: float = 0.0
    cache_creation_tokens_total: float = 0.0
    cumulative_billed_dollars: float = 0.0     # cache-aware (PR #1)
    final_assistant_text_bytes: int = 0

    # Time
    first_ts_ns: int = 0
    last_ts_ns: int = 0
    session_duration_seconds: float = 0.0

    # Verification helpers
    transcript_sha3: str | None = None
    n_audit_records_walked: int = 0


# ─────────────────────────────────────────────────────────────────────
# Internals — audit walk
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _AuditAggregate:
    """Per-session aggregates from the audit chain — derives the
    retrospective ratios from PR #45 PostToolUse data."""

    n_pretool: int = 0
    n_posttool: int = 0
    n_success: int = 0
    n_failure: int = 0
    n_timeout: int = 0
    n_partial: int = 0
    n_edit_calls: int = 0
    n_backtracks: int = 0
    n_redundant: int = 0
    n_is_error: int = 0
    first_ts_ns: int = 0
    last_ts_ns: int = 0
    n_walked: int = 0
    max_input_tokens_seen: float = 0.0


def _scan_audit_for_session(
    audit_path: Path, session_id: str,
) -> _AuditAggregate:
    """Walk audit.jsonl, filter to `aid == session_id`, aggregate."""
    agg = _AuditAggregate()
    if not audit_path.is_file():
        return agg
    try:
        for raw in audit_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            agg.n_walked += 1
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(rec.get("aid", "")) != session_id:
                continue
            ts = int(rec.get("ts_ns", 0) or 0)
            if ts > 0:
                if agg.first_ts_ns == 0 or ts < agg.first_ts_ns:
                    agg.first_ts_ns = ts
                if ts > agg.last_ts_ns:
                    agg.last_ts_ns = ts

            if rec.get("hook") == "PostToolUse":
                agg.n_posttool += 1
                status = str(rec.get("status", ""))
                if status == "success":
                    agg.n_success += 1
                elif status == "failure":
                    agg.n_failure += 1
                elif status == "timeout":
                    agg.n_timeout += 1
                elif status == "partial":
                    agg.n_partial += 1

                # PR #45 post_analysis signals
                pa = (
                    rec.get("explain", {}) or {}
                ).get("post_analysis", {}) or {}
                cls = pa.get("classification", {}) or {}
                if cls.get("is_error"):
                    agg.n_is_error += 1
                if rec.get("tool") in ("Edit", "MultiEdit"):
                    agg.n_edit_calls += 1
                    if pa.get("backtrack"):
                        agg.n_backtracks += 1
                if pa.get("redundant_of"):
                    agg.n_redundant += 1
            elif "decision" in rec:
                agg.n_pretool += 1
    except OSError:
        return agg
    return agg


# ─────────────────────────────────────────────────────────────────────
# Internals — transcript walk for assistant text bytes + max input
# ─────────────────────────────────────────────────────────────────────


def _scan_transcript_extras(transcript_path: Path) -> dict[str, float]:
    """Single transcript walk for things `read_transcript_context`
    doesn't expose: max input_tokens seen across turns (for
    context_utilization_ratio) and final assistant text bytes."""
    out: dict[str, float] = {
        "max_input_tokens": 0.0,
        "final_assistant_text_bytes": 0.0,
    }
    if not transcript_path.is_file():
        return out
    try:
        last_assistant_text = ""
        for raw in transcript_path.read_text(
            encoding="utf-8"
        ).splitlines():
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type") or ev.get("role") or ""
            if kind not in ("assistant", "model_response", "claude"):
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            content = msg.get("content") or ev.get("content")
            if isinstance(content, list):
                texts = [
                    blk.get("text", "")
                    for blk in content
                    if isinstance(blk, dict) and blk.get("type") == "text"
                ]
                if texts:
                    last_assistant_text = " ".join(texts)
            elif isinstance(content, str) and content.strip():
                last_assistant_text = content

            usage = msg.get("usage") or ev.get("usage") or {}
            if isinstance(usage, dict):
                # Per-turn input includes cached + new — use the FULL
                # picture for context_utilization (LLM saw all of it).
                turn_input = float(usage.get("input_tokens", 0) or 0)
                turn_input += float(
                    usage.get("cache_read_input_tokens", 0) or 0
                )
                turn_input += float(
                    usage.get("cache_creation_input_tokens", 0) or 0
                )
                if turn_input > out["max_input_tokens"]:
                    out["max_input_tokens"] = turn_input
        out["final_assistant_text_bytes"] = float(
            len(last_assistant_text.encode("utf-8"))
        )
    except OSError:
        return out
    return out


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def analyze_session(
    *,
    transcript_path: Path | None,
    audit_path: Path | None,
    session_id: str,
    model_for_cost: str = "claude-haiku-4-5",
) -> SessionRetrospective:
    """Compute the SessionRetrospective for one session.

    Both ``transcript_path`` and ``audit_path`` are optional —
    missing inputs degrade the retrospective gracefully (zero-filled
    fields) rather than crash. The Stop hook must NEVER crash
    Claude Code, so this function follows the same belt-and-braces
    contract as our other forensic paths.
    """
    retro = SessionRetrospective(
        aid=session_id,
        session_id=session_id,
        model_for_cost=model_for_cost,
    )

    # ── 1. Transcript walk via the existing reader (PR #1 / PR #34) ──
    if transcript_path and transcript_path.is_file():
        ctx = read_transcript_context(
            transcript_path, model_for_cost=model_for_cost,
        )
        if ctx is not None:
            retro.input_tokens_total = ctx.cumulative_cost.input_token_count
            retro.output_tokens_total = ctx.cumulative_cost.output_token_count
            retro.reasoning_tokens_total = ctx.cumulative_cost.reasoning_token_count
            retro.cumulative_billed_dollars = ctx.cumulative_billed_dollars
            retro.transcript_sha3 = ctx.transcript_sha3
            retro.n_tool_calls = len(ctx.recent_tool_calls)

        # Second walk for max_input_tokens + final assistant text bytes.
        # transcript_reader doesn't surface these (per-call irrelevant).
        extras = _scan_transcript_extras(transcript_path)
        max_input = extras.get("max_input_tokens", 0.0)
        retro.final_assistant_text_bytes = int(
            extras.get("final_assistant_text_bytes", 0)
        )

        # Re-derive cache_read / cache_creation from per-turn cache
        # totals — read_transcript_context folds them into in_tokens,
        # but for the cache_hit_rate ratio we need them separated.
        cr, cc, in_real = _split_cache_tokens(transcript_path)
        retro.cache_read_tokens_total = cr
        retro.cache_creation_tokens_total = cc
        # input_tokens_total above includes cache_*; for "real input" we
        # subtract.
        retro.input_tokens_total = max(
            0.0,
            retro.input_tokens_total - cr - cc,
        )

        window = MODEL_CONTEXT_WINDOWS.get(
            model_for_cost, MODEL_CONTEXT_WINDOWS["default"]
        )
        if window > 0:
            retro.context_utilization_ratio = min(1.0, max_input / window)

    # ── 2. Audit walk for retrospective ratios ───────────────────────
    if audit_path and audit_path.is_file():
        agg = _scan_audit_for_session(audit_path, session_id)
        retro.n_pretool_records = agg.n_pretool
        retro.n_posttool_records = agg.n_posttool
        retro.n_tool_success = agg.n_success
        retro.n_tool_failure = agg.n_failure
        retro.n_tool_timeout = agg.n_timeout
        retro.n_tool_partial = agg.n_partial
        retro.n_edit_calls = agg.n_edit_calls
        retro.n_backtracks = agg.n_backtracks
        retro.n_redundant = agg.n_redundant
        retro.n_is_error = agg.n_is_error
        retro.first_ts_ns = agg.first_ts_ns
        retro.last_ts_ns = agg.last_ts_ns
        retro.n_audit_records_walked = agg.n_walked

        if agg.first_ts_ns and agg.last_ts_ns:
            retro.session_duration_seconds = (
                (agg.last_ts_ns - agg.first_ts_ns) / 1_000_000_000.0
            )

    # ── 3. Derive efficiency ratios ──────────────────────────────────
    cumulative_total = (
        retro.input_tokens_total
        + retro.output_tokens_total
        + retro.cache_read_tokens_total
        + retro.cache_creation_tokens_total
    )
    total_input_with_cache = (
        retro.input_tokens_total
        + retro.cache_read_tokens_total
        + retro.cache_creation_tokens_total
    )
    if total_input_with_cache > 0:
        retro.cache_hit_rate = retro.cache_read_tokens_total / total_input_with_cache

    if retro.output_tokens_total > 0:
        retro.reasoning_to_action_ratio = (
            retro.reasoning_tokens_total / retro.output_tokens_total
        )

    if retro.n_tool_success > 0:
        retro.tokens_per_successful_tool_invocation = (
            cumulative_total / retro.n_tool_success
        )

    if retro.final_assistant_text_bytes > 0:
        retro.tokens_per_byte_of_final_output = (
            cumulative_total / retro.final_assistant_text_bytes
        )

    if retro.session_duration_seconds > 60.0:
        minutes = retro.session_duration_seconds / 60.0
        retro.budget_burn_rate_dollars_per_min = (
            retro.cumulative_billed_dollars / minutes
        )

    # Inefficiency ratios.
    if retro.n_edit_calls > 0:
        retro.backtrack_ratio = retro.n_backtracks / retro.n_edit_calls
    if retro.n_pretool_records > 0:
        retro.redundancy_ratio = retro.n_redundant / retro.n_pretool_records
    if retro.n_posttool_records > 0:
        retro.error_rate = retro.n_is_error / retro.n_posttool_records

    # Turn counts (assistant + user) — light approximation.
    if transcript_path and transcript_path.is_file():
        retro.n_user_messages, retro.n_assistant_messages = _count_messages(
            transcript_path
        )
        retro.n_turns = retro.n_user_messages + retro.n_assistant_messages

    return retro


def _split_cache_tokens(
    transcript_path: Path,
) -> tuple[float, float, float]:
    """Walk transcript and return
    (cache_read_total, cache_creation_total, input_real_total)."""
    cr = cc = inr = 0.0
    try:
        for raw in transcript_path.read_text(
            encoding="utf-8"
        ).splitlines():
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type") or ev.get("role") or ""
            if kind not in ("assistant", "model_response", "claude"):
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            usage = msg.get("usage") or ev.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            cr += float(usage.get("cache_read_input_tokens", 0) or 0)
            cc += float(usage.get("cache_creation_input_tokens", 0) or 0)
            inr += float(usage.get("input_tokens", 0) or 0)
    except OSError:
        pass
    return cr, cc, inr


def _count_messages(transcript_path: Path) -> tuple[int, int]:
    """Returns (n_user, n_assistant). Cheap second walk — same file
    is already in OS cache after read_transcript_context."""
    n_u = n_a = 0
    try:
        for raw in transcript_path.read_text(
            encoding="utf-8"
        ).splitlines():
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type") or ev.get("role") or ""
            if kind in ("user", "human"):
                n_u += 1
            elif kind in ("assistant", "model_response", "claude"):
                n_a += 1
    except OSError:
        pass
    return n_u, n_a


def to_audit_record(
    retro: SessionRetrospective, *, ts_ns: int | None = None,
) -> dict[str, Any]:
    """Wrap the retrospective into a record ready for audit append.
    Lives at the same level as PreToolUse / PostToolUse records:

        {hook: "Stop",
         aid: "...",
         ts_ns: ...,
         explain: {session_retrospective: {<flat dict>}}}
    """
    return {
        "ts_ns": ts_ns or time.time_ns(),
        "tool": "(session_end)",
        "aid": retro.aid,
        "hook": "Stop",
        "mode": "local",
        "explain": {"session_retrospective": asdict(retro)},
    }


def format_brief(retro: SessionRetrospective) -> str:
    """One-screen summary suitable for stderr at session end."""
    parts = [
        f"=== Aegis session retrospective (aid={retro.session_id[:24]}) ===",
        f"  duration:        {retro.session_duration_seconds:>9.1f}s",
        f"  turns:           {retro.n_turns:>4}  "
        f"(user={retro.n_user_messages}, assistant={retro.n_assistant_messages})",
        f"  tool calls:      {retro.n_posttool_records:>4}  "
        f"(success={retro.n_tool_success}, fail={retro.n_tool_failure}, "
        f"timeout={retro.n_tool_timeout})",
        f"  total billed $:  ${retro.cumulative_billed_dollars:>9.4f}  "
        f"({retro.budget_burn_rate_dollars_per_min:.2f}$/min)",
        "",
        "  Efficiency:",
        f"    cache_hit_rate:               {retro.cache_hit_rate:>5.2f}",
        f"    reasoning:action ratio:       {retro.reasoning_to_action_ratio:>5.2f}",
        f"    tokens / successful tool:     {retro.tokens_per_successful_tool_invocation:>10.1f}",
        f"    tokens / final output byte:   {retro.tokens_per_byte_of_final_output:>5.2f}",
        f"    context utilisation:          {retro.context_utilization_ratio:>5.2f}",
        "",
        "  Inefficiency:",
        f"    backtrack ratio (Edit→revert): {retro.backtrack_ratio:>5.2f}  "
        f"({retro.n_backtracks}/{retro.n_edit_calls})",
        f"    redundancy ratio (same call):  {retro.redundancy_ratio:>5.2f}  "
        f"({retro.n_redundant}/{retro.n_pretool_records})",
        f"    error rate:                    {retro.error_rate:>5.2f}  "
        f"({retro.n_is_error}/{retro.n_posttool_records})",
    ]
    return "\n".join(parts)
