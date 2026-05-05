"""PreCompact hook analysis — capture context-management waste.

Claude Code fires the PreCompact hook just before it auto-compacts
the conversation history (when token budget gets close to the
model's context window). The hook gives us a chance to record
"what is about to be thrown away" before it is.

Why we care
-----------

Frequent compaction is a strong inefficiency signal:

* "agent filled the 200k window N times this session" = it's not
  managing context well
* every compaction triggers a full re-summarisation LLM call
  (visible in transcript usage as a sudden cache_creation spike)
* compacted turns are LOST as far as the LLM is concerned — only
  the summary survives

By writing one audit record per compaction, the Stop-hook
retrospective (PR #46) gains a denominator for "compactions per
session" and the fleet monitor (PR #41) can flag agents that
trigger compaction unusually often.

What this module produces
-------------------------

A :class:`CompactionRecord` with the pre-compaction snapshot:
total turns, cumulative tokens / dollars, transcript size,
context-utilisation ratio. Privacy: pure metadata, never the
raw transcript content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aegis.atv.transcript_reader import read_transcript_context
from aegis.cost.retrospective import MODEL_CONTEXT_WINDOWS


@dataclass
class CompactionRecord:
    """One PreCompact event — 'agent is about to discard N turns'."""

    aid: str
    session_id: str
    trigger: str                                  # "auto" | "manual" | ""
    model_for_cost: str

    # Pre-compaction snapshot
    n_turns_before: int = 0
    n_assistant_turns_before: int = 0
    cumulative_tokens_before: float = 0.0
    cumulative_billed_dollars_before: float = 0.0
    transcript_size_bytes_before: int = 0
    transcript_sha3_before: str | None = None

    # Derived
    context_utilization_pre: float = 0.0           # 0..1
    max_input_tokens_seen: float = 0.0


def _count_turns(transcript_path: Path) -> tuple[int, int]:
    """(n_turns_total, n_assistant_turns)."""
    n_total = n_assist = 0
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
            n_total += 1
            kind = ev.get("type") or ev.get("role") or ""
            if kind in ("assistant", "model_response", "claude"):
                n_assist += 1
    except OSError:
        return 0, 0
    return n_total, n_assist


def _max_input_tokens(transcript_path: Path) -> float:
    """Max per-turn input (input + cache_read + cache_creation) seen
    across the transcript — used for context_utilization_pre."""
    peak = 0.0
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
            t = (
                float(usage.get("input_tokens", 0) or 0)
                + float(usage.get("cache_read_input_tokens", 0) or 0)
                + float(usage.get("cache_creation_input_tokens", 0) or 0)
            )
            if t > peak:
                peak = t
    except OSError:
        pass
    return peak


def analyse_precompact_event(
    *,
    session_id: str,
    transcript_path: Path | None,
    trigger: str = "",
    model_for_cost: str = "claude-haiku-4-5",
) -> CompactionRecord:
    """Snapshot pre-compaction state. Belt-and-braces: missing /
    unreadable transcript → zero-filled record (never crash)."""
    record = CompactionRecord(
        aid=session_id,
        session_id=session_id,
        trigger=trigger or "",
        model_for_cost=model_for_cost,
    )
    if not transcript_path or not transcript_path.is_file():
        return record

    try:
        record.transcript_size_bytes_before = transcript_path.stat().st_size
    except OSError:
        record.transcript_size_bytes_before = 0

    try:
        n_total, n_assist = _count_turns(transcript_path)
        record.n_turns_before = n_total
        record.n_assistant_turns_before = n_assist
    except Exception:  # noqa: BLE001
        pass

    try:
        ctx = read_transcript_context(
            transcript_path, model_for_cost=model_for_cost,
        )
        if ctx is not None:
            record.cumulative_tokens_before = (
                ctx.cumulative_cost.cumulative_tokens
            )
            record.cumulative_billed_dollars_before = (
                ctx.cumulative_billed_dollars
            )
            record.transcript_sha3_before = ctx.transcript_sha3
    except Exception:  # noqa: BLE001
        pass

    record.max_input_tokens_seen = _max_input_tokens(transcript_path)
    window = MODEL_CONTEXT_WINDOWS.get(
        model_for_cost, MODEL_CONTEXT_WINDOWS["default"]
    )
    if window > 0:
        record.context_utilization_pre = min(
            1.0, record.max_input_tokens_seen / window
        )

    return record


def to_audit_record(
    rec: CompactionRecord, *, ts_ns: int | None = None,
) -> dict[str, Any]:
    """Wrap into the audit chain shape used by Pre/Post/Stop records."""
    import time
    return {
        "ts_ns": ts_ns or time.time_ns(),
        "tool": "(precompact)",
        "aid": rec.aid,
        "hook": "PreCompact",
        "mode": "local",
        "explain": {"compaction": asdict(rec)},
    }


# Convenience for callers that want to anchor the SHA3 of
# whatever they're discarding in a deterministic way.
def stable_string_hash(s: str, length: int = 16) -> str:
    return hashlib.sha3_256(s.encode("utf-8")).hexdigest()[:length]
