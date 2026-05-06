"""Tests for ``aegis.judge.advisor`` — sLLM-backed ActionAdvice
composer (PR-ζ-head, Phase C finale).

The tests cover three surfaces:

* :class:`DummyAdvisor` — must be a drop-in for the heuristic composer
* :class:`HaikuAdvisor` — Anthropic API mocked via respx; covers the
  parse / fall-through / API-error paths
* :func:`get_advisor` — env-driven dispatch, including the missing-key
  fallback policy

The fixtures mirror the ones in :mod:`tests.integration.test_action_advice`
so the two suites are easy to compare.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from aegis.atv.temporal import ATVSnapshot, TemporalContext
from aegis.burnin.anomaly import AnomalyTag
from aegis.judge.action_advice import ActionAdvice
from aegis.judge.advisor import (
    ADVISOR_PROMPT_HASH,
    ADVISOR_SYSTEM_PROMPT,
    Advisor,
    DummyAdvisor,
    HaikuAdvisor,
    compose_advice_sllm,
    get_advisor,
)

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _mk_tag(
    metric: str,
    severity: str = "warning",
    z: float = 2.5,
) -> AnomalyTag:
    from typing import cast

    from aegis.burnin.anomaly import Severity

    return AnomalyTag(
        metric=metric,
        severity=cast(Severity, severity),
        observed=10.0,
        baseline_mean=1.0,
        baseline_std=1.0,
        z_score=z,
        description=f"{metric} = 10 ({z:.1f}σ above baseline)",
    )


def _mk_temporal(
    *,
    n_history: int = 4,
    flagged_turns: list[int] | None = None,
) -> TemporalContext:
    flagged = set(flagged_turns or [])
    snaps: list[ATVSnapshot] = []
    for i in range(n_history):
        rel = i - (n_history - 1)
        snaps.append(
            ATVSnapshot(
                turn_index_rel=rel,
                ts_ns=0,
                tool_name="Read",
                args_excerpt="",
                decision="ALLOW",
                outcome="success",
                backtrack=(rel in flagged),
                redundant=False,
                is_error=False,
            )
        )
    return TemporalContext(
        history=tuple(snaps),
        window_size=n_history,
        cumulative_token_trajectory=tuple(0 for _ in range(n_history)),
        cache_hit_rate_trajectory=tuple(0.0 for _ in range(n_history)),
        n_backtracks=len(flagged),
        n_redundant=0,
        n_errors=0,
        n_failures=0,
        cache_hit_rate_max_drop_pp=0.0,
        token_velocity_per_turn=0.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Read",),
    )


def _anthropic_response(text: str) -> dict[str, object]:
    """Shape that mirrors the real Anthropic /v1/messages response.
    Matches the helper used in :mod:`tests.unit.test_judge_haiku`."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


# ──────────────────────────────────────────────────────────────────────
# Prompt hash + module surface
# ──────────────────────────────────────────────────────────────────────


class TestPromptHash:
    def test_prompt_hash_is_64_hex(self) -> None:
        assert len(ADVISOR_PROMPT_HASH) == 64
        int(ADVISOR_PROMPT_HASH, 16)  # parses as hex

    def test_system_prompt_mentions_decision_options(self) -> None:
        # All four decision options must be in the prompt so the model
        # knows DEFER is on the table.
        for d in ("ALLOW", "BLOCK", "REQUIRE_APPROVAL", "DEFER"):
            assert d in ADVISOR_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────────────────
# DummyAdvisor — heuristic equivalence
# ──────────────────────────────────────────────────────────────────────


class TestDummyAdvisor:
    def test_returns_action_advice(self) -> None:
        advice = DummyAdvisor().advise(base_decision="ALLOW")
        assert isinstance(advice, ActionAdvice)
        assert advice.advisor_kind == "heuristic"

    def test_passes_anomalies_to_heuristic(self) -> None:
        advice = DummyAdvisor().advise(
            anomalies=[
                _mk_tag("session_error_rate", "alert", z=3.5),
            ],
            base_decision="ALLOW",
        )
        # Alert anomaly → REQUIRE_APPROVAL via heuristic rules.
        assert advice.decision == "REQUIRE_APPROVAL"

    def test_extra_kwargs_ignored_safely(self) -> None:
        # baseline / catalog / intent_classifier / action_table are not
        # consumed by the heuristic composer, but the dummy advisor
        # must accept them without error.
        advice = DummyAdvisor().advise(
            baseline=None,
            catalog=None,
            intent_classifier=None,
            action_table=None,
            base_decision="ALLOW",
        )
        assert advice.decision == "ALLOW"


