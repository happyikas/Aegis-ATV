"""Unit tests for tools/aegis_payload.py (D1).

Covers normalize_input for both Claude Code and legacy shapes, and
format_output mapping internal verdicts (allow/block/require_approval)
onto Claude Code's hookSpecificOutput.permissionDecision vocabulary.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tools/ is not a package in pyproject; add it to sys.path so the test can
# import aegis_payload directly without changing project layout.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools"))

import aegis_payload  # noqa: E402


def test_normalize_input_claude_code_shape() -> None:
    req = {
        "session_id": "sess-1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/Users/x/proj",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    out = aegis_payload.normalize_input(req)
    assert out["mode"] == "claude_code"
    assert out["tool"] == "Bash"
    assert out["args"] == {"command": "ls"}
    assert out["aid"] == "sess-1"
    assert out["session_id"] == "sess-1"
    assert out["cwd"] == "/Users/x/proj"
    # invocation_id is auto-filled if absent
    assert isinstance(out["invocation_id"], str)
    assert len(out["invocation_id"]) == 32


def test_normalize_input_legacy_shape() -> None:
    req = {
        "tool": "Read",
        "args": {"file_path": "/etc/passwd"},
        "agent_id": "demo-agent",
    }
    out = aegis_payload.normalize_input(req)
    assert out["mode"] == "legacy"
    assert out["tool"] == "Read"
    assert out["args"] == {"file_path": "/etc/passwd"}
    assert out["aid"] == "demo-agent"
    assert isinstance(out["invocation_id"], str)


def test_normalize_input_legacy_defaults() -> None:
    out = aegis_payload.normalize_input({})
    assert out["mode"] == "legacy"
    assert out["tool"] == ""
    assert out["args"] == {}
    assert out["aid"] == "default"
    assert out["session_id"] == ""


def test_normalize_input_invocation_id_passthrough() -> None:
    req = {"tool_name": "Bash", "tool_input": {}, "invocation_id": "deadbeef"}
    out = aegis_payload.normalize_input(req)
    assert out["invocation_id"] == "deadbeef"


def test_format_output_claude_code_allow() -> None:
    ctx = {"mode": "claude_code"}
    out = aegis_payload.format_output(
        {"decision": "allow", "reason": ""}, ctx, extras={"latency_ms": 12}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "AegisData: ok"
    assert out["continue"] is True
    assert out["suppressOutput"] is False
    assert out["_aegis"]["verdict"] == "allow"
    assert out["_aegis"]["latency_ms"] == 12


def test_format_output_claude_code_block_maps_to_deny() -> None:
    ctx = {"mode": "claude_code"}
    out = aegis_payload.format_output(
        {"decision": "block", "reason": "rule:destructive_fs"}, ctx, extras={}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == (
        "AegisData: rule:destructive_fs"
    )


def test_format_output_claude_code_require_approval_maps_to_ask() -> None:
    ctx = {"mode": "claude_code"}
    out = aegis_payload.format_output(
        {"decision": "require_approval", "reason": "blast=8"}, ctx, extras={}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_format_output_legacy_preserves_extras() -> None:
    ctx = {"mode": "legacy"}
    out = aegis_payload.format_output(
        {"decision": "block", "reason": "rule:x"},
        ctx,
        extras={"telemetry_id": "atv-1", "latency_ms": 7},
    )
    assert out == {
        "verdict": "block",
        "reason": "rule:x",
        "telemetry_id": "atv-1",
        "latency_ms": 7,
    }


def test_format_output_unknown_decision_falls_back_to_allow() -> None:
    ctx = {"mode": "claude_code"}
    out = aegis_payload.format_output(
        {"decision": "weird-new-state", "reason": ""}, ctx, extras={}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
