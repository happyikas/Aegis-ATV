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
import sys
from pathlib import Path
from typing import IO, Any


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

    result: dict[str, Any] = {"transcript": "skipped"}
    if transcript and Path(transcript).exists():
        try:
            from aegis.cost.transcript import import_into_wal

            result = import_into_wal(Path(transcript), session_id=session_id)
        except Exception as e:  # noqa: BLE001 - hook must never crash Claude Code
            result = {"transcript": "error", "error": str(e)}

    print(json.dumps({"_aegis": result}), file=out_stream)
    return 0


def main() -> int:
    return handle_session_end()


if __name__ == "__main__":
    raise SystemExit(main())
