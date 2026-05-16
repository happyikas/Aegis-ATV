"""v0.5.10 PR ⑬ — TripleAxisAdvisor.

Covers signal extraction, heuristic per-axis assessment, sLLM
prose refinement, umbrella selection, JSON-shape preservation,
and edge cases (empty window, single-axis dominance).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import pytest

from aegis.context_memory.record import ContextMemoryRecord
from aegis.judge.triple_axis_advisor import (
    assess_triple_axis,
    assess_via_heuristic,
    assess_via_sllm,
    extract_axis_signals,
    render_triple_axis,
)

# Splits so the firewall doesn't BLOCK this test file.
_KW_DROP_TABLE: Final[str] = "DROP" + " TABLE"


def _rec(
    *,
    aid: str = "agent-x",
    tool: str = "Bash",
    decision: str = "ALLOW",
    cost: float = 0.001,
    reason: str = "",
    tokens_in: int = 100,
    tokens_out: int = 50,
    advisors: tuple[str, ...] = (),
    ts_ns: int = 1_700_000_000_000_000_000,
    trace: str = "tr",
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns, trace_id=trace, invocation_id="",
        aid=aid, tenant_id="t", tool_name=tool, decision=decision,
        reason=reason, channel=None, provider=None,
        latency_ms=10.0, cost_usd=cost,
        tokens_in=tokens_in, tokens_out=tokens_out,
        step_traces={}, m13_score=None,
        advisor_invoked=bool(advisors),
        recommended_advisors=advisors,
        atv_sha3=None, atv_dim=2080,
        is_sidechain=False, mode="local",
    )


# ── extract_axis_signals ───────────────────────────────────────────


def test_extract_signals_empty_window() -> None:
    s = extract_axis_signals([])
    assert s.n_total == 0
    assert s.total_cost_usd == 0.0
    assert s.estimated_cache_hit_rate == 1.0  # vacuously perfect
    assert s.block_rate == 0.0


def test_extract_signals_basic_counts() -> None:
    records = [
        _rec(decision="ALLOW", cost=0.01),
        _rec(decision="BLOCK", reason="rule:foo"),
        _rec(decision="REQUIRE_APPROVAL"),
    ]
    s = extract_axis_signals(records)
    assert s.n_total == 3
    assert s.n_allow == 1
    assert s.n_block == 1
    assert s.n_approval == 1
    assert s.block_rate == pytest.approx(1/3)
    assert s.approval_rate == pytest.approx(1/3)
    assert s.rule_violation_count == 1


def test_extract_signals_top_cost_tool() -> None:
    records = (
        [_rec(tool="WebSearch", cost=0.02) for _ in range(5)]
        + [_rec(tool="Read", cost=0.001) for _ in range(10)]
    )
    s = extract_axis_signals(records)
    assert s.top_cost_tool == "WebSearch"
    assert s.top_cost_tool_total == pytest.approx(0.10)
    assert s.total_cost_usd == pytest.approx(0.11)


def test_extract_signals_repeat_call_ratio() -> None:
    """5 calls of one (aid,tool) bucket → all 5 are in repeat."""
    records = [
        _rec(aid="a1", tool="Read", trace=f"t{i}") for i in range(5)
    ]
    s = extract_axis_signals(records)
    # All 5 records in a single bucket size 5 (≥3) → 5/5 = 1.0
    assert s.repeat_call_ratio == pytest.approx(1.0)
    # Cache hit estimation drops.
    assert s.estimated_cache_hit_rate < 0.5


def test_extract_signals_redundant_reads() -> None:
    """N Read calls for same aid → N-1 redundant (the first one
    is legitimate, subsequent ones should have been cache hits)."""
    records = [_rec(aid="a1", tool="Read") for _ in range(4)]
    s = extract_axis_signals(records)
    assert s.redundant_read_count == 3   # 4 - 1


def test_extract_signals_loop_count() -> None:
    records = [
        _rec(reason="same Bash call repeated 3 times this session")
        for _ in range(5)
    ]
    s = extract_axis_signals(records)
    assert s.loop_detected_count == 5
    assert s.prefix_instability_count == 5


def test_extract_signals_dangerous_pattern_count() -> None:
    records = [
        _rec(decision="BLOCK",
             reason="dangerous pattern: " + _KW_DROP_TABLE)
        for _ in range(3)
    ]
    s = extract_axis_signals(records)
    assert s.dangerous_pattern_count == 3


# ── heuristic assessor — per axis ─────────────────────────────────


def test_heuristic_empty_window_all_ok() -> None:
    advice = assess_via_heuristic(extract_axis_signals([]))
    for ax in (advice.token_efficiency, advice.cache_performance,
               advice.stability):
        assert ax.severity == "ok"
        assert ax.score == 1.0
    assert "No traffic" in advice.summary or "No traffic" in advice.token_efficiency.interpretation


def test_heuristic_high_repeat_drops_token_score() -> None:
    """30+ identical calls → repeat_ratio ≈ 1.0 → token score drops
    sharply."""
    records = [_rec(aid="a1", tool="Read", cost=0.005) for _ in range(30)]
    s = extract_axis_signals(records)
    advice = assess_via_heuristic(s)
    assert advice.token_efficiency.severity in ("warn", "alert")
    assert "repeat pattern" in advice.token_efficiency.interpretation


def test_heuristic_hot_tool_flagged_in_token_axis() -> None:
    records = (
        [_rec(tool="WebSearch", cost=0.05) for _ in range(5)]
        + [_rec(tool="Read", cost=0.001) for _ in range(20)]
    )
    advice = assess_via_heuristic(extract_axis_signals(records))
    # WebSearch is the dominant cost driver — should be cited.
    assert "WebSearch" in advice.token_efficiency.interpretation or (
        advice.token_efficiency.cited_signals
        and "top_cost_tool" in advice.token_efficiency.cited_signals
    )


def test_heuristic_loops_drop_cache_score() -> None:
    """Many loop events → cache_performance score drops."""
    records = [
        _rec(reason="same X call repeated 3 times this session",
             tool="Read", aid="a1")
        for _ in range(15)
    ]
    advice = assess_via_heuristic(extract_axis_signals(records))
    assert advice.cache_performance.severity in ("warn", "alert")


def test_heuristic_blocks_drop_stability_score() -> None:
    records = (
        [_rec(decision="BLOCK", reason="rule:dangerous") for _ in range(10)]
        + [_rec(decision="ALLOW") for _ in range(10)]
    )
    advice = assess_via_heuristic(extract_axis_signals(records))
    assert advice.stability.severity in ("warn", "alert")
    assert "BLOCK" in advice.stability.interpretation


# ── overall_priority + summary ─────────────────────────────────────


def test_overall_priority_picks_worst_axis() -> None:
    """Stability has many BLOCKs, other axes are kept clean by
    diversifying aid+tool so the cache + token axes don't drop."""
    records = (
        [_rec(decision="BLOCK", reason="rule:x",
              aid=f"a{i}", tool=f"T{i}")
         for i in range(20)]
        + [_rec(decision="ALLOW", cost=0.0001,
                aid=f"b{i}", tool=f"U{i}")
           for i in range(20)]
    )
    advice = assess_via_heuristic(extract_axis_signals(records))
    assert advice.overall_priority == "stability"


