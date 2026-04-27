"""Claude Code hook payload adapter.

D1 (donor: aegis-mvp v1.0.0). Bridges Claude Code's PreToolUse hook
schema to AegisData's internal ``/evaluate`` request shape, and maps
internal verdicts back into the ``hookSpecificOutput.permissionDecision``
vocabulary Claude Code expects.

Claude Code sends::

    {
      "session_id": "abc123",
      "transcript_path": "...",
      "cwd": "/Users/x/proj",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "rm -rf /"}
    }

Legacy/test format (existing MVP callers, agent_demo, scenarios) is::

    {"tool": "Bash", "args": {...}, "agent_id": "..."}

Both are normalised to a single internal dict with a ``mode`` discriminator
so :func:`format_output` can pick the correct response shape.
"""

from __future__ import annotations

import uuid


def normalize_input(req: dict) -> dict:
    """Return canonical ``{tool, args, aid, invocation_id, cwd, session_id, mode}``.

    The ``mode`` field is the discriminator used by :func:`format_output` to
    choose between Claude Code's ``hookSpecificOutput`` schema and the
    legacy ``{verdict, reason, ...}`` payload tests/CLI consumers expect.
    """
    if "tool_name" in req:
        return {
            "tool": req.get("tool_name", ""),
            "args": req.get("tool_input", {}) or {},
            "aid": req.get("session_id", "default"),
            "invocation_id": req.get("invocation_id") or uuid.uuid4().hex,
            "cwd": req.get("cwd", ""),
            "session_id": req.get("session_id", ""),
            "mode": "claude_code",
        }
    return {
        "tool": req.get("tool", ""),
        "args": req.get("args", {}) or {},
        "aid": req.get("agent_id", "default"),
        "invocation_id": req.get("invocation_id") or uuid.uuid4().hex,
        "cwd": req.get("cwd", ""),
        "session_id": req.get("session_id", ""),
        "mode": "legacy",
    }


def format_output(verdict: dict, ctx: dict, extras: dict) -> dict:
    """Build a response payload appropriate for the caller's protocol.

    Claude Code (PreToolUse) expects ``hookSpecificOutput.permissionDecision``
    in ``{"allow", "deny", "ask"}`` — we map AegisData's internal vocabulary
    (``allow`` / ``block`` / ``require_approval``) onto that. Legacy callers
    keep the original ``{verdict, reason, **extras}`` shape.
    """
    decision = verdict.get("decision", "allow")
    reason = verdict.get("reason", "")

    cc_decision = {
        "allow": "allow",
        "block": "deny",
        "require_approval": "ask",
    }.get(decision, "allow")

    legacy = {
        "verdict": decision,
        "reason": reason,
        **extras,
    }

    if ctx["mode"] == "claude_code":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": cc_decision,
                "permissionDecisionReason": (
                    f"AegisData: {reason}" if reason else "AegisData: ok"
                ),
            },
            "continue": True,
            "suppressOutput": False,
            "_aegis": legacy,
        }
    return legacy
