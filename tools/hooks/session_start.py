#!/usr/bin/env python3
"""Claude Code SessionStart hook — first-session welcome message.

Fires every time Claude Code starts a new session. On the very first
firing after install (detected by absence of ``~/.aegis/.welcomed``
marker), print a short friendly welcome to stderr that:

* Confirms Aegis is active
* Suggests one concrete try-me prompt the user can test
* Surfaces the new `/aegis-help` slash command (PR3) for discovery
* Notes the killer feature (signed audit chain) in one line

After the first firing the marker is touched and subsequent
SessionStart events are silent — we don't want to nag returning
users.

Failure mode
------------

NEVER blocks the session (always exit 0). Errors → stderr, swallowed.
Welcome message always opt-out via ``AEGIS_WELCOME_DISABLE=1``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Any

WELCOMED_MARKER = Path.home() / ".aegis" / ".welcomed"
DISABLE = os.environ.get("AEGIS_WELCOME_DISABLE", "0") == "1"
VERBOSE = os.environ.get("AEGIS_HOOK_VERBOSE", "0") == "1"


def _emit(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _is_first_session() -> bool:
    return not WELCOMED_MARKER.exists()


def _mark_welcomed() -> None:
    """Best-effort — never crash if the path can't be written."""
    try:
        WELCOMED_MARKER.parent.mkdir(parents=True, exist_ok=True)
        WELCOMED_MARKER.touch(exist_ok=True)
    except OSError as e:
        if VERBOSE:
            _emit(f"[aegis-welcome] could not create marker: {e}")


def _welcome_lines() -> list[str]:
    """The actual welcome text. One screen, no scrolling required."""
    return [
        "",
        "🛡️  Aegis is active. Every Claude Code tool call is now firewall-",
        "   filtered and cryptographically logged at ~/.aegis/audit.jsonl.",
        "",
        "Try it: ask me to do something destructive in this session, e.g.",
        '   "create a script that recursively deletes /var/data" — Aegis',
        "   will BLOCK before the tool actually runs.",
        "",
        "Slash commands (type these into Claude Code, no shell needed):",
        "   /aegis-help       — list all Aegis commands",
        "   /aegis-report     — risk summary of recent activity",
        "   /aegis-verify     — verify the cryptographic audit chain",
        "",
        "Signed audit log (opt-in, one-time):  aegis audit-key init",
        "Disable this welcome:  export AEGIS_WELCOME_DISABLE=1",
        "",
    ]


def handle_session_start(
    stdin: IO[str] | None = None, stdout: IO[str] | None = None,
) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    raw = in_stream.read()
    # Always emit a JSON ack so Claude Code can compose hook outputs.
    response: dict[str, Any] = {"_aegis": {"welcome": "skipped"}}

    if DISABLE:
        response["_aegis"]["welcome"] = "disabled"
        print(json.dumps(response), file=out_stream)
        return 0

    if raw and raw.strip():
        try:
            event: dict[str, Any] = json.loads(raw)
            evt = event.get("hook_event_name") or ""
            if evt and evt != "SessionStart":
                # Wrong hook payload — silent skip.
                print(json.dumps(response), file=out_stream)
                return 0
        except json.JSONDecodeError:
            pass

    if not _is_first_session():
        # Returning user — silent.
        response["_aegis"]["welcome"] = "returning"
        print(json.dumps(response), file=out_stream)
        return 0

    # First session ever (or marker was deleted) — print welcome.
    for line in _welcome_lines():
        _emit(line)
    _mark_welcomed()
    response["_aegis"]["welcome"] = "shown"
    print(json.dumps(response), file=out_stream)
    return 0


def main() -> int:
    return handle_session_start()


if __name__ == "__main__":
    raise SystemExit(main())