# ──────────────────────────────────────────────────────────────────────
# HaikuAdvisor — respx-mocked
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def _haiku_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a stub API key so HaikuAdvisor can be constructed without
    hitting the real env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


class TestHaikuAdvisor:
    @respx.mock
    def test_parses_clean_json(self, _haiku_env: None) -> None:
        body = json.dumps(
            {
                "decision": "BLOCK",
                "confidence": 0.92,
                "reason": "destructive bash with redundancy",
                "next_action_hint": "ask the user before retrying",
                "alternative_tool": "Read",
                "cited_anomalies": ["session_error_rate"],
                "cited_turns_rel": [-1, -2],
            }
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(
            temporal_ctx=_mk_temporal(),
            anomalies=[_mk_tag("session_error_rate", "alert", z=3.5)],
            base_decision="ALLOW",
            current_tool="Bash",
        )
        assert advice.decision == "BLOCK"
        assert advice.confidence == pytest.approx(0.92)
        assert advice.next_action_hint == "ask the user before retrying"
        assert advice.alternative_tool == "Read"
        assert advice.cited_anomalies == ("session_error_rate",)
        assert advice.cited_turns_rel == (-1, -2)
        assert advice.advisor_kind == "sllm-haiku"
        assert advice.advisor_hash == ADVISOR_PROMPT_HASH

    @respx.mock
    def test_strips_prose_around_json(self, _haiku_env: None) -> None:
        body = (
            "sure, here is my advice:\n"
            '{"decision":"ALLOW","confidence":0.7,"reason":"routine"}\n'
            "thanks."
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        assert advice.decision == "ALLOW"
        assert advice.confidence == pytest.approx(0.7)

    @respx.mock
    def test_unparseable_falls_back_with_haiku_stamp(
        self, _haiku_env: None
    ) -> None:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response("definitely not json")
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        # Conservative: fallback escalates to REQUIRE_APPROVAL.
        assert advice.decision == "REQUIRE_APPROVAL"
        # Stamped as haiku — audit must see the call DID happen.
        assert advice.advisor_kind == "sllm-haiku"
        assert advice.advisor_hash == ADVISOR_PROMPT_HASH
        assert "unparseable" in advice.reason.lower()

    @respx.mock
    def test_invalid_decision_falls_back(self, _haiku_env: None) -> None:
        body = json.dumps({"decision": "MAYBE", "confidence": 0.5})
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        assert advice.decision == "REQUIRE_APPROVAL"
        assert advice.advisor_kind == "sllm-haiku"

    @respx.mock
    def test_api_error_falls_back_to_heuristic(
        self, _haiku_env: None
    ) -> None:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(503, json={"error": "down"})
        )
        advice = HaikuAdvisor().advise(
            anomalies=[_mk_tag("session_error_rate", "alert", z=3.5)],
            base_decision="ALLOW",
        )
        # API down → plain heuristic. advisor_kind reverts to heuristic
        # so audit can see the call did NOT reach the model.
        assert advice.advisor_kind == "heuristic"
        assert advice.decision == "REQUIRE_APPROVAL"  # alert anomaly

    @respx.mock
    def test_confidence_clamped_when_model_overflows(
        self, _haiku_env: None
    ) -> None:
        body = json.dumps(
            {
                "decision": "ALLOW",
                "confidence": 1.7,  # ActionAdvice.__post_init__ clamps
                "reason": "ok",
            }
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        assert advice.confidence == 1.0


# ──────────────────────────────────────────────────────────────────────
# get_advisor() — env dispatch
# ──────────────────────────────────────────────────────────────────────


class TestGetAdvisor:
    def test_default_is_dummy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AEGIS_ADVISOR_PROVIDER", raising=False)
        assert isinstance(get_advisor(), DummyAdvisor)

    def test_explicit_dummy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AEGIS_ADVISOR_PROVIDER", "dummy")
        assert isinstance(get_advisor(), DummyAdvisor)

    def test_haiku_with_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AEGIS_ADVISOR_PROVIDER", "haiku")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert isinstance(get_advisor(), HaikuAdvisor)

    def test_haiku_without_key_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AEGIS_ADVISOR_PROVIDER", "haiku")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert isinstance(get_advisor(), DummyAdvisor)

    def test_unknown_provider_falls_back_to_dummy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A typo in production env shouldn't crash the firewall.
        monkeypatch.setenv("AEGIS_ADVISOR_PROVIDER", "haku-typo")
        assert isinstance(get_advisor(), DummyAdvisor)


# ──────────────────────────────────────────────────────────────────────
# compose_advice_sllm() — public API
# ──────────────────────────────────────────────────────────────────────


class TestComposeAdviceSllm:
    def test_uses_dummy_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AEGIS_ADVISOR_PROVIDER", raising=False)
        advice = compose_advice_sllm(
            anomalies=[_mk_tag("session_error_rate", "alert", z=3.5)],
            base_decision="ALLOW",
        )
        assert advice.advisor_kind == "heuristic"
        assert advice.decision == "REQUIRE_APPROVAL"

    def test_advisor_injection_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even with env set to haiku, an injected advisor wins.
        monkeypatch.setenv("AEGIS_ADVISOR_PROVIDER", "haiku")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        captured: dict[str, object] = {}

        class _Capture:
            def advise(self, **kwargs: object) -> ActionAdvice:
                captured.update(kwargs)
                return ActionAdvice(
                    decision="ALLOW",
                    reason="captured",
                    confidence=0.5,
                    advisor_kind="heuristic",
                )

        advice = compose_advice_sllm(
            base_decision="ALLOW",
            current_tool="Bash",
            advisor=_Capture(),
        )
        assert advice.reason == "captured"
        assert captured["current_tool"] == "Bash"


# ──────────────────────────────────────────────────────────────────────
# User message assembly — sanity check on the layered prompt
# ──────────────────────────────────────────────────────────────────────


class TestUserMessageAssembly:
    @respx.mock
    def test_proposed_call_section_present(
        self, _haiku_env: None
    ) -> None:
        captured: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            captured["user_msg"] = payload["messages"][0]["content"]
            return httpx.Response(
                200,
                json=_anthropic_response(
                    json.dumps(
                        {
                            "decision": "ALLOW",
                            "confidence": 0.5,
                            "reason": "ok",
                        }
                    )
                ),
            )

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=_capture
        )
        HaikuAdvisor().advise(
            temporal_ctx=_mk_temporal(),
            base_decision="REQUIRE_APPROVAL",
            base_reason="loop detected",
            current_tool="Bash",
        )
        msg = captured["user_msg"]
        assert "PROPOSED CALL" in msg
        assert "Bash" in msg
        assert "REQUIRE_APPROVAL" in msg
        assert "loop detected" in msg

    @respx.mock
    def test_action_table_adds_alternatives_section(
        self, _haiku_env: None
    ) -> None:
        from aegis.burnin.action_embeddings import default_table

        captured: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            captured["user_msg"] = payload["messages"][0]["content"]
            return httpx.Response(
                200,
                json=_anthropic_response(
                    json.dumps(
                        {
                            "decision": "ALLOW",
                            "confidence": 0.5,
                            "reason": "ok",
                        }
                    )
                ),
            )

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=_capture
        )
        HaikuAdvisor().advise(
            temporal_ctx=_mk_temporal(),
            action_table=default_table(),
            base_decision="ALLOW",
            current_tool="Read",
        )
        assert "CANDIDATE ALTERNATIVES" in captured["user_msg"]


