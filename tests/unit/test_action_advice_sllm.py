"""v0.5.9 PR ⑫ — ActionAdvice sLLM brain (PR-ζ-head).

Covers the sLLM composer end-to-end with stub LLM callables (no
network / no GGUF dependency). Verifies:

* Heuristic baseline always produces an ALLOW-shaped advice when
  no anomalies are present.
* sLLM enhancement refines `reason` / `next_action_hint` /
  `alternative_tool` when the model returns parseable JSON.
* Verdict-class fields (decision, confidence, cited_anomalies,
  cited_turns_rel, recommended_advisors) stay heuristic — the
  sLLM cannot lift a BLOCK to ALLOW.
* Robust parsing across response shapes: plain JSON, markdown
  fence, prose-wrapped JSON, partial fields.
* Defensive fallback: malformed JSON / no LLM / type errors all
  return the heuristic baseline silently.
* Umbrella `compose_advice` switches on env or explicit kwarg.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aegis.judge.action_advice import ActionAdvice, compose_advice_heuristic
from aegis.judge.action_advice_sllm import (
    _build_prompt,
    _extract_json_blob,
    _parse_sllm_response,
    compose_advice,
    compose_advice_sllm,
)

# ── helpers ────────────────────────────────────────────────────────


def _baseline() -> ActionAdvice:
    """Standard heuristic baseline for these tests."""
    return compose_advice_heuristic(
        base_decision="BLOCK",
        base_reason="rule:dangerous_pattern matched",
        current_tool="Bash",
    )


def _stub_returning(payload: str) -> Callable[[str], str]:
    """Build a fake llm_call that ignores prompt and returns ``payload``."""
    def _stub(prompt: str) -> str:
        return payload
    return _stub


# ── prompt builder ─────────────────────────────────────────────────


def test_build_prompt_includes_baseline_fields() -> None:
    """The prompt must surface every field the LLM needs to reason
    about — decision, reason, anomalies, current tool."""
    baseline = _baseline()
    prompt = _build_prompt(baseline, current_tool="Bash")
    assert "BLOCK" in prompt
    assert "dangerous_pattern" in prompt
    assert "Bash" in prompt
    assert "JSON" in prompt   # output format hint


def test_build_prompt_handles_empty_anomalies() -> None:
    """Baseline with no anomalies should still produce a coherent
    prompt — 'none' fills the cited_anomalies / cited_turns_rel
    slots."""
    advice = compose_advice_heuristic(base_decision="ALLOW")
    prompt = _build_prompt(advice)
    assert "none" in prompt


# ── _extract_json_blob ─────────────────────────────────────────────


def test_extract_json_plain() -> None:
    blob = _extract_json_blob('{"reason": "x"}')
    assert blob == '{"reason": "x"}'


def test_extract_json_from_markdown_fence() -> None:
    text = '```json\n{"reason": "fenced"}\n```'
    blob = _extract_json_blob(text)
    assert blob == '{"reason": "fenced"}'


def test_extract_json_from_markdown_fence_no_lang_tag() -> None:
    text = '```\n{"reason": "no-lang"}\n```'
    blob = _extract_json_blob(text)
    assert blob == '{"reason": "no-lang"}'


def test_extract_json_with_leading_prose() -> None:
    text = "Here is my analysis:\n{\"reason\": \"with-prose\"}\n"
    blob = _extract_json_blob(text)
    assert blob == '{"reason": "with-prose"}'


def test_extract_json_with_nested_braces() -> None:
    """Balanced-brace walker must handle nested objects."""
    text = '{"a": {"b": 1}, "c": [1, 2]}'
    blob = _extract_json_blob(text)
    assert blob == text


def test_extract_json_returns_none_on_empty() -> None:
    assert _extract_json_blob("") is None
    assert _extract_json_blob("no json here") is None


# ── _parse_sllm_response ───────────────────────────────────────────


def test_parse_response_refines_all_three_fields() -> None:
    baseline = _baseline()
    response = (
        '{"reason": "new reason", '
        '"next_action_hint": "do X", '
        '"alternative_tool": "Read"}'
    )
    refined, used = _parse_sllm_response(response, baseline=baseline)
    assert used is True
    assert refined.advisor_kind == "sllm"
    assert refined.reason == "new reason"
    assert refined.next_action_hint == "do X"
    assert refined.alternative_tool == "Read"
    # Decision-class fields are unchanged.
    assert refined.decision == baseline.decision
    assert refined.confidence == baseline.confidence
    assert refined.cited_anomalies == baseline.cited_anomalies


def test_parse_response_partial_fields() -> None:
    """LLM returns only one prose field — others stay heuristic."""
    baseline = _baseline()
    response = '{"reason": "polished reason"}'
    refined, used = _parse_sllm_response(response, baseline=baseline)
    assert used is True
    assert refined.reason == "polished reason"
    # Hint not overridden.
    assert refined.next_action_hint == baseline.next_action_hint


def test_parse_response_null_string_treated_as_absent() -> None:
    """A literal 'null' string for a prose field is treated as not-
    provided, NOT as the literal string."""
    baseline = _baseline()
    response = (
        '{"reason": "ok", '
        '"alternative_tool": "null"}'
    )
    refined, used = _parse_sllm_response(response, baseline=baseline)
    assert used is True
    # alternative_tool was 'null' string → falls through to baseline value.
    assert refined.alternative_tool == baseline.alternative_tool


def test_parse_response_returns_baseline_on_invalid_json() -> None:
    baseline = _baseline()
    refined, used = _parse_sllm_response(
        "not valid json {broken", baseline=baseline,
    )
    assert used is False
    assert refined is baseline


def test_parse_response_returns_baseline_on_non_object() -> None:
    """JSON arrays / scalars / strings are not valid ActionAdvice
    payloads — fall back to baseline."""
    baseline = _baseline()
    for bad in ["[1, 2, 3]", "\"just a string\"", "42", "null"]:
        refined, used = _parse_sllm_response(bad, baseline=baseline)
        assert used is False, f"unexpected use for: {bad}"


def test_parse_response_returns_baseline_on_none() -> None:
    baseline = _baseline()
    refined, used = _parse_sllm_response(None, baseline=baseline)
    assert used is False
    assert refined is baseline


def test_parse_response_returns_baseline_on_no_useful_change() -> None:
    """If every field in the LLM response matches the baseline,
    we treat that as a no-op and return the heuristic baseline."""
    baseline = _baseline()
    response = (
        f'{{"reason": "{baseline.reason}"}}'
    )
    refined, used = _parse_sllm_response(response, baseline=baseline)
    assert used is False


def test_parse_response_clamps_long_strings() -> None:
    """Defensive: a runaway LLM that returns a 10k-char string
    must not crash + we truncate to the documented max length."""
    baseline = _baseline()
    huge = "x" * 5000
    response = '{"reason": "' + huge + '"}'
    refined, used = _parse_sllm_response(response, baseline=baseline)
    assert used is True
    assert len(refined.reason) <= 400


# ── compose_advice_sllm (end-to-end) ───────────────────────────────


def test_compose_advice_sllm_uses_llm_when_returns_json() -> None:
    advice = compose_advice_sllm(
        llm_call=_stub_returning(
            '{"reason": "refined", "next_action_hint": "do thing"}'
        ),
        base_decision="BLOCK",
        base_reason="rule fired",
        current_tool="Bash",
    )
    assert advice.advisor_kind == "sllm"
    assert advice.reason == "refined"
    assert advice.next_action_hint == "do thing"
    assert advice.decision == "BLOCK"


def test_compose_advice_sllm_falls_back_when_llm_returns_none() -> None:
    advice = compose_advice_sllm(
        llm_call=lambda p: None,
        base_decision="BLOCK",
        base_reason="rule fired",
    )
    assert advice.advisor_kind == "heuristic"


def test_compose_advice_sllm_falls_back_when_llm_raises() -> None:
    """An exception inside the LLM call must NOT propagate — the
    advisor sits on the firewall hot path."""
    def boom(prompt: str) -> str:
        raise RuntimeError("simulated llm failure")
    advice = compose_advice_sllm(
        llm_call=boom,
        base_decision="BLOCK",
        base_reason="rule fired",
    )
    assert advice.advisor_kind == "heuristic"


def test_compose_advice_sllm_does_not_change_decision() -> None:
    """The sLLM MUST NOT lift a BLOCK to ALLOW even if it tried —
    decision class stays heuristic-determined."""
    advice = compose_advice_sllm(
        llm_call=_stub_returning(
            # LLM tries to flip the verdict (a real model wouldn't,
            # but a prompt-injection attack might). The parser
            # ignores `decision` entirely.
            '{"decision": "ALLOW", "reason": "all good"}'
        ),
        base_decision="BLOCK",
        base_reason="rule fired",
    )
    assert advice.decision == "BLOCK"


def test_compose_advice_sllm_default_llm_call_with_dummy_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AEGIS_JUDGE_PROVIDER=dummy, the default LLM call returns
    None → composer returns heuristic baseline."""
    monkeypatch.setenv("AEGIS_JUDGE_PROVIDER", "dummy")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_JUDGE_MODEL_PATH", raising=False)
    advice = compose_advice_sllm(
        base_decision="BLOCK",
        base_reason="rule fired",
    )
    assert advice.advisor_kind == "heuristic"


