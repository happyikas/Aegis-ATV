"""Unit tests for tools/hooks/session_end.py (D6)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools" / "hooks"))

import session_end  # noqa: E402,I001


def _run(stdin_payload: str | None) -> dict:
    stdin = io.StringIO(stdin_payload) if stdin_payload is not None else io.StringIO("")
    stdout = io.StringIO()
    rc = session_end.handle_session_end(stdin=stdin, stdout=stdout)
    assert rc == 0
    return json.loads(stdout.getvalue())


def test_handle_session_end_skips_when_no_transcript() -> None:
    """No transcript path → cost-import skipped, retrospective still
    written (with zero-fill from missing audit + missing transcript)."""
    out = _run(json.dumps({"session_id": "s1", "transcript_path": ""}))
    env = out["_aegis"]
    assert env["cost"] == {"transcript": "skipped"}
    # PR #2 retrospective always runs (graceful degradation).
    assert env["retrospective"] == "written"


def test_handle_session_end_skips_when_transcript_missing(tmp_path: Path) -> None:
    out = _run(
        json.dumps(
            {"session_id": "s1", "transcript_path": str(tmp_path / "absent.jsonl")}
        )
    )
    env = out["_aegis"]
    assert env["cost"] == {"transcript": "skipped"}
    assert env["retrospective"] == "written"


def test_handle_session_end_imports_when_transcript_exists(tmp_path: Path) -> None:
    p = tmp_path / "tr.jsonl"
    p.write_text(
        json.dumps(
            {
                "type": "assistant_message",
                "message": {
                    "model": "haiku",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        + "\n"
    )
    out = _run(json.dumps({"session_id": "s2", "transcript_path": str(p)}))
    env = out["_aegis"]
    cost = env["cost"]
    assert cost["status"] == "imported"
    assert cost["turns"] == 1
    # Retrospective fires too with zero-fill on the audit side.
    assert env["retrospective"] == "written"


def test_handle_session_end_swallows_invalid_stdin(tmp_path: Path) -> None:
    out = _run("not json at all")
    env = out["_aegis"]
    assert env["cost"] == {"transcript": "skipped"}
    assert env["retrospective"] == "written"


def test_handle_session_end_swallows_empty_stdin() -> None:
    out = _run("")
    env = out["_aegis"]
    assert env["cost"] == {"transcript": "skipped"}
    assert env["retrospective"] == "written"


def test_handle_session_end_reports_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "tr.jsonl"
    p.write_text("{}\n")

    def boom(*_a: object, **_kw: object) -> dict[str, object]:
        raise RuntimeError("ledger offline")

    import aegis.cost.transcript as tr_mod

    monkeypatch.setattr(tr_mod, "import_into_wal", boom)
    out = _run(json.dumps({"session_id": "s3", "transcript_path": str(p)}))
    env = out["_aegis"]
    cost = env["cost"]
    assert cost["transcript"] == "error"
    assert "ledger offline" in cost["error"]
    # Retrospective should still succeed even if cost-import path failed —
    # they're independent.
    assert env["retrospective"] == "written"
