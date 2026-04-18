#!/usr/bin/env python3
"""Idempotently install the Aegis PreToolUse hook in ~/.claude/settings.json.

Safety properties:
  * If settings.json already exists, it is backed up to
    settings.json.bak.<unix-timestamp> before any modification.
  * Other unrelated keys in settings.json are preserved verbatim.
  * If a PreToolUse hook pointing at THIS aegis_hook.py is already
    present, the script no-ops (so re-running is safe).
  * Other PreToolUse entries — if you have any — are kept; ours is
    appended.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_SCRIPT = HERE / "aegis_hook.py"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def main() -> int:
    if not HOOK_SCRIPT.exists():
        print(_red(f"hook script not found: {HOOK_SCRIPT}"), file=sys.stderr)
        return 1
    if not HOOK_SCRIPT.stat().st_mode & 0o100:
        print(_yellow(f"making {HOOK_SCRIPT.name} executable"))
        HOOK_SCRIPT.chmod(HOOK_SCRIPT.stat().st_mode | 0o111)

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

    if SETTINGS_PATH.exists():
        try:
            existing = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            print(
                _red(f"existing settings.json is not valid JSON ({e}); refusing to touch it."),
                file=sys.stderr,
            )
            return 1
        backup = SETTINGS_PATH.with_name(f"settings.json.bak.{int(time.time())}")
        shutil.copy2(SETTINGS_PATH, backup)
        print(_yellow(f"backed up existing settings → {backup.name}"))
    else:
        existing = {}
        print(f"creating new {SETTINGS_PATH}")

    cmd = f"python3 {HOOK_SCRIPT}"
    new_entry = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd}],
    }

    hooks_section = existing.setdefault("hooks", {})
    pretooluse = hooks_section.setdefault("PreToolUse", [])

    # Idempotency check.
    for entry in pretooluse:
        for h in entry.get("hooks", []):
            if str(HOOK_SCRIPT) in h.get("command", ""):
                print(_green(f"already installed — {h['command']!r}"))
                print("(re-run with --force-reinstall to add anyway)")
                return 0

    pretooluse.append(new_entry)
    SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\n")

    print(_green(f"\u2713 installed PreToolUse hook → {SETTINGS_PATH}"))
    print(f"  command: {cmd}")
    print('  matcher: "*" (every tool — narrow this in settings.json if too noisy)')
    print()
    print("Restart Claude Code for the hook to take effect.")
    print()
    print("Verify it works by asking Claude to do something obvious like:")
    print("    Run rm -rf /  (it should be blocked by Aegis with stderr)")
    print()
    print("Tail the verdicts:")
    print("    curl -s http://localhost:8000/audit/claude-code-XXXXXXXX | jq")
    return 0


if __name__ == "__main__":
    sys.exit(main())
