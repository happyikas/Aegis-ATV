"""Unit tests for src/aegis/cost/transcript.py (D5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aegis.cost import transcript as tr_mod


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_stream_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user_message"}\n'
        "\n"
        "this is not json\n"
        '{"type":"assistant_message"}\n'
    )
    out = list(tr_mod._stream(p))
    assert [r["type"] for r in out] == ["user_message", "assistant_message"]


def test_usage_of_extracts_tokens_and_cache() -> None:
    rec = {
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        },
    }
    out = tr_mod._usage_of(rec)
    assert out == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read": 10,
        "cache_creation": 5,
        "model": "claude-sonnet-4-6",
    }


def test_usage_of_returns_none_when_absent() -> None:
    assert tr_mod._usage_of({"type": "user_message"}) is None


def test_tool_uses_legacy_field() -> None:
    rec = {"message": {"tool_uses": [{"id": "toolu_1", "name": "Bash"}]}}
    out = tr_mod._tool_uses(rec)
    assert out[0]["id"] == "toolu_1"


def test_tool_uses_content_array() -> None:
    rec = {
        "message": {
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "toolu_2", "name": "Read"},
            ]
        }
    }
    out = tr_mod._tool_uses(rec)
    assert [t["id"] for t in out] == ["toolu_2"]


def test_parse_transcript_collects_assistant_turns(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "user_message"},
            {
                "type": "assistant_message",
                "message": {
                    "model": "claude-haiku",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "content": [{"type": "tool_use", "id": "toolu_a"}],
                },
            },
            {"type": "tool_result", "tool_use_id": "toolu_a"},
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 7,
                        "cache_creation_input_tokens": 3,
                    },
                },
            },
        ],
    )
    turns = tr_mod.parse_transcript(p)
    assert len(turns) == 2
    assert turns[0]["model"] == "claude-haiku"
    assert turns[0]["tool_use_ids"] == ["toolu_a"]
    assert turns[1]["model"] == "claude-sonnet"
    assert turns[1]["cache_creation"] == 3
    assert turns[1]["tool_use_ids"] == []


def test_import_into_wal_returns_no_transcript_when_missing(tmp_path: Path) -> None:
    out = tr_mod.import_into_wal(tmp_path / "nope.jsonl")
    assert out["status"] == "no_transcript"


def test_import_into_wal_default_writer_returns_counts(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant_message",
                "message": {
                    "model": "haiku",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [
                        {"type": "tool_use", "id": "toolu_x"},
                        {"type": "tool_use", "id": "toolu_y"},
                    ],
                },
            },
            {
                "type": "assistant_message",
                "message": {
                    "model": "haiku",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ],
    )
    out = tr_mod.import_into_wal(p)
    assert out["status"] == "imported"
    assert out["turns"] == 2
    assert out["tool_attributed"] == 2  # toolu_x + toolu_y
    assert out["aggregate"] == 1  # the second turn has no tool_uses
    assert out["total_usd"] == 0.0  # no-op writer


def test_ledger_writer_can_be_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant_message",
                "message": {
                    "model": "sonnet",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ],
    )

    def fake_writer(turns: list[dict[str, Any]], _sid: str) -> dict[str, Any]:
        return {"tool_attributed": 0, "aggregate": len(turns), "total_usd": 0.42}

    monkeypatch.setattr(tr_mod, "ledger_writer", fake_writer)
    out = tr_mod.import_into_wal(p, session_id="sess-1")
    assert out["total_usd"] == 0.42
    assert out["aggregate"] == 1


def test_public_export_is_available() -> None:
    from aegis import cost

    assert hasattr(cost, "parse_transcript")
    assert hasattr(cost, "import_into_wal")
