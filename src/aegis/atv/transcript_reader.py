"""Claude Code transcript reader — extracts ATV-fill context (v4.5+).

Claude Code persists every session as a JSONL file (the
``transcript_path`` in PreToolUse / Stop hooks). Each line is one
event: assistant message, user message, tool call, tool result.

This module reads that JSONL and extracts six pieces of context that
populate ATV subfields the v4.4 adapter currently leaves empty:

* ``last_assistant_message`` — text of the last assistant turn
  (used as ``ATVInput.agent_state_text`` and ``plan_text``).
* ``recent_tool_calls`` — list of the last N tool invocations
  (used as ``ATVInput.recent_actions``).
* ``transcript_sha3`` — SHA3-256 of the entire transcript bytes
  (used as ``ATVInput.memory_fingerprint``).
* ``cumulative_cost`` — token / dollar totals so far
  (used as ``ATVInput.cost_estimate``).
* ``novelty_score`` — Jaccard distance between the new tool args
  and the recent action history (proxy for ``novelty_score``).
* ``mcp_signals`` — counts of MCP tool invocations
  (used as ``ATVInput.mcp_context``).

Read-only: this module never writes the transcript. Failure modes
are caught locally — the caller falls back to the v4.4 sparse adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.cost.pricing import billed_dollars
from aegis.schema import CostEfficiencyMetrics

# How many recent tool calls to surface in recent_actions.
_MAX_RECENT_ACTIONS = 20
# Floor on transcript length we consider "real" (avoid spurious
# extraction from a 0-byte transcript). Below this we skip.
_MIN_TRANSCRIPT_BYTES = 100


@dataclass
class TranscriptContext:
    """Extracted context — one snapshot per PreToolUse call.

    ``cumulative_cost.cumulative_dollars`` is the FLOP-table proxy
    used by M12 cost-divergence (Claim 27); it intentionally over-
    estimates because it can't see Anthropic's cache discounts.
    ``cumulative_billed_dollars`` is the cache-aware estimate that
    matches what Anthropic actually charges (within a few %).
    """

    last_assistant_message: str = ""
    current_plan: str = ""
    recent_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    transcript_sha3: str | None = None
    cumulative_cost: CostEfficiencyMetrics = field(default_factory=CostEfficiencyMetrics)
    cumulative_billed_dollars: float = 0.0   # cache-aware (PR #1)
    novelty_score: float = 0.0
    behavior_metrics: dict[str, float] = field(default_factory=dict)
    mcp_signals: dict[str, float] = field(default_factory=dict)


def read_transcript_context(
    transcript_path: str | Path,
    *,
    next_tool_args_json: str = "",
    model_for_cost: str = "claude-haiku-4-5",
) -> TranscriptContext | None:
    """Read a Claude Code transcript JSONL and extract ATV-fill context.

    Returns ``None`` if the file is missing, empty, or unparseable —
    callers should fall back to the v4.4 sparse adapter on None.
    """
    p = Path(transcript_path)
    if not p.is_file():
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if len(raw) < _MIN_TRANSCRIPT_BYTES:
        return None

    sha3 = hashlib.sha3_256(raw).hexdigest()

    last_assistant = ""
    plan = ""
    tool_calls: list[dict[str, Any]] = []
    in_tokens_total = 0.0
    out_tokens_total = 0.0
    reasoning_tokens_total = 0.0
    # Tracked separately so PR #1 can apply Anthropic's cache-aware
    # pricing (cache_read at 10 %, cache_creation at 125 %) instead of
    # treating both at full input rate.
    cache_read_tokens_total = 0.0
    cache_creation_tokens_total = 0.0
    mcp_call_count = 0
    bash_count = 0
    edit_count = 0
    read_count = 0
    user_messages = 0
    assistant_messages = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Claude Code transcript is well-typed. Field names may vary by
        # version; we look for the common ones.
        kind = ev.get("type") or ev.get("role") or ""
        if kind in ("assistant", "model_response", "claude"):
            assistant_messages += 1
            # Claude Code's real schema nests message.{content,usage}.
            # Tests + legacy callers may pass content/usage at the top
            # level, so we look in both places.
            msg = ev.get("message")
            if not isinstance(msg, dict):
                msg = {}
            content = (
                msg.get("content")
                or ev.get("content")
                or ev.get("text")
                or ""
            )
            if isinstance(content, list):
                # Claude content blocks: list of {type, text|tool_use|...}
                text_parts = [
                    blk.get("text", "")
                    for blk in content
                    if isinstance(blk, dict) and blk.get("type") == "text"
                ]
                content = " ".join(text_parts)
                # tool_use blocks live inside the same content array
                # in Claude Code's real shape — extract them here.
                for blk in (msg.get("content") or ev.get("content") or []):
                    if (
                        isinstance(blk, dict)
                        and blk.get("type") in ("tool_use", "tool_call")
                    ):
                        name = str(
                            blk.get("name") or blk.get("tool_name") or ""
                        )
                        tinput = blk.get("input") or blk.get("tool_input") or {}
                        tool_calls.append({"name": name, "input": tinput})
                        if name.lower().startswith(("mcp__", "mcp:")):
                            mcp_call_count += 1
                        elif name in ("Bash", "shell", "execute_shell"):
                            bash_count += 1
                        elif name in ("Edit", "Write", "MultiEdit"):
                            edit_count += 1
                        elif name in ("Read", "read_file"):
                            read_count += 1
            if isinstance(content, str) and content:
                last_assistant = content
                # First markdown line that looks like a plan ("plan:", "## Plan")
                for ln in content.splitlines():
                    low = ln.strip().lower()
                    if low.startswith(("plan:", "# plan", "## plan", "### plan")):
                        plan = ln.strip()
                        break
            # Token usage — real Claude Code stores it under message.usage.
            usage = msg.get("usage") or ev.get("usage") or {}
            if isinstance(usage, dict):
                in_tokens_total += float(usage.get("input_tokens", 0) or 0)
                out_tokens_total += float(usage.get("output_tokens", 0) or 0)
                # reasoning_tokens (extended thinking) is a synonym used
                # in fixtures; the real shape doesn't carry it as a
                # separate counter (it's bundled into output).
                reasoning_tokens_total += float(
                    usage.get("reasoning_tokens", 0) or 0
                )
                # Cache tokens — accumulate twice: into in_tokens_total
                # for the FLOP-table proxy (M12 needs full token count
                # to compare against HW FLOPs) AND into the dedicated
                # cache buckets so PR #1's cache-aware billed_dollars
                # can apply Anthropic's actual rates (cache_read at
                # 10 %, cache_creation at 125 %).
                cr = float(usage.get("cache_read_input_tokens", 0) or 0)
                cc = float(usage.get("cache_creation_input_tokens", 0) or 0)
                in_tokens_total += cr + cc
                cache_read_tokens_total += cr
                cache_creation_tokens_total += cc
        elif kind in ("user", "human"):
            user_messages += 1
        elif kind in ("tool_use", "tool_call"):
            name = ev.get("name") or ev.get("tool_name") or ""
            tinput = ev.get("input") or ev.get("tool_input") or {}
            tool_calls.append({"name": name, "input": tinput})
            if name.lower().startswith(("mcp__", "mcp:")):
                mcp_call_count += 1
            elif name in ("Bash", "shell", "execute_shell"):
                bash_count += 1
            elif name in ("Edit", "Write", "MultiEdit"):
                edit_count += 1
            elif name in ("Read", "read_file"):
                read_count += 1

    # Trim to last N
    tool_calls = tool_calls[-_MAX_RECENT_ACTIONS:]

    # Compute cumulative cost using the model's FLOPS table.
    # Note: cum_dollars is the FLOP proxy — used by M12 dollar_cost
    # divergence (compares HW FLOPs × $/FLOP to this on the SW side).
    # The cache-aware billed_dollars below is what the operator's
    # Anthropic invoice will actually look like.
    cum_dollars = expected_flops(
        model_for_cost, in_tokens_total, out_tokens_total,
    ) * DEFAULT_DOLLAR_PER_FLOP
    # input_tokens_total above already includes cache_* tokens; subtract
    # them out so the standard-input rate isn't applied twice when we
    # compute the cache-aware billed estimate.
    real_input_tokens = max(
        0.0,
        in_tokens_total - cache_read_tokens_total - cache_creation_tokens_total,
    )
    cum_billed = billed_dollars(
        model_name=model_for_cost,
        input_tokens=real_input_tokens,
        output_tokens=out_tokens_total,
        cache_read_tokens=cache_read_tokens_total,
        cache_creation_tokens=cache_creation_tokens_total,
    )
    cost = CostEfficiencyMetrics(
        input_token_count=in_tokens_total,
        output_token_count=out_tokens_total,
        reasoning_token_count=reasoning_tokens_total,
        cumulative_tokens=in_tokens_total + out_tokens_total + reasoning_tokens_total,
        cumulative_dollars=cum_dollars,
    )

    novelty = _compute_novelty(tool_calls, next_tool_args_json)

    behavior: dict[str, float] = {}
    total_messages = max(1, user_messages + assistant_messages)
    behavior["assistant_to_user_ratio"] = assistant_messages / total_messages
    behavior["bash_call_density"] = float(bash_count) / max(1, len(tool_calls))
    behavior["edit_call_density"] = float(edit_count) / max(1, len(tool_calls))
    behavior["read_call_density"] = float(read_count) / max(1, len(tool_calls))

    mcp_signals: dict[str, float] = {}
    if tool_calls:
        mcp_signals["server_identity_score"] = mcp_call_count / max(1, len(tool_calls))
        mcp_signals["tool_count_change"] = float(len(tool_calls))
        mcp_signals["trust_band"] = 0.5 if mcp_call_count > 0 else 0.0

    return TranscriptContext(
        last_assistant_message=last_assistant[:4000],  # cap before embed
        current_plan=plan[:500],
        recent_tool_calls=tool_calls,
        transcript_sha3=sha3,
        cumulative_cost=cost,
        cumulative_billed_dollars=cum_billed,
        novelty_score=novelty,
        behavior_metrics=behavior,
        mcp_signals=mcp_signals,
    )


def _compute_novelty(
    recent_calls: list[dict[str, Any]],
    next_args_json: str,
) -> float:
    """Jaccard-distance proxy for ``composite_novelty``.

    If the upcoming tool's args share no substring tokens with recent
    history, novelty is high (≈ 1.0). If everything overlaps, ≈ 0.
    Cheap, deterministic, no model dependency.
    """
    if not next_args_json or not recent_calls:
        return 0.0
    next_tokens = set(_tokenize(next_args_json))
    if not next_tokens:
        return 0.0
    overlap_max = 0.0
    for call in recent_calls[-10:]:
        prev = json.dumps(
            call.get("input", {}), sort_keys=True, default=str,
        )
        prev_tokens = set(_tokenize(prev))
        if not prev_tokens:
            continue
        intersection = len(next_tokens & prev_tokens)
        union = len(next_tokens | prev_tokens)
        sim = intersection / union if union > 0 else 0.0
        if sim > overlap_max:
            overlap_max = sim
    return float(min(1.0, max(0.0, 1.0 - overlap_max)))


def _tokenize(s: str) -> list[str]:
    """Cheap tokeniser — non-alnum split, lowercase, length ≥ 2."""
    out: list[str] = []
    cur: list[str] = []
    for ch in s.lower():
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        else:
            if len(cur) >= 2:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 2:
        out.append("".join(cur))
    return out


def operator_present_from_env() -> float:
    """Detect whether a human operator is at the console.

    Heuristic: presence of ``AEGIS_OPERATOR_PRESENT=true`` or
    interactive TTY on stdin. Returns 0..1 — 1.0 means \"operator at
    keyboard\", 0.0 means batch / unattended.
    """
    explicit = os.environ.get("AEGIS_OPERATOR_PRESENT", "").lower()
    if explicit in ("true", "1", "yes"):
        return 1.0
    if explicit in ("false", "0", "no"):
        return 0.0
    # Best-effort TTY check.
    try:
        import sys
        return 1.0 if sys.stdin.isatty() else 0.0
    except (AttributeError, OSError):
        return 0.0


__all__ = [
    "TranscriptContext",
    "operator_present_from_env",
    "read_transcript_context",
]
