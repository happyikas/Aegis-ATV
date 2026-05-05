#!/usr/bin/env python3
"""Claude Code PreCompact hook — context-management waste capture.

Fires before Claude Code auto-compacts the conversation history
(usually when the cumulative input approaches the model's context
window). Each compaction triggers a full re-summarisation LLM call,
so frequent compactions are an inefficiency signal.

We snapshot the pre-compaction state (tokens, dollars, turn count,
context utilisation) and append one ``hook="PreCompact"`` record to
the audit chain. Stop-hook retrospective (PR #46) and fleet
monitor (PR #41) can both aggregate these to surface "this agent
fills the window N times per session".

Failure mode
------------

This hook NEVER blocks Claude Code (always exit 0). Compaction
proceeds whether or not we record. Errors → stderr, swallowed.

Install via ``aegis install`` (PR #47) — registers automatically
with the right env prefix.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Any

LOCAL_AUDIT_PATH = Path(
    os.environ.get(
        "AEGIS_LOCAL_AUDIT", str(Path.home() / ".aegis" / "audit.jsonl")
    )
)
MODEL_FOR_COST = os.environ.get("AEGIS_MODEL_FOR_COST", "claude-haiku-4-5")
VERBOSE = os.environ.get("AEGIS_HOOK_VERBOSE", "0") == "1"


def _emit(msg: str) -> None:
    print(f"[aegis-precompact] {msg}", file=sys.stderr, flush=True)


def _append_audit(record: dict[str, Any]) -> None:
    try:
        from aegis.audit.local_chain import append as chain_append
        chain_append(LOCAL_AUDIT_PATH, record)
    except OSError:
        pass
    except Exception as e:  # noqa: BLE001 — never crash
        if VERBOSE:
            _emit(f"audit append failed: {e}")


def handle_precompact(
    stdin: IO[str] | None = None, stdout: IO[str] | None = None
) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    raw = in_stream.read()
    if not raw or not raw.strip():
        return 0
    try:
        event: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    if event.get("hook_event_name") not in (None, "", "PreCompact"):
        return 0

    session_id = event.get("session_id", "")
    transcript = event.get("transcript_path", "")
    trigger = event.get("trigger", "")

    summary: dict[str, Any] = {"compaction": "skipped"}
    try:
        from aegis.cost.precompact_analysis import (
            analyse_precompact_event,
            to_audit_record,
        )

        rec = analyse_precompact_event(
            session_id=session_id,
            transcript_path=Path(transcript) if transcript else None,
            trigger=trigger,
            model_for_cost=MODEL_FOR_COST,
        )
        _append_audit(to_audit_record(rec))
        summary = {
            "compaction": "recorded",
            "n_turns_before": rec.n_turns_before,
            "cumulative_tokens_before": rec.cumulative_tokens_before,
            "context_utilization_pre": rec.context_utilization_pre,
            "trigger": trigger,
        }
        if VERBOSE:
            _emit(
                f"compaction recorded — turns={rec.n_turns_before} "
                f"tokens={rec.cumulative_tokens_before:.0f} "
                f"util={rec.context_utilization_pre:.2f}"
            )
    except Exception as e:  # noqa: BLE001 — never crash Claude Code
        summary = {"compaction": "error", "error": str(e)}
        if VERBOSE:
            _emit(f"error: {e}")

    print(json.dumps({"_aegis": summary}), file=out_stream)
    return 0


def main() -> int:
    return handle_precompact()


if __name__ == "__main__":
    raise SystemExit(main())
