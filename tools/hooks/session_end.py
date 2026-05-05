#!/usr/bin/env python3
"""Claude Code Stop hook — auto-import transcript cost (D6).

Donor: aegis-mvp v1.0.0 ``claude_hooks/session_end.py``.

Claude Code fires this hook when a session ends. The hook reads a
JSON payload from stdin::

    {
      "session_id": "abc123",
      "transcript_path": "/Users/x/.claude/projects/.../sess.jsonl"
    }

and (best effort) parses the transcript through
:func:`aegis.cost.transcript.import_into_wal` so cost data is back-filled
into the configured ledger writer (a no-op until Phase 5 binds it).

The donor additionally called ``crypto.merkle.compute_root_now`` and
``wal.writer.flush`` to finalise its WAL. MVP/'s audit log is signed
in-line per record (the M5/M9 chain), so there is no batched root to
finalise — those calls were dropped.

Install via ``aegis install`` (D3) or manually in
``~/.claude/settings.json``::

    {
      "hooks": {
        "Stop": [{
          "hooks": [{
            "type": "command",
            "command": "python3 /ABS/PATH/MVP/tools/hooks/session_end.py"
          }]
        }]
      }
    }
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
RETRO_BRIEF_ENV: str = "AEGIS_RETROSPECTIVE_BRIEF"
MODEL_FOR_COST = os.environ.get("AEGIS_MODEL_FOR_COST", "claude-haiku-4-5")


def _append_audit_record(record: dict[str, Any]) -> None:
    """Append the Stop record into the SHA3 audit chain."""
    try:
        from aegis.audit.local_chain import append as chain_append
        chain_append(LOCAL_AUDIT_PATH, record)
    except OSError:
        pass
    except Exception:  # noqa: BLE001 — Stop must never crash Claude Code
        pass


def _write_retrospective(transcript: str, session_id: str) -> dict[str, Any]:
    """Build the retrospective + append it to the audit chain (PR #2
    of the multi-hook series). Returns a dict for the ``_aegis``
    output envelope."""
    summary: dict[str, Any] = {"retrospective": "skipped"}
    try:
        from aegis.cost.retrospective import (
            analyze_session,
            format_brief,
            to_audit_record,
        )

        retro = analyze_session(
            transcript_path=Path(transcript) if transcript else None,
            audit_path=LOCAL_AUDIT_PATH if LOCAL_AUDIT_PATH.is_file() else None,
            session_id=session_id,
            model_for_cost=MODEL_FOR_COST,
        )
        record = to_audit_record(retro)
        _append_audit_record(record)

        # Optional stderr brief — opt-in via env so default Stop is silent.
        if os.environ.get(RETRO_BRIEF_ENV, "0") in ("1", "true", "True", "yes"):
            sys.stderr.write(format_brief(retro) + "\n")
            sys.stderr.flush()

        summary = {
            "retrospective": "written",
            "n_turns": retro.n_turns,
            "n_tool_calls": retro.n_posttool_records,
            "billed_dollars": retro.cumulative_billed_dollars,
            "cache_hit_rate": retro.cache_hit_rate,
            "backtrack_ratio": retro.backtrack_ratio,
            "redundancy_ratio": retro.redundancy_ratio,
            "error_rate": retro.error_rate,
        }
    except Exception as e:  # noqa: BLE001 — never crash Claude Code
        summary = {"retrospective": "error", "error": str(e)}
    return summary


def handle_session_end(
    stdin: IO[str] | None = None, stdout: IO[str] | None = None
) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    transcript = ""
    session_id = ""
    try:
        req = json.load(in_stream)
        transcript = req.get("transcript_path", "")
        session_id = req.get("session_id", "")
    except (json.JSONDecodeError, ValueError):
        # Claude Code may send empty stdin in some edge cases; degrade gracefully.
        pass

    # 1. Legacy cost-import path (D6) — donor contract. No-op until a
    #    sidecar ledger writer is bound.
    cost_result: dict[str, Any] = {"transcript": "skipped"}
    if transcript and Path(transcript).exists():
        try:
            from aegis.cost.transcript import import_into_wal

            cost_result = import_into_wal(
                Path(transcript), session_id=session_id,
            )
        except Exception as e:  # noqa: BLE001 - never crash
            cost_result = {"transcript": "error", "error": str(e)}

    # 2. Retrospective record (PR #2 of multi-hook series).
    retro_result = _write_retrospective(transcript, session_id)

    print(
        json.dumps({"_aegis": {"cost": cost_result, **retro_result}}),
        file=out_stream,
    )
    return 0


def main() -> int:
    return handle_session_end()


if __name__ == "__main__":
    raise SystemExit(main())