# ──────────────────────────────────────────────────────────────────────
# Protocol structural conformance
# ──────────────────────────────────────────────────────────────────────


class TestProtocolConformance:
    def test_dummy_satisfies_protocol(self) -> None:
        a: Advisor = DummyAdvisor()
        assert callable(a.advise)


# ──────────────────────────────────────────────────────────────────────
# v2.5.2 PR-ψ-multi-domain — recommended_advisors round-trip via Haiku
# ──────────────────────────────────────────────────────────────────────


class TestMultiDomainHaiku:
    @respx.mock
    def test_haiku_emits_recommended_advisors(
        self, _haiku_env: None
    ) -> None:
        body = json.dumps({
            "decision": "REQUIRE_APPROVAL",
            "confidence": 0.85,
            "reason": "cost +30%, cache collapsed, destructive path",
            "recommended_advisors": [
                {
                    "advisor": "cost-optimizer", "priority": "high",
                    "action": "review HW/SW divergence",
                    "reasoning": "ratio 3.1x",
                    "cited_signals": ["hw_vs_sw_divergence_ratio"],
                },
                {
                    "advisor": "kv-cache-optimizer", "priority": "high",
                    "action": "stabilise prompt prefix",
                    "cited_signals": ["cache_hit_rate_max_drop_pp"],
                },
                {
                    "advisor": "security-reviewer", "priority": "high",
                    "action": "block deletion until ACK",
                    "cited_signals": ["destructive_path_match"],
                },
            ],
        })
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(
            temporal_ctx=_mk_temporal(),
            current_tool="Bash",
            cost_signals={"hw_vs_sw_divergence_ratio": 3.1},
            cache_signals={"cache_hit_rate_max_drop_pp": 51.0},
            security_signals={
                "verdict_decision": "REQUIRE_APPROVAL",
                "destructive_path_match": True,
                "policy_rule": "rule:backup_path_destructive",
            },
        )
        assert advice.decision == "REQUIRE_APPROVAL"
        names = [r.advisor for r in advice.recommended_advisors]
        assert names == [
            "cost-optimizer", "kv-cache-optimizer", "security-reviewer",
        ]
        assert all(r.priority == "high" for r in advice.recommended_advisors)

    @respx.mock
    def test_haiku_drops_unknown_advisor_names(
        self, _haiku_env: None
    ) -> None:
        body = json.dumps({
            "decision": "ALLOW", "confidence": 0.7, "reason": "ok",
            "recommended_advisors": [
                {"advisor": "cost-optimizer", "priority": "low",
                 "action": "x"},
                {"advisor": "i-am-hallucinated", "priority": "high",
                 "action": "y"},
            ],
        })
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        names = [r.advisor for r in advice.recommended_advisors]
        assert names == ["cost-optimizer"]

    @respx.mock
    def test_user_message_includes_signal_sections(
        self, _haiku_env: None
    ) -> None:
        captured: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            captured["user_msg"] = payload["messages"][0]["content"]
            return httpx.Response(
                200,
                json=_anthropic_response(
                    json.dumps({
                        "decision": "ALLOW", "confidence": 0.5,
                        "reason": "ok", "recommended_advisors": [],
                    })
                ),
            )

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=_capture
        )
        HaikuAdvisor().advise(
            temporal_ctx=_mk_temporal(),
            current_tool="Bash",
            cost_signals={"hw_vs_sw_divergence_ratio": 3.0},
            cache_signals={"cache_hit_rate_max_drop_pp": 40.0},
            security_signals={
                "destructive_path_match": True,
                "policy_rule": "rule:git_destructive",
            },
        )
        msg = captured["user_msg"]
        assert "COST METRICS" in msg
        assert "KV CACHE METRICS" in msg
        assert "SECURITY SIGNALS" in msg


