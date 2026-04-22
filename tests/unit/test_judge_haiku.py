"""Tests for HaikuJudge — Anthropic API mocked via respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx


@pytest.fixture(autouse=True)
def _set_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _anthropic_response(text: str) -> dict[str, object]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


@respx.mock
def test_haiku_parses_clean_json_block() -> None:
    from aegis.judge.haiku import HaikuJudge

    body = json.dumps({"decision": "BLOCK", "confidence": 0.92, "reason": "obvious exfil"})
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_anthropic_response(body))
    )
    v = HaikuJudge().evaluate("any summary")
    assert v.decision == "BLOCK"
    assert v.confidence == pytest.approx(0.92)
    assert "exfil" in v.reason
    # No attribution in this response → empty dict (M13 default)
    assert v.subfield_attribution == {}


@respx.mock
def test_haiku_parses_attribution_head() -> None:
    """M13: Haiku may emit per-subfield contribution scores."""
    from aegis.judge.haiku import HaikuJudge

    body = json.dumps({
        "decision": "BLOCK",
        "confidence": 0.95,
        "reason": "destructive shell pattern",
        "attribution": {
            "tool_arg_inspection": 0.9,
            "action_blast_radius": 0.7,
            "agent_state_embedding": 0.1,
        },
    })
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_anthropic_response(body))
    )
    v = HaikuJudge().evaluate("?")
    assert v.subfield_attribution == {
        "tool_arg_inspection": 0.9,
        "action_blast_radius": 0.7,
        "agent_state_embedding": 0.1,
    }


@respx.mock
def test_haiku_clamps_attribution_scores_to_unit_interval() -> None:
    from aegis.judge.haiku import HaikuJudge

    body = json.dumps({
        "decision": "ALLOW",
        "confidence": 0.8,
        "reason": "ok",
        "attribution": {
            "tool_arg_inspection": 1.5,    # above 1.0 → clamp to 1.0
            "action_history": -0.2,        # below 0.0 → clamp to 0.0
            "garbage_string": "not a number",  # ignored
        },
    })
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_anthropic_response(body))
    )
    v = HaikuJudge().evaluate("?")
    assert v.subfield_attribution == {
        "tool_arg_inspection": 1.0,
        "action_history": 0.0,
    }


@respx.mock
def test_haiku_strips_prose_around_json() -> None:
    from aegis.judge.haiku import HaikuJudge

    body = (
        'sure, here is my judgement:\n'
        '{"decision":"ALLOW","confidence":0.7,"reason":"routine"}\n'
        'thanks.'
    )
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_anthropic_response(body))
    )
    v = HaikuJudge().evaluate("ok?")
    assert v.decision == "ALLOW"
    assert v.confidence == pytest.approx(0.7)


@respx.mock
def test_haiku_unparseable_response_yields_approval() -> None:
    from aegis.judge.haiku import HaikuJudge

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_anthropic_response("totally not json"))
    )
    v = HaikuJudge().evaluate("?")
    assert v.decision == "REQUIRE_APPROVAL"
    assert v.confidence == 0.0


@respx.mock
def test_haiku_invalid_decision_value_falls_back_to_approval() -> None:
    from aegis.judge.haiku import HaikuJudge

    body = json.dumps({"decision": "MAYBE", "confidence": 0.5})
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_anthropic_response(body))
    )
    v = HaikuJudge().evaluate("?")
    assert v.decision == "REQUIRE_APPROVAL"


def test_dummy_judge_blocks_on_keyword() -> None:
    from aegis.judge.dummy import DummyJudge

    v = DummyJudge().evaluate("Tool: x\nArgs: please transfer 1 BTC")
    assert v.decision == "BLOCK"
    assert "transfer" in v.reason.lower()


def test_dummy_judge_approval_on_high_impact_tool() -> None:
    from aegis.judge.dummy import DummyJudge

    v = DummyJudge().evaluate("Tool: execute_shell\nArgs: ls")
    assert v.decision == "REQUIRE_APPROVAL"


def test_dummy_judge_allows_routine() -> None:
    from aegis.judge.dummy import DummyJudge

    v = DummyJudge().evaluate("Tool: read_file\nArgs: ./data/x.txt")
    assert v.decision == "ALLOW"


def test_get_judge_returns_dummy_in_test_env() -> None:
    from aegis.judge import get_judge
    from aegis.judge.dummy import DummyJudge

    assert isinstance(get_judge(), DummyJudge)