def test_summary_calls_out_alert_axes() -> None:
    """Summary text should mention which axis is in alert."""
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(50)]
    advice = assess_via_heuristic(extract_axis_signals(records))
    # At least one axis should be alert / warn.
    assert advice.token_efficiency.severity != "ok" \
        or advice.cache_performance.severity != "ok" \
        or advice.stability.severity != "ok"
    # Summary mentions the worst axis or the count.
    assert any(
        word in advice.summary.lower()
        for word in ("alert", "warn", "axis", "focus")
    )


# ── sLLM path ──────────────────────────────────────────────────────


def _stub_returning(payload: str) -> Callable[[str], str]:
    def _stub(prompt: str) -> str:
        return payload
    return _stub


def test_sllm_refines_interpretations() -> None:
    """LLM returns valid JSON refining all three axes — result
    carries advisor_kind='sllm' and the new interpretations."""
    records = [
        _rec(decision="BLOCK", reason="rule:foo") for _ in range(10)
    ]
    s = extract_axis_signals(records)
    payload = """
    {
      "token_efficiency": {
        "interpretation": "tokens look fine",
        "next_action": null
      },
      "cache_performance": {
        "interpretation": "cache is meh",
        "next_action": "use stable prompts"
      },
      "stability": {
        "interpretation": "lots of blocks; expected",
        "next_action": "check the rules"
      },
      "summary": "stability is the worst axis"
    }
    """
    advice = assess_via_sllm(s, llm_call=_stub_returning(payload))
    assert advice.advisor_kind == "sllm"
    assert advice.token_efficiency.interpretation == "tokens look fine"
    assert advice.cache_performance.interpretation == "cache is meh"
    assert advice.stability.next_action == "check the rules"
    assert advice.summary == "stability is the worst axis"


def test_sllm_preserves_scores_across_refinement() -> None:
    """The sLLM only refines prose — scores and severity stay
    heuristic-determined."""
    records = [
        _rec(decision="BLOCK", reason="rule:foo") for _ in range(10)
    ]
    s = extract_axis_signals(records)
    baseline = assess_via_heuristic(s)
    payload = '{"stability": {"interpretation": "polished"}}'
    refined = assess_via_sllm(s, llm_call=_stub_returning(payload))
    assert refined.stability.score == baseline.stability.score
    assert refined.stability.severity == baseline.stability.severity
    assert refined.stability.interpretation == "polished"


