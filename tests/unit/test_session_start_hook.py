"""Unit tests for tools/hooks/session_start.py (Sprint 1 PR4)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools" / "hooks"))

import session_start  # noqa: E402,I001


def _run(stdin_payload: str | None = None) -> tuple[dict, str]:
    """Drive the hook with a fake stdin/stdout/stderr trio.

    Returns ``(parsed_json_response, captured_stderr)``.
    """
    stdin = io.StringIO(stdin_payload) if stdin_payload is not None else io.StringIO("")
    stdout = io.StringIO()
    # session_start uses sys.stderr directly via _emit; capture via monkeypatch.
    rc = session_start.handle_session_start(stdin=stdin, stdout=stdout)
    assert rc == 0
    return json.loads(stdout.getvalue()), ""


def test_first_session_prints_welcome_and_creates_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """First session ever → welcome shown to stderr + marker file created."""
    marker = tmp_path / ".aegis" / ".welcomed"
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", marker)
    monkeypatch.setattr(session_start, "DISABLE", False)

    response, _ = _run(json.dumps(
        {"hook_event_name": "SessionStart", "session_id": "first"}
    ))
    captured = capsys.readouterr()

    assert response["_aegis"]["welcome"] == "shown"
    assert marker.exists()
    # Welcome content present
    assert "Aegis is active" in captured.err
    assert "/aegis-help" in captured.err
    assert "audit-key init" in captured.err


def test_second_session_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Marker already exists → silent on stderr, JSON ack 'returning'."""
    marker = tmp_path / ".aegis" / ".welcomed"
    marker.parent.mkdir(parents=True)
    marker.touch()
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", marker)
    monkeypatch.setattr(session_start, "DISABLE", False)

    response, _ = _run(json.dumps(
        {"hook_event_name": "SessionStart", "session_id": "second"}
    ))
    captured = capsys.readouterr()

    assert response["_aegis"]["welcome"] == "returning"
    assert captured.err == ""  # silent for returning users


def test_disable_env_var_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AEGIS_WELCOME_DISABLE=1 → never print, marker untouched."""
    marker = tmp_path / ".aegis" / ".welcomed"
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", marker)
    monkeypatch.setattr(session_start, "DISABLE", True)

    response, _ = _run(json.dumps(
        {"hook_event_name": "SessionStart", "session_id": "disabled"}
    ))
    captured = capsys.readouterr()

    assert response["_aegis"]["welcome"] == "disabled"
    assert captured.err == ""
    # Even on a first install the marker is NOT created when disabled —
    # so the user can re-enable later and still see the welcome once.
    assert not marker.exists()


def test_wrong_hook_event_name_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If Claude Code routes some other event to this script (defensive
    coding), don't print the welcome and don't touch the marker."""
    marker = tmp_path / ".aegis" / ".welcomed"
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", marker)
    monkeypatch.setattr(session_start, "DISABLE", False)

    response, _ = _run(json.dumps(
        {"hook_event_name": "SomeOtherEvent", "session_id": "x"}
    ))
    captured = capsys.readouterr()

    assert response["_aegis"]["welcome"] == "skipped"
    assert captured.err == ""
    assert not marker.exists()


def test_malformed_stdin_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage stdin → still exit 0 (never block the session). Treats
    the call as if Claude Code routed a SessionStart with no body."""
    marker = tmp_path / ".aegis" / ".welcomed"
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", marker)
    monkeypatch.setattr(session_start, "DISABLE", False)

    response, _ = _run("this is not json {{{")
    assert response["_aegis"]["welcome"] in ("shown", "returning")


def test_empty_stdin_treats_as_first_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty stdin (no event payload) — be permissive and assume
    SessionStart, since some Claude Code versions may not include a
    payload at all on this hook."""
    marker = tmp_path / ".aegis" / ".welcomed"
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", marker)
    monkeypatch.setattr(session_start, "DISABLE", False)

    response, _ = _run("")
    captured = capsys.readouterr()
    assert response["_aegis"]["welcome"] == "shown"
    assert "Aegis is active" in captured.err


def test_marker_creation_failure_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only filesystem (or absent HOME) — welcome still printed,
    hook exits 0."""
    bad_marker = Path("/nonexistent/cannot-create/.welcomed")
    monkeypatch.setattr(session_start, "WELCOMED_MARKER", bad_marker)
    monkeypatch.setattr(session_start, "DISABLE", False)

    response, _ = _run(json.dumps({"hook_event_name": "SessionStart"}))
    # Still considered "shown" — printing happened, marker write failed
    # silently per the best-effort contract.
    assert response["_aegis"]["welcome"] == "shown"