# ── compose_advice (umbrella) ──────────────────────────────────────


def test_compose_advice_default_is_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default — no env var, no prefer_sllm → heuristic path
    (preserves v0.5.8 behavior byte-for-byte)."""
    monkeypatch.delenv("AEGIS_ACTION_ADVICE_PROVIDER", raising=False)
    advice = compose_advice(base_decision="ALLOW")
    assert advice.advisor_kind == "heuristic"


def test_compose_advice_env_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AEGIS_ACTION_ADVICE_PROVIDER=sllm` flips the umbrella to
    the sLLM composer. With a stub LLM, the result carries
    advisor_kind="sllm"."""
    monkeypatch.setenv("AEGIS_ACTION_ADVICE_PROVIDER", "sllm")
    advice = compose_advice(
        llm_call=_stub_returning('{"reason": "envrouted"}'),
        base_decision="BLOCK",
    )
    assert advice.advisor_kind == "sllm"
    assert advice.reason == "envrouted"


def test_compose_advice_explicit_kwarg_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`prefer_sllm=False` kwarg beats env=sllm."""
    monkeypatch.setenv("AEGIS_ACTION_ADVICE_PROVIDER", "sllm")
    advice = compose_advice(
        prefer_sllm=False,
        llm_call=_stub_returning('{"reason": "should not be used"}'),
        base_decision="ALLOW",
    )
    assert advice.advisor_kind == "heuristic"


def test_compose_advice_unknown_env_value_is_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_ACTION_ADVICE_PROVIDER", "magic")
    advice = compose_advice(base_decision="ALLOW")
    assert advice.advisor_kind == "heuristic"


# ── audit shape preservation ───────────────────────────────────────


def test_sllm_advice_preserves_audit_fields() -> None:
    """sLLM-enhanced advice must still carry the citation fields
    that audit / replay depend on."""
    baseline = compose_advice_heuristic(
        base_decision="BLOCK",
        base_reason="rule:foo matched",
        current_tool="Bash",
    )
    refined = compose_advice_sllm(
        llm_call=_stub_returning('{"reason": "new"}'),
        base_decision="BLOCK",
        base_reason="rule:foo matched",
        current_tool="Bash",
    )
    # Citation fields preserved verbatim.
    assert refined.cited_anomalies == baseline.cited_anomalies
    assert refined.cited_turns_rel == baseline.cited_turns_rel
    assert refined.recommended_advisors == baseline.recommended_advisors
    # advisor_hash changed (sLLM hash vs heuristic hash).
    assert refined.advisor_hash != baseline.advisor_hash


def test_sllm_advice_produced_at_is_recent() -> None:
    """`produced_at_ns` should be set on every advice, sLLM or
    heuristic — audit needs the timestamp for replay."""
    import time
    before = time.time_ns()
    advice = compose_advice_sllm(
        llm_call=_stub_returning('{"reason": "x"}'),
        base_decision="ALLOW",
    )
    after = time.time_ns()
    assert before <= advice.produced_at_ns <= after