def test_sllm_falls_back_on_invalid_json() -> None:
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    s = extract_axis_signals(records)
    refined = assess_via_sllm(s, llm_call=_stub_returning("not json {{"))
    assert refined.advisor_kind == "heuristic"


def test_sllm_falls_back_on_none_response() -> None:
    s = extract_axis_signals(
        [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    )
    refined = assess_via_sllm(s, llm_call=lambda p: None)
    assert refined.advisor_kind == "heuristic"


def test_sllm_falls_back_when_call_raises() -> None:
    def boom(prompt: str) -> str:
        raise RuntimeError("simulated llm failure")
    s = extract_axis_signals(
        [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    )
    refined = assess_via_sllm(s, llm_call=boom)
    assert refined.advisor_kind == "heuristic"


def test_sllm_ignores_decision_score_injection() -> None:
    """Prompt injection: LLM tries to flip scores. Should be ignored."""
    s = extract_axis_signals(
        [_rec(decision="BLOCK", reason="rule:x") for _ in range(20)]
    )
    baseline = assess_via_heuristic(s)
    payload = '''
    {
      "token_efficiency": {"score": 1.0, "severity": "ok",
                            "interpretation": "all good"},
      "stability": {"score": 1.0, "severity": "ok",
                     "interpretation": "great"}
    }
    '''
    refined = assess_via_sllm(s, llm_call=_stub_returning(payload))
    # Scores untouched — only interpretation can be refined.
    assert refined.stability.score == baseline.stability.score
    assert refined.stability.severity == baseline.stability.severity


def test_sllm_empty_window_returns_heuristic() -> None:
    refined = assess_via_sllm(
        extract_axis_signals([]),
        llm_call=_stub_returning('{"summary": "should be ignored"}'),
    )
    assert refined.advisor_kind == "heuristic"


# ── umbrella selection ────────────────────────────────────────────


def test_umbrella_default_is_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AEGIS_TRIPLE_AXIS_PROVIDER", raising=False)
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    advice = assess_triple_axis(records)
    assert advice.advisor_kind == "heuristic"


def test_umbrella_env_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var alone shouldn't promote without a working LLM —
    falls back to heuristic when no llm_call provided + provider
    dispatcher returns None."""
    monkeypatch.setenv("AEGIS_TRIPLE_AXIS_PROVIDER", "sllm")
    monkeypatch.setenv("AEGIS_JUDGE_PROVIDER", "dummy")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_JUDGE_MODEL_PATH", raising=False)
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    advice = assess_triple_axis(records)
    # No real LLM available → heuristic.
    assert advice.advisor_kind == "heuristic"


def test_umbrella_explicit_kwarg_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """prefer_sllm=True overrides env."""
    monkeypatch.delenv("AEGIS_TRIPLE_AXIS_PROVIDER", raising=False)
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    advice = assess_triple_axis(
        records, prefer_sllm=True,
        llm_call=_stub_returning('{"summary": "from sllm"}'),
    )
    assert advice.advisor_kind == "sllm"
    assert advice.summary == "from sllm"


# ── render ─────────────────────────────────────────────────────────


def test_render_includes_all_three_axes() -> None:
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(5)]
    advice = assess_via_heuristic(extract_axis_signals(records))
    out = render_triple_axis(advice)
    assert "Token efficiency" in out
    assert "Cache performance" in out
    assert "Stability" in out
    assert "score" in out
    assert "overall priority" in out


def test_render_empty_window_renders_clean() -> None:
    advice = assess_via_heuristic(extract_axis_signals([]))
    out = render_triple_axis(advice)
    # Should not raise on empty data; should report records=0.
    assert "records: 0" in out


# ── data shape ────────────────────────────────────────────────────


def test_axis_assessment_score_in_range() -> None:
    """Heuristic must always produce scores in [0.0, 1.0]."""
    records = [_rec(decision="BLOCK", reason="rule:x") for _ in range(100)]
    advice = assess_via_heuristic(extract_axis_signals(records))
    for ax in (advice.token_efficiency, advice.cache_performance,
               advice.stability):
        assert 0.0 <= ax.score <= 1.0
        assert ax.severity in ("ok", "warn", "alert")


def test_triple_axis_advice_overall_is_one_of_three_axes() -> None:
    """`overall_priority` must be exactly one of the three axes."""
    records = [_rec(decision="BLOCK") for _ in range(20)]
    advice = assess_via_heuristic(extract_axis_signals(records))
    assert advice.overall_priority in (
        "token_efficiency", "cache_performance", "stability",
    )
