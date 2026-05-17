#!/usr/bin/env python3
"""Claude Code SessionStart hook — welcome + per-session status banner.

Fires every time Claude Code starts a new session. Two modes:

1. **First session** (no ``~/.aegis/.welcomed`` marker): full welcome
   message that suggests one try-me prompt + surfaces the /aegis-help
   slash command.
2. **Returning user** (v0.7.0): one-line status banner — total audit
   records, BLOCKs in last 24h, autonomy state. Provides "Claude Code
   agent view" parity by surfacing Aegis state at every session start.

### Verbosity control

* ``AEGIS_WELCOME_DISABLE=1`` — silence everything (first-session
  welcome and per-session banner).
* ``AEGIS_SESSION_BANNER=off|brief|full`` (default: ``brief``)
  * ``off``: silent for returning users (legacy v0.5 behaviour).
  * ``brief``: one-line status (new v0.7.0 default).
  * ``full``: force the full welcome even for returning users.

### Failure mode

NEVER blocks the session (always exit 0). All I/O is best-effort and
budgeted to keep cold-path latency well under 100 ms.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import IO, Any

WELCOMED_MARKER = Path.home() / ".aegis" / ".welcomed"
DISABLE = os.environ.get("AEGIS_WELCOME_DISABLE", "0") == "1"
VERBOSE = os.environ.get("AEGIS_HOOK_VERBOSE", "0") == "1"
BANNER_MODE = os.environ.get("AEGIS_SESSION_BANNER", "brief").lower()

# Per-session status banner read budgets — keep small to stay fast.
# 512 KB tail = ~3,500 records at typical ~150 B/rec — comfortably
# covers 24 h for most active sessions while staying well under a
# 100 ms cold-path budget on local SSD.
_AUDIT_TAIL_BYTES = 512 * 1024
_TRUST_TABLE = Path.home() / ".aegis" / "autonomy" / "trust_table.json"
_AUDIT_PATH = Path.home() / ".aegis" / "audit.jsonl"


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
    """First-session welcome — one screen, no scrolling required."""
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


# ──────────────────────────────────────────────────────────────────
# v0.7.0 — per-session status banner
# ──────────────────────────────────────────────────────────────────


def _read_audit_tail() -> list[dict]:
    """Read at most the last ``_AUDIT_TAIL_BYTES`` of audit.jsonl as
    parsed records. Defensive — returns [] on any I/O / decode error."""
    try:
        size = _AUDIT_PATH.stat().st_size
    except OSError:
        return []
    offset = max(0, size - _AUDIT_TAIL_BYTES)
    try:
        with _AUDIT_PATH.open("rb") as f:
            f.seek(offset)
            blob = f.read()
    except OSError:
        return []
    # Drop the partial first line if we started mid-record.
    if offset > 0:
        idx = blob.find(b"\n")
        if idx >= 0:
            blob = blob[idx + 1 :]
    records: list[dict] = []
    for line in blob.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _total_audit_lines() -> int:
    """Fast line count of audit.jsonl. Best-effort."""
    try:
        with _AUDIT_PATH.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _autonomy_state() -> tuple[bool, int]:
    """Returns (learned, n_patterns). The SessionStart hook process
    doesn't inherit AEGIS_AUTONOMY_ENABLED from the PreToolUse hook
    command, so we report the practical signal: "has the operator
    run ``aegis autonomy learn`` and produced a non-empty trust
    table?". That is the gate that controls whether autonomy can
    fire on the hot path — env-flag-off + non-empty table is rare
    and fixable by setting one env var."""
    n_patterns = 0
    if _TRUST_TABLE.exists():
        try:
            d = json.loads(_TRUST_TABLE.read_text())
            patterns = d.get("patterns") if isinstance(d, dict) else None
            if isinstance(patterns, list):
                n_patterns = len(patterns)
        except (OSError, json.JSONDecodeError):
            pass
    return n_patterns > 0, n_patterns


def _brief_banner_line() -> str | None:
    """Compose the one-line status banner. Returns None on total
    failure so the hook can stay silent rather than emit garbage."""
    total = _total_audit_lines()
    if total <= 0:
        return None
    tail = _read_audit_tail()
    # Last 24h BLOCK count from the tail. (24h often fits in 64 KB at
    # ~150 B/record; if not, this is a conservative under-count.)
    now_ns = time.time_ns()
    horizon_ns = now_ns - 24 * 3600 * 1_000_000_000
    n_block_24h = 0
    for r in tail:
        ts = r.get("ts_ns")
        if (
            isinstance(ts, int)
            and ts >= horizon_ns
            and str(r.get("decision", "")).upper() == "BLOCK"
        ):
            n_block_24h += 1

    learned, n_pat = _autonomy_state()
    aut_str = (
        f"autonomy: {n_pat} pattern(s) learned" if learned
        else "autonomy: not trained"
    )
    return (
        f"🛡️  Aegis · {total:,} audit records · "
        f"{n_block_24h} BLOCKs in 24h · {aut_str}"
    )


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────


def handle_session_start(
    stdin: IO[str] | None = None, stdout: IO[str] | None = None,
) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    raw = in_stream.read()
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
                print(json.dumps(response), file=out_stream)
                return 0
        except json.JSONDecodeError:
            pass

    is_first = _is_first_session()

    if is_first or BANNER_MODE == "full":
        # Full welcome.
        for line in _welcome_lines():
            _emit(line)
        _mark_welcomed()
        response["_aegis"]["welcome"] = "shown"
    elif BANNER_MODE == "off":
        # Legacy quiet mode for returning users.
        response["_aegis"]["welcome"] = "returning-silent"
    else:
        # Default "brief" — one-line per-session banner (v0.7.0).
        banner = _brief_banner_line()
        if banner:
            _emit(banner)
            response["_aegis"]["welcome"] = "brief"
        else:
            response["_aegis"]["welcome"] = "returning-silent"

    print(json.dumps(response), file=out_stream)
    return 0


def main() -> int:
    return handle_session_start()


if __name__ == "__main__":
    raise SystemExit(main())
