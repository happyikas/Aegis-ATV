#!/usr/bin/env python3
"""Claude Code PreToolUse hook → AegisData /evaluate.

Fires before every tool call inside Claude Code, asks the running Aegis
service whether the call should proceed, and short-circuits with stderr
if the verdict is BLOCK (or REQUIRE_APPROVAL by default).

Install in ``~/.claude/settings.json``:

    {
      "hooks": {
        "PreToolUse": [{
          "matcher": "*",
          "hooks": [{
            "type": "command",
            "command": "python3 /ABS/PATH/MVP/tools/aegis_hook.py"
          }]
        }]
      }
    }

Stdlib only — no pip install needed in your Claude Code shell.

Env vars:
    AEGIS_URL              http://localhost:8000  (where Aegis is running)
    AEGIS_TENANT_ID        claude-code            (tagged on every record)
    AEGIS_HOOK_TIMEOUT     5                      (seconds, /evaluate)
    AEGIS_FAIL_OPEN        0                      (set to 1 to allow tools
                                                   when Aegis is unreachable;
                                                   default = block to fail safe)
    AEGIS_APPROVE_AS_BLOCK 1                      (set to 0 to let
                                                   REQUIRE_APPROVAL pass with
                                                   a stderr warning instead of
                                                   blocking)
    AEGIS_HOOK_VERBOSE     0                      (set to 1 to print every
                                                   verdict, even ALLOWs, to
                                                   stderr — useful for debugging)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

# Sibling module — when this script runs as `python3 /abs/path/aegis_hook.py`,
# Python puts /abs/path/ on sys.path[0], so the unqualified import works.
from aegis_safety import classify_call

AEGIS_URL = os.environ.get("AEGIS_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.environ.get("AEGIS_HOOK_TIMEOUT", "5"))
TENANT = os.environ.get("AEGIS_TENANT_ID", "claude-code")
FAIL_OPEN = os.environ.get("AEGIS_FAIL_OPEN", "0") == "1"
APPROVE_AS_BLOCK = os.environ.get("AEGIS_APPROVE_AS_BLOCK", "1") == "1"
VERBOSE = os.environ.get("AEGIS_HOOK_VERBOSE", "0") == "1"

# Map Claude Code's built-in tool names → the tool taxonomy Aegis knows
# about (drives blast-radius lookup in step 320). Anything not in this
# table defaults to "call_external_api" (medium blast = 5).
TOOL_MAP: dict[str, str] = {
    "Bash":              "execute_shell",
    "BashOutput":        "read_file",
    "KillShell":         "execute_shell",
    "Read":              "read_file",
    "Write":             "write_file",
    "Edit":              "write_file",
    "MultiEdit":         "write_file",
    "NotebookEdit":      "write_file",
    "Glob":              "list_directory",
    "Grep":              "read_file",
    "WebFetch":          "call_external_api",
    "WebSearch":         "call_external_api",
    "Task":              "call_external_api",
    "TodoWrite":         "write_file",
    "ExitPlanMode":      "read_file",
    "ListMcpResources":  "read_file",
    "ReadMcpResource":   "read_file",
}


def _emit(msg: str) -> None:
    print(f"[aegis-hook] {msg}", file=sys.stderr, flush=True)


def _build_payload(event: dict) -> dict:
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    session_id = event.get("session_id") or str(uuid.uuid4())

    aegis_tool = TOOL_MAP.get(tool_name, "call_external_api")

    # Heuristic cost estimate per tool family.
    cost: dict[str, float] = {"exp_dollars": 0.0001, "confidence": 0.7}
    if aegis_tool == "write_file":
        body = (
            tool_input.get("content")
            or tool_input.get("new_string")
            or tool_input.get("file_text")
            or ""
        )
        cost["exp_bytes_write"] = float(len(str(body)))
    elif aegis_tool == "execute_shell":
        cost["exp_bytes_write"] = 0.0
        cost["exp_dollars"] = 0.001  # shell side-effects unbounded — bias slightly higher

    args_json = json.dumps(tool_input)[:4000]
    plan_text = f"execute Claude Code tool: {tool_name}"

    # PRE-LLM safety classifier — populates safety_flags from real text.
    # Provider chosen by AEGIS_SAFETY_PROVIDER env (dummy / openai / haiku);
    # default 'dummy' is offline regex, no API key needed.
    safety_flags = classify_call(tool_args_json=args_json, plan_text=plan_text)

    return {
        "header": {
            "trace_id": session_id,
            "span_id": str(uuid.uuid4()),
            "tenant_id": TENANT,
            "aid": f"claude-code-{session_id[:8]}",
            "ats": "ATV-2080-v1",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": f"claude-code session {session_id}",
        "plan_text": plan_text,
        "tool_name": aegis_tool,
        # Cap arg JSON to keep the embedding call cheap & avoid token blowup.
        "tool_args_json": args_json,
        "safety_flags": safety_flags,
        "cost_estimate": cost,
    }


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        _emit("no stdin payload — allowing")
        return 0

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        _emit(f"invalid PreToolUse JSON ({e}) — allowing to avoid breaking session")
        return 0

    # Only handle PreToolUse; other events are noise.
    if event.get("hook_event_name") not in (None, "", "PreToolUse"):
        return 0

    tool_name = event.get("tool_name", "")
    payload = _build_payload(event)

    req = urllib.request.Request(
        f"{AEGIS_URL}/evaluate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            verdict = json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        if FAIL_OPEN:
            _emit(f"Aegis unreachable at {AEGIS_URL} ({e}); FAIL_OPEN → allowing tool")
            return 0
        _emit(
            f"Aegis unreachable at {AEGIS_URL}: {e}\n"
            f"           Set AEGIS_FAIL_OPEN=1 to allow when down, or start the service."
        )
        return 2

    decision = str(verdict.get("decision") or "BLOCK")
    reason = str(verdict.get("reason") or "")
    atv_id = str(verdict.get("atv_id") or "?")[:8]

    if decision == "ALLOW":
        if VERBOSE:
            _emit(f"ALLOW  {tool_name}  atv={atv_id}")
        return 0

    if decision == "REQUIRE_APPROVAL" and not APPROVE_AS_BLOCK:
        _emit(
            f"WARN   {tool_name} would REQUIRE_APPROVAL — letting through (AEGIS_APPROVE_AS_BLOCK=0)\n"
            f"           reason: {reason}  atv={atv_id}"
        )
        return 0

    _emit(
        f"{decision}  {tool_name}  atv={atv_id}\n"
        f"           reason: {reason}"
    )
    return 2  # exit 2 = blocking error (Claude sees stderr, stops the call)


if __name__ == "__main__":
    sys.exit(main())
