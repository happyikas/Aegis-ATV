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


# ── PR 3: RAG block in Haiku user message ─────────────────────────────


class TestHaikuRAGUserMessage:
    """Verifies _build_user_message appends a corpus retrieval block when
    RAG is enabled and the corpus has chunks."""

    def test_no_rag_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings
        from aegis.judge.haiku import _build_user_message
        object.__setattr__(settings, "aegis_rag_enabled", False)
        out = _build_user_message("test summary")
        assert out == "test summary"

    def test_rag_block_appended_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings
        from aegis.judge.haiku import _build_user_message
        from aegis.judge.rag_corpus import reset_corpus_cache
        from aegis.judge.rag_retrieval import reset_index_cache
        object.__setattr__(settings, "aegis_rag_enabled", True)
        reset_corpus_cache()
        reset_index_cache()
        out = _build_user_message("force-push to main is dangerous")
        # The block adds a section header and at least one [rule]/[playbook] entry.
        assert "## Relevant policy / incident context" in out
        assert "[rule]" in out or "[playbook]" in out
        assert out.startswith("force-push to main is dangerous")

    def test_failsoft_returns_bare_summary(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.judge import haiku
        # Patch retrieve_block to raise; the wrapper must swallow.
        monkeypatch.setattr(
            "aegis.judge.rag_retrieval.retrieve_block",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        out = haiku._build_user_message("simple summary")
        assert out == "simple summary"


@respx.mock
def test_haiku_evaluate_sends_rag_augmented_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: HaikuJudge.evaluate() should put the RAG block in the
    user message when aegis_rag_enabled is True."""
    from aegis.config import settings
    from aegis.judge.haiku import HaikuJudge
    from aegis.judge.rag_corpus import reset_corpus_cache
    from aegis.judge.rag_retrieval import reset_index_cache
    object.__setattr__(settings, "aegis_rag_enabled", True)
    reset_corpus_cache()
    reset_index_cache()

    body = json.dumps({"decision": "ALLOW", "confidence": 0.5, "reason": "ok"})
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_anthropic_response(body))

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_record)
    HaikuJudge().evaluate("force-push pattern observed")

    msgs = captured["body"]["messages"]  # type: ignore[index]
    assert msgs[0]["role"] == "user"
    user_text = msgs[0]["content"]
    assert "force-push pattern observed" in user_text
    assert "## Relevant policy / incident context" in user_text


@respx.mock
def test_haiku_evaluate_no_rag_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis.config import settings
    from aegis.judge.haiku import HaikuJudge
    object.__setattr__(settings, "aegis_rag_enabled", False)

    body = json.dumps({"decision": "ALLOW", "confidence": 0.5, "reason": "ok"})
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_anthropic_response(body))

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_record)
    HaikuJudge().evaluate("simple summary")

    msgs = captured["body"]["messages"]  # type: ignore[index]
    user_text = msgs[0]["content"]
    assert user_text == "simple summary"
    assert "## Relevant policy" not in user_text


# ── PR 3: rag_retrieval honours aegis_rag_enabled ─────────────────────


def test_retrieve_block_disabled_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis.config import settings
    from aegis.judge.rag_retrieval import retrieve_block
    object.__setattr__(settings, "aegis_rag_enabled", False)
    assert retrieve_block("anything") == ""
