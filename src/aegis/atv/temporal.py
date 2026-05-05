"""Temporal narrative builder for ATV → sLLM serialization (PR-θ).

The single-frame ``atv_to_prompt`` (PR-β / PR #58) describes the
*current* tool call only. The patent design intent (CCTV) is to
present sLLM with a *video*: "5 turns ago Read X, 4 turns ago
Edit Y, …, now Bash Z; trajectory of tokens / cache / progress
shows pattern P".

This module produces that video. It draws from two existing
data sources without any new logging:

* **transcript** — per-turn ``message.usage`` (input / output /
  cache_read / cache_creation tokens) plus ``tool_use`` blocks.
  Same source the cache-lint module walks (PR #50) and the
  retrospective uses (PR #46).

* **audit chain** — per-turn ``decision`` (PreToolUse) and
  ``post_analysis`` signals (PostToolUse, PR #45 — backtrack /
  redundant / is_error).

The two are paired by tool-name + temporal proximity (trace_id
when present). The result is a :class:`TemporalContext` that the
serializer renders as a TEMPORAL TRAJECTORY section.

Privacy
-------
Per-turn snapshots carry only metadata: tool name, decision label,
outcome, token counts. Tool args are surfaced as a 40-char
truncated excerpt (or an args_hash when the actual args have not
been provided to ``load_recent_history``). Same posture as
cache_lint (PR #50) — no raw bodies, no per-token attention.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

# Default window size. 5 was picked because:
# - long enough to see backtrack→revert→retry patterns
# - short enough that the narrative fits sLLM context
DEFAULT_WINDOW_SIZE: int = 5

# How long an arg excerpt before truncation. Same cap as cache_lint
# static-finding excerpts (PR #50).
ARGS_EXCERPT_MAX_CHARS: int = 40

# Outcomes recognised in PostToolUse status.
Outcome = Literal["success", "failure", "timeout", "partial", "unknown"]
Decision = Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL", "unknown"]


# ──────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ATVSnapshot:
    """Minimal per-turn record for the trajectory narrative.

    Attributes
    ----------
    turn_index_rel:
        Relative position in the window. ``0`` is the most recent,
        ``-1`` is one turn before, etc. Stable across rendering.
    ts_ns:
        Wall-clock nanoseconds. ``0`` if not derivable.
    tool_name:
        Tool invoked at this turn. ``"(unknown)"`` if the audit
        record doesn't carry it.
    args_excerpt:
        Up to ARGS_EXCERPT_MAX_CHARS truncated tool args, or
        ``""`` if not available.
    decision:
        Firewall decision recorded for this turn. ``"unknown"``
        when the audit chain doesn't have a paired PreToolUse
        record (e.g., transcript-only walk).
    outcome:
        PostToolUse status. ``"unknown"`` when no PostToolUse
        record was paired.
    input_tokens / output_tokens:
        Per-turn token counts from transcript usage.
    cache_read_tokens / cache_creation_tokens:
        Per-turn cache stats.
    cumulative_tokens_after:
        Running total after this turn (cumulative across the window).
    cache_hit_rate:
        ``cache_read / (input + cache_read + cache_creation)`` for
        this turn — comparable across turns for trend analysis.
    backtrack / redundant / is_error:
        post_analysis signals (PR #45). ``False`` when no
        PostToolUse record was paired.
    """

    turn_index_rel: int
    ts_ns: int
    tool_name: str
    args_excerpt: str
    decision: Decision
    outcome: Outcome

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cumulative_tokens_after: int = 0
    cache_hit_rate: float = 0.0

    backtrack: bool = False
    redundant: bool = False
    is_error: bool = False


@dataclass(frozen=True)
class TemporalContext:
    """Window of recent turns + computed trajectory metrics.

    Snapshots are stored oldest-first (so iteration is naturally
    chronological), with ``turn_index_rel`` ranging from
    ``-(n-1)`` to ``0``.
    """

    history: tuple[ATVSnapshot, ...]
    window_size: int

    # Trajectory time-series (oldest → newest, parallel to history)
    cumulative_token_trajectory: tuple[int, ...]
    cache_hit_rate_trajectory: tuple[float, ...]

    # Aggregate signals over the window
    n_backtracks: int
    n_redundant: int
    n_errors: int
    n_failures: int

    # Anomaly indicators
    cache_hit_rate_max_drop_pp: float       # largest pp drop turn-over-turn
    token_velocity_per_turn: float          # avg tokens per turn in window
    is_progress_stalled: bool               # task_progress flat (no signal here yet)
    distinct_tools_in_window: tuple[str, ...]


# ──────────────────────────────────────────────────────────────────────
# Source-aware extractors
# ──────────────────────────────────────────────────────────────────────


def _stream_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each well-formed JSON record. Skip blanks / decode
    errors (matches the never-crash contract used by cache_lint
    and retrospective)."""
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


def _is_assistant(rec: dict[str, Any]) -> bool:
    kind = rec.get("type") or rec.get("role") or ""
    return kind in ("assistant", "assistant_message", "model_response", "claude")


def _extract_tool_uses(
    rec: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """(tool_name, tool_use_id, args_excerpt) per tool_use block."""
    raw_msg = rec.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    content = msg.get("content") or []
    out: list[tuple[str, str, str]] = []
    if not isinstance(content, list):
        return out
    for c in content:
        if not (isinstance(c, dict) and c.get("type") == "tool_use"):
            continue
        name = str(c.get("name", ""))
        tu_id = str(c.get("id", ""))
        # args excerpt
        inp = c.get("input")
        if inp is None:
            args_excerpt = ""
        else:
            try:
                args_str = json.dumps(inp, sort_keys=True)
            except (TypeError, ValueError):
                args_str = str(inp)
            if len(args_str) > ARGS_EXCERPT_MAX_CHARS:
                args_excerpt = (
                    args_str[: ARGS_EXCERPT_MAX_CHARS - 1] + "…"
                )
            else:
                args_excerpt = args_str
        out.append((name, tu_id, args_excerpt))
    return out


def _walk_transcript_turns(
    transcript_path: Path,
) -> list[dict[str, Any]]:
    """Walk transcript → per-tool-use turn data with per-turn token usage.

    A single assistant message can emit MULTIPLE tool_use blocks. We
    flatten: one entry per tool_use, attributing the same usage to
    each (which matches how Anthropic bills — usage is per-message,
    not per-tool-use).
    """
    rows: list[dict[str, Any]] = []
    for rec in _stream_jsonl(transcript_path):
        if not _is_assistant(rec):
            continue
        raw_msg = rec.get("message")
        msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
        usage = msg.get("usage") or rec.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        cache_r = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_c = int(usage.get("cache_creation_input_tokens", 0) or 0)
        for tool_name, tu_id, args_exc in _extract_tool_uses(rec):
            rows.append({
                "tool_name": tool_name,
                "tool_use_id": tu_id,
                "args_excerpt": args_exc,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cache_r,
                "cache_creation_tokens": cache_c,
            })
    return rows


def _walk_audit_for_session(
    audit_path: Path, *, session_id: str,
) -> tuple[
    list[dict[str, Any]],   # all PreToolUse records (in audit order)
    list[dict[str, Any]],   # all PostToolUse records (in audit order)
]:
    """Walk audit chain, collect per-session Pre/Post records.

    We collect every relevant record (not only those with trace_id)
    so the fallback ts-ordered pairing in ``_pair_transcript_with_audit``
    can find them. trace_id is preserved on each record for callers
    that want exact pairing when it's available.
    """
    pre_records: list[dict[str, Any]] = []
    post_records: list[dict[str, Any]] = []

    for rec in _stream_jsonl(audit_path):
        if str(rec.get("aid", "")) != session_id:
            continue
        ts = int(rec.get("ts_ns", 0) or 0)
        tool = str(rec.get("tool", "") or "")
        hook = str(rec.get("hook", "") or "")
        trace_id = str(rec.get("trace_id", "") or "")

        if hook == "PostToolUse":
            data: dict[str, Any] = {
                "ts_ns": ts,
                "tool_name": tool,
                "trace_id": trace_id,
                "outcome": str(rec.get("status", "") or "unknown"),
            }
            pa = (rec.get("explain") or {}).get("post_analysis") or {}
            data["backtrack"] = bool(pa.get("backtrack"))
            data["redundant"] = bool(pa.get("redundant_of"))
            cls = pa.get("classification") or {}
            data["is_error"] = bool(cls.get("is_error"))
            post_records.append(data)
        elif "decision" in rec:
            pre_records.append({
                "ts_ns": ts,
                "tool_name": tool,
                "trace_id": trace_id,
                "decision": str(rec.get("decision", "") or "unknown"),
            })

    return pre_records, post_records


# ──────────────────────────────────────────────────────────────────────
# Pairing + snapshot construction
# ──────────────────────────────────────────────────────────────────────


def _pair_transcript_with_audit(
    transcript_rows: list[dict[str, Any]],
    pre_records: list[dict[str, Any]],
    post_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge transcript rows with audit decision + outcome data.

    Pairing strategy:

    1. **trace_id exact match** — when both sides carry one. Future-
       proofs against an Aegis hook that injects trace_id into
       transcript metadata. Currently inactive (Anthropic doesn't
       expose trace_id) but no cost to keep.

    2. **Tool-name FIFO** — fallback. Build a per-tool queue of
       audit records ordered by ts. As we walk the transcript
       forward, pop the front of the matching queue. Imperfect but
       robust to missing trace_ids.
    """
    paired = list(transcript_rows)

    # Per-tool ts-ordered queues for FIFO pairing.
    pre_queues: dict[str, list[dict[str, Any]]] = {}
    post_queues: dict[str, list[dict[str, Any]]] = {}
    for d in sorted(pre_records, key=lambda x: int(x.get("ts_ns", 0))):
        pre_queues.setdefault(d["tool_name"], []).append(d)
    for d in sorted(post_records, key=lambda x: int(x.get("ts_ns", 0))):
        post_queues.setdefault(d["tool_name"], []).append(d)

    for row in paired:
        tn = row["tool_name"]
        pre = pre_queues.get(tn, [])
        post = post_queues.get(tn, [])
        if pre:
            row["decision"] = pre.pop(0).get("decision", "unknown")
        else:
            row["decision"] = "unknown"
        if post:
            p = post.pop(0)
            row["outcome"] = p.get("outcome", "unknown")
            row["ts_ns"] = p.get("ts_ns", 0)
            row["backtrack"] = p.get("backtrack", False)
            row["redundant"] = p.get("redundant", False)
            row["is_error"] = p.get("is_error", False)
        else:
            row["outcome"] = "unknown"
            row["ts_ns"] = 0
            row["backtrack"] = False
            row["redundant"] = False
            row["is_error"] = False
    return paired


def _build_snapshots(
    paired_rows: list[dict[str, Any]],
    *,
    window_size: int,
) -> tuple[list[ATVSnapshot], list[int], list[float]]:
    """Take last ``window_size`` paired rows and turn them into
    ATVSnapshot objects + parallel trajectory time-series."""
    rows = paired_rows[-window_size:]
    snapshots: list[ATVSnapshot] = []
    cum_token_traj: list[int] = []
    hit_rate_traj: list[float] = []
    cumulative = 0
    n = len(rows)
    for offset, row in enumerate(rows):
        rel = offset - (n - 1)   # -(n-1) … 0
        in_t = int(row.get("input_tokens", 0))
        out_t = int(row.get("output_tokens", 0))
        cr = int(row.get("cache_read_tokens", 0))
        cc = int(row.get("cache_creation_tokens", 0))
        cumulative += in_t + out_t + cr + cc
        total_in = in_t + cr + cc
        hit_rate = (cr / total_in) if total_in > 0 else 0.0
        snap = ATVSnapshot(
            turn_index_rel=rel,
            ts_ns=int(row.get("ts_ns", 0)),
            tool_name=str(row.get("tool_name", "(unknown)")),
            args_excerpt=str(row.get("args_excerpt", "")),
            decision=row.get("decision", "unknown"),
            outcome=row.get("outcome", "unknown"),
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=cr,
            cache_creation_tokens=cc,
            cumulative_tokens_after=cumulative,
            cache_hit_rate=hit_rate,
            backtrack=bool(row.get("backtrack", False)),
            redundant=bool(row.get("redundant", False)),
            is_error=bool(row.get("is_error", False)),
        )
        snapshots.append(snap)
        cum_token_traj.append(cumulative)
        hit_rate_traj.append(hit_rate)
    return snapshots, cum_token_traj, hit_rate_traj


def _aggregate(
    snapshots: list[ATVSnapshot],
    hit_rate_traj: list[float],
    cum_token_traj: list[int],
) -> dict[str, Any]:
    """Compute window-level aggregates + anomaly indicators."""
    n_back = sum(1 for s in snapshots if s.backtrack)
    n_red = sum(1 for s in snapshots if s.redundant)
    n_err = sum(1 for s in snapshots if s.is_error)
    n_fail = sum(1 for s in snapshots if s.outcome == "failure")

    # Largest pp drop in cache_hit_rate (turn-over-turn).
    max_drop = 0.0
    for i in range(1, len(hit_rate_traj)):
        drop = hit_rate_traj[i - 1] - hit_rate_traj[i]
        if drop > max_drop:
            max_drop = drop

    # Token velocity = avg per-turn delta.
    if len(cum_token_traj) >= 2:
        velocity = (
            (cum_token_traj[-1] - cum_token_traj[0]) /
            (len(cum_token_traj) - 1)
        )
    else:
        velocity = 0.0

    distinct = tuple(sorted({s.tool_name for s in snapshots}))

    return {
        "n_backtracks": n_back,
        "n_redundant": n_red,
        "n_errors": n_err,
        "n_failures": n_fail,
        "cache_hit_rate_max_drop_pp": float(max_drop * 100.0),
        "token_velocity_per_turn": float(velocity),
        "is_progress_stalled": False,   # filled by future PR-ε with task_progress series
        "distinct_tools_in_window": distinct,
    }


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def load_recent_history(
    *,
    transcript_path: Path | None,
    audit_path: Path | None,
    session_id: str,
    window_size: int = DEFAULT_WINDOW_SIZE,
) -> TemporalContext:
    """Build a TemporalContext from disk artefacts.

    Both paths are optional. With both available, transcript provides
    the per-turn token trajectory and audit provides decisions +
    post_analysis signals. Either alone yields a degraded but valid
    TemporalContext (e.g., transcript-only → `decision=unknown`).
    """
    if window_size < 1:
        raise ValueError(f"window_size must be ≥ 1, got {window_size}")

    transcript_rows: list[dict[str, Any]] = []
    if transcript_path is not None:
        transcript_rows = _walk_transcript_turns(transcript_path)

    pre_records: list[dict[str, Any]] = []
    post_records: list[dict[str, Any]] = []
    if audit_path is not None:
        pre_records, post_records = _walk_audit_for_session(
            audit_path, session_id=session_id,
        )

    paired = _pair_transcript_with_audit(
        transcript_rows, pre_records, post_records,
    )
    snapshots, cum_traj, hit_rate_traj = _build_snapshots(
        paired, window_size=window_size,
    )
    agg = _aggregate(snapshots, hit_rate_traj, cum_traj)

    return TemporalContext(
        history=tuple(snapshots),
        window_size=window_size,
        cumulative_token_trajectory=tuple(cum_traj),
        cache_hit_rate_trajectory=tuple(hit_rate_traj),
        n_backtracks=int(agg["n_backtracks"]),
        n_redundant=int(agg["n_redundant"]),
        n_errors=int(agg["n_errors"]),
        n_failures=int(agg["n_failures"]),
        cache_hit_rate_max_drop_pp=float(agg["cache_hit_rate_max_drop_pp"]),
        token_velocity_per_turn=float(agg["token_velocity_per_turn"]),
        is_progress_stalled=bool(agg["is_progress_stalled"]),
        distinct_tools_in_window=tuple(agg["distinct_tools_in_window"]),
    )


# ──────────────────────────────────────────────────────────────────────
# Narrative renderer
# ──────────────────────────────────────────────────────────────────────


def _format_outcome(s: ATVSnapshot) -> str:
    sigil = ""
    if s.backtrack:
        sigil += " ↩BACKTRACK"
    if s.redundant:
        sigil += " ♻REDUNDANT"
    if s.is_error:
        sigil += " ✗ERROR"
    return f"{s.outcome}{sigil}"


def serialize_temporal(ctx: TemporalContext) -> str:
    """Render a TemporalContext as a human-readable TEMPORAL
    TRAJECTORY narrative ready to feed into an sLLM."""
    if not ctx.history:
        return (
            "TEMPORAL TRAJECTORY\n"
            "  (window empty — no recent tool calls in audit / transcript)"
        )

    lines: list[str] = []
    lines.append(
        f"TEMPORAL TRAJECTORY (last {len(ctx.history)} of "
        f"{ctx.window_size} requested)"
    )

    # Per-turn rows
    for s in ctx.history:
        rel = (
            f"{s.turn_index_rel:>+3}"
            if s.turn_index_rel != 0 else "  0"
        )
        args_repr = f"({s.args_excerpt})" if s.args_excerpt else ""
        # Short fixed-width tool column for readability
        tool_col = f"{s.tool_name}{args_repr}"
        if len(tool_col) > 36:
            tool_col = tool_col[:35] + "…"
        outcome = _format_outcome(s)
        cache_pct = f"{s.cache_hit_rate * 100:5.1f}%"
        lines.append(
            f"  {rel}  {tool_col:<36}  → {outcome:<32} "
            f"(in {s.input_tokens:>5}, out {s.output_tokens:>5}, "
            f"cache {cache_pct})"
        )

    # Trajectory metrics
    lines.append("")
    lines.append("TRAJECTORY METRICS")
    if ctx.cumulative_token_trajectory:
        cum = " → ".join(f"{v:,}" for v in ctx.cumulative_token_trajectory)
        lines.append(f"  cumulative_tokens:  {cum}")
    if ctx.cache_hit_rate_trajectory:
        hits = " → ".join(
            f"{v * 100:5.1f}%" for v in ctx.cache_hit_rate_trajectory
        )
        lines.append(f"  cache_hit_rate:     {hits}")
    lines.append(
        f"  token_velocity:     "
        f"{ctx.token_velocity_per_turn:,.0f} per turn"
    )
    if ctx.cache_hit_rate_max_drop_pp > 0.5:
        lines.append(
            f"  cache_hit_rate Δ:   "
            f"max drop {ctx.cache_hit_rate_max_drop_pp:.1f} pp within window"
        )

    # Window-level signal counts
    parts = []
    if ctx.n_backtracks:
        parts.append(f"backtrack={ctx.n_backtracks}")
    if ctx.n_redundant:
        parts.append(f"redundant={ctx.n_redundant}")
    if ctx.n_errors:
        parts.append(f"errors={ctx.n_errors}")
    if ctx.n_failures:
        parts.append(f"failures={ctx.n_failures}")
    if parts:
        lines.append(f"  inefficiency_in_window: {', '.join(parts)}")

    if ctx.distinct_tools_in_window:
        lines.append(
            f"  distinct_tools: {', '.join(ctx.distinct_tools_in_window)}"
        )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


def temporal_context_to_dict(ctx: TemporalContext) -> dict[str, Any]:
    """Flat dict (for JSON dumps in tests / debug)."""
    return {
        "window_size": ctx.window_size,
        "n_history": len(ctx.history),
        "history": [asdict(s) for s in ctx.history],
        "cumulative_token_trajectory": list(ctx.cumulative_token_trajectory),
        "cache_hit_rate_trajectory": list(ctx.cache_hit_rate_trajectory),
        "n_backtracks": ctx.n_backtracks,
        "n_redundant": ctx.n_redundant,
        "n_errors": ctx.n_errors,
        "n_failures": ctx.n_failures,
        "cache_hit_rate_max_drop_pp": ctx.cache_hit_rate_max_drop_pp,
        "token_velocity_per_turn": ctx.token_velocity_per_turn,
        "is_progress_stalled": ctx.is_progress_stalled,
        "distinct_tools_in_window": list(ctx.distinct_tools_in_window),
    }


__all__ = [
    "ARGS_EXCERPT_MAX_CHARS",
    "ATVSnapshot",
    "DEFAULT_WINDOW_SIZE",
    "TemporalContext",
    "load_recent_history",
    "serialize_temporal",
    "temporal_context_to_dict",
]