class TestDummyAdvisorMultiDomain:
    def test_dummy_passes_signals_through_to_heuristic(self) -> None:
        advice = DummyAdvisor().advise(
            base_decision="REQUIRE_APPROVAL",
            cost_signals={"hw_vs_sw_divergence_ratio": 3.0},
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "cost-optimizer" in names


# ──────────────────────────────────────────────────────────────────────
# v2.8 PR-β — Tier 3 sLLM action_steps[] round-trip
# ──────────────────────────────────────────────────────────────────────


class TestHaikuActionSteps:
    @respx.mock
    def test_haiku_emits_action_steps_with_concrete_params(
        self, _haiku_env: None
    ) -> None:
        """Tier 3 advisor surface: Haiku emits structured executable
        steps with concrete turn indices, model names, savings
        estimates. Each step round-trips through the parser intact."""
        body = json.dumps({
            "decision": "REQUIRE_APPROVAL",
            "confidence": 0.85,
            "reason": "cost 1.50x of budget; cache broke at turn -6",
            "recommended_advisors": [
                {
                    "advisor": "cost-optimizer",
                    "priority": "high",
                    "action": "Trim expensive turns or swap model",
                    "reasoning": "projected $1.50 = 150% of budget",
                    "cited_signals": ["budget_used_ratio",
                                      "cache_hit_rate_max_drop_pp"],
                    "action_steps": [
                        {
                            "verb": "prune-turns",
                            "parameters": {
                                "turn_indices_rel": [-6, -5, -4],
                                "saved_tokens_estimate": 14000,
                                "saved_dollars_estimate": 0.32,
                            },
                            "expected_impact":
                                "ratio 1.50 → 1.18; cache stabilises",
                            "confidence": 0.85,
                            "cited_signals": ["cache_hit_rate_max_drop_pp"],
                        },
                        {
                            "verb": "swap-model",
                            "parameters": {
                                "from_model": "claude-opus-4-7",
                                "to_model": "claude-haiku-4-5",
                                "ratio_savings": 3.0,
                            },
                            "expected_impact": "~3x cheaper for remainder",
                            "confidence": 0.6,
                            "cited_signals": ["budget_used_ratio"],
                        },
                    ],
                },
            ],
        })
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")

        assert len(advice.recommended_advisors) == 1
        rec = advice.recommended_advisors[0]
        assert rec.advisor == "cost-optimizer"
        assert len(rec.action_steps) == 2

        prune = rec.action_steps[0]
        assert prune.verb == "prune-turns"
        assert prune.parameters["turn_indices_rel"] == [-6, -5, -4]
        assert prune.parameters["saved_tokens_estimate"] == 14000
        assert prune.confidence == 0.85
        assert "cache_hit_rate_max_drop_pp" in prune.cited_signals

        swap = rec.action_steps[1]
        assert swap.verb == "swap-model"
        assert swap.parameters["from_model"] == "claude-opus-4-7"
        assert swap.parameters["ratio_savings"] == 3.0

    @respx.mock
    def test_haiku_unknown_verb_dropped_silently(
        self, _haiku_env: None
    ) -> None:
        """Hallucination defense: a step with verb='do-magic-fix' must
        be dropped without affecting other valid steps."""
        body = json.dumps({
            "decision": "ALLOW", "confidence": 0.6, "reason": "ok",
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "low",
                "action": "x",
                "action_steps": [
                    {"verb": "do-magic-fix", "parameters": {}},
                    {"verb": "end-session", "parameters": {}},
                    {"verb": "another-fake", "parameters": {"foo": 1}},
                ],
            }],
        })
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        steps = advice.recommended_advisors[0].action_steps
        verbs = [s.verb for s in steps]
        assert verbs == ["end-session"]

    @respx.mock
    def test_haiku_step_missing_required_param_dropped(
        self, _haiku_env: None
    ) -> None:
        """swap-model requires from_model + to_model; one without
        them must be silently dropped."""
        body = json.dumps({
            "decision": "ALLOW", "confidence": 0.6, "reason": "ok",
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "low",
                "action": "x",
                "action_steps": [
                    {"verb": "swap-model",
                     "parameters": {"from_model": "opus"}},
                    {"verb": "swap-model",
                     "parameters": {"from_model": "opus",
                                    "to_model": "haiku"}},
                ],
            }],
        })
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json=_anthropic_response(body)
            )
        )
        advice = HaikuAdvisor().advise(base_decision="ALLOW")
        steps = advice.recommended_advisors[0].action_steps
        assert len(steps) == 1
        assert steps[0].parameters["to_model"] == "haiku"


class TestPromptVersion:
    def test_prompt_hash_changed_for_v3(self) -> None:
        """ADVISOR_PROMPT_HASH must change when the prompt is
        revised. Audit replay relies on this to distinguish advice
        produced by v2 (no action_steps) vs v3 (with action_steps)."""
        # Minimal sanity: the hash is a 64-hex string and the system
        # prompt mentions the new closed verb catalog.
        assert len(ADVISOR_PROMPT_HASH) == 64
        for verb in (
            "prune-turns", "swap-model", "swap-tool", "end-session",
            "summarize-window", "narrow-scope", "clarify-intent",
            "run-diagnostic", "verify-state",
            "notify-operator", "require-approval",
        ):
            assert verb in ADVISOR_SYSTEM_PROMPT
