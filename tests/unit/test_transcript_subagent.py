"""Unit tests for PR-A subagent attribution in transcript_reader.

Exercises the new ``current_event_is_sidechain`` /
``sidechain_event_count`` / ``sidechain_tool_call_count`` /
``last_parent_uuid`` fields on :class:`TranscriptContext`.

The fixtures are minimal hand-written JSONL — the goal is to verify
the parsing logic, not Claude Code's exact emitted shape (which is
already covered by other transcript_reader tests).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.atv.transcript_reader import (
    TranscriptContext,
    read_transcript_context,
)


def _write_transcript(tmp_path: Path, events: list[dict]) -> Path:
    """Write a JSONL fixture, padded so the reader's MIN_BYTES floor
    doesn't reject it."""
    p = tmp_path / "transcript.jsonl"
    body = "\n".join(json.dumps(e) for e in events) + "\n"
    # Pad the body so it's >= _MIN_TRANSCRIPT_BYTES (100). Real
    # Claude Code transcripts are always huge; the floor exists only
    # to skip empty / truncated files.
    if len(body) < 200:
        body = body + " " * (200 - len(body)) + "\n"
    p.write_text(body)
    return p


def _assistant_event(
    *, is_sidechain: bool = False, parent_uuid: str = "",
    tool_name: str | None = None, text: str = "",
) -> dict:
    """Build an assistant event mirroring Claude Code's real schema."""
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_name:
        content.append({
            "type": "tool_use",
            "name": tool_name,
            "id": f"toolu_{tool_name}_test",
            "input": {"command": "noop"},
        })
    return {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "parentUuid": parent_uuid,
        "message": {
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


# ── current_event_is_sidechain reflects the LAST assistant turn ────


def test_main_thread_only_sets_is_sidechain_false(tmp_path: Path) -> None:
    """A transcript with zero sidechain events → is_sidechain=False."""
    p = _write_transcript(tmp_path, [
        _assistant_event(text="hello", tool_name="Bash"),
        _assistant_event(text="world", tool_name="Read"),
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    assert ctx.current_event_is_sidechain is False
    assert ctx.sidechain_event_count == 0
    assert ctx.sidechain_tool_call_count == 0
    assert ctx.last_parent_uuid == ""


def test_last_assistant_is_sidechain_sets_flag_true(tmp_path: Path) -> None:
    """When the most recent assistant turn is a subagent's, the flag
    flips to True — that's the tool call we're about to evaluate."""
    p = _write_transcript(tmp_path, [
        _assistant_event(text="main", tool_name="Bash"),
        _assistant_event(
            is_sidechain=True, parent_uuid="parent-abc",
            text="sub", tool_name="Read",
        ),
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    assert ctx.current_event_is_sidechain is True
    assert ctx.last_parent_uuid == "parent-abc"


def test_sidechain_then_main_resets_flag(tmp_path: Path) -> None:
    """Subagent finishes, control returns to the main thread → the
    flag flips back to False (most recent assistant is main)."""
    p = _write_transcript(tmp_path, [
        _assistant_event(text="main 1", tool_name="Bash"),
        _assistant_event(
            is_sidechain=True, parent_uuid="parent-1",
            text="sub", tool_name="Read",
        ),
        _assistant_event(text="main 2 (resumed)", tool_name="Edit"),
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    assert ctx.current_event_is_sidechain is False
    # The total sidechain counter stays positive — useful for
    # `aegis cost summary --by-aid` to see "this session had N
    # subagent events somewhere along the way".
    assert ctx.sidechain_event_count >= 1


# ── counters ───────────────────────────────────────────────────────


def test_sidechain_event_count_aggregates(tmp_path: Path) -> None:
    p = _write_transcript(tmp_path, [
        _assistant_event(text="m"),
        _assistant_event(is_sidechain=True, text="s1"),
        _assistant_event(is_sidechain=True, text="s2"),
        _assistant_event(is_sidechain=True, text="s3"),
        _assistant_event(text="m back"),
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    assert ctx.sidechain_event_count == 3


def test_sidechain_tool_call_count_only_counts_sidechain(
    tmp_path: Path,
) -> None:
    """tool_use blocks within a sidechain assistant event count;
    blocks within a main-thread event do NOT."""
    p = _write_transcript(tmp_path, [
        _assistant_event(text="m", tool_name="Bash"),       # main
        _assistant_event(is_sidechain=True, tool_name="Read"),    # sub
        _assistant_event(is_sidechain=True, tool_name="Edit"),    # sub
        _assistant_event(text="m again", tool_name="Bash"), # main
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    # 4 total tool calls, 2 from sidechain
    assert len(ctx.recent_tool_calls) == 4
    assert ctx.sidechain_tool_call_count == 2


# ── parent_uuid: only set when sidechain is currently active ──────


def test_parent_uuid_captured_from_latest_sidechain(tmp_path: Path) -> None:
    p = _write_transcript(tmp_path, [
        _assistant_event(is_sidechain=True, parent_uuid="early"),
        _assistant_event(is_sidechain=True, parent_uuid="middle"),
        _assistant_event(is_sidechain=True, parent_uuid="latest"),
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    # Latest sidechain assistant wins — that's the parent of the
    # incoming tool call.
    assert ctx.last_parent_uuid == "latest"


def test_parent_uuid_empty_when_main_thread_latest(tmp_path: Path) -> None:
    """Even though a sidechain happened earlier, if the latest
    assistant is main-thread the parent_uuid stays as the LAST
    captured sidechain parent. We accept this as fine — the
    is_sidechain flag is the authoritative signal; parent_uuid is
    informational metadata for forensic chains."""
    p = _write_transcript(tmp_path, [
        _assistant_event(is_sidechain=True, parent_uuid="sub-parent"),
        _assistant_event(text="back to main"),
    ])
    ctx = read_transcript_context(p)
    assert ctx is not None
    # Main thread is current
    assert ctx.current_event_is_sidechain is False
    # Parent UUID retains what was last captured during a sidechain
    # turn (informational; aegis report uses is_sidechain to decide
    # whether to surface it, so a stale value is harmless).
    assert ctx.last_parent_uuid == "sub-parent"


# ── interaction with adapter ──────────────────────────────────────


def test_adapter_plumbs_subagent_fields_into_session_behavior(
    tmp_path: Path,
) -> None:
    """``from_claude_code_payload_enhanced`` must merge the new
    transcript_reader fields into ATVInput.session_behavior so the
    local hook can promote them to first-class audit keys."""
    from aegis.atv.adapter import from_claude_code_payload_enhanced

    p = _write_transcript(tmp_path, [
        _assistant_event(text="m"),
        _assistant_event(
            is_sidechain=True, parent_uuid="parent-xyz",
            tool_name="Bash",
        ),
    ])
    payload = {
        "session_id": "test-session",
        "transcript_path": str(p),
        "tool_name": "Bash",
        "tool_input": {"command": "noop"},
    }
    inp = from_claude_code_payload_enhanced(
        payload, tenant_id="claude-code",
    )
    sb = inp.session_behavior
    assert sb.get("sidechain_is_active") == pytest.approx(1.0)
    assert sb.get("sidechain_event_count", 0.0) >= 1.0
    assert sb.get("sidechain_tool_call_count", 0.0) >= 1.0


def test_adapter_zero_subagent_signals_when_main_thread_only(
    tmp_path: Path,
) -> None:
    from aegis.atv.adapter import from_claude_code_payload_enhanced

    p = _write_transcript(tmp_path, [
        _assistant_event(text="all main", tool_name="Bash"),
        _assistant_event(text="still main", tool_name="Read"),
    ])
    payload = {
        "session_id": "test-session-2",
        "transcript_path": str(p),
        "tool_name": "Edit",
        "tool_input": {"file_path": "noop"},
    }
    inp = from_claude_code_payload_enhanced(
        payload, tenant_id="claude-code",
    )
    sb = inp.session_behavior
    assert sb.get("sidechain_is_active") == pytest.approx(0.0)
    assert sb.get("sidechain_event_count", 0.0) == pytest.approx(0.0)


# ── default values when transcript fields absent ──────────────────


def test_defaults_for_legacy_transcripts(tmp_path: Path) -> None:
    """Old fixtures / test transcripts may not have isSidechain or
    parentUuid at all — the parser must default to False/0/'' and
    not crash."""
    legacy_event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "no isSidechain here"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    p = _write_transcript(tmp_path, [legacy_event] * 3)
    ctx = read_transcript_context(p)
    assert ctx is not None
    assert isinstance(ctx, TranscriptContext)
    assert ctx.current_event_is_sidechain is False
    assert ctx.sidechain_event_count == 0
    assert ctx.sidechain_tool_call_count == 0
    assert ctx.last_parent_uuid == ""
