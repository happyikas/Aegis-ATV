"""Unit tests for src/aegis/judge/hybrid.py (v3.0)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.judge.attribution_head import AttributionHead
from aegis.judge.base import Judge, JudgeVerdict
from aegis.judge.dummy import DummyJudge
from aegis.judge.hybrid import HybridJudge, HybridLayer, _default_layers
from aegis.judge.local_phi import LocalPhiJudge
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


def _atv_input(
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    *,
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
    model: str = "claude-haiku-4-5",
) -> ATVInput:
    args = args or {"command": "ls"}
    cum_dollars = expected_flops(model, in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="demo",
            aid="agent-test",
            timestamp_ns=0,
            model_hash=model,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens,
            output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
        ),
    )


# Mock layer that returns a fixed verdict, useful for routing logic tests
class _FixedJudge(Judge):
    def __init__(self, decision: str, confidence: float, reason: str = "fixed") -> None:
        self.decision = decision
        self.confidence = confidence
        self.reason = reason
        self.calls = 0

    def evaluate(self, summary: str) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(
            decision=self.decision,  # type: ignore[arg-type]
            confidence=self.confidence,
            reason=self.reason,
            model_hash=f"fixed-{self.decision}",
        )


# ---- Default layer construction ---------------------------------------


def test_default_layers_no_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    layers = _default_layers()
    names = [layer.name for layer in layers]
    assert "m13_attribution" in names
    assert "local_phi" in names
    assert "haiku" not in names  # excluded when no API key
    assert names[-1] == "dummy"  # always last


def test_default_layers_with_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    layers = _default_layers()
    names = [layer.name for layer in layers]
    assert names == ["m13_attribution", "local_phi", "haiku", "dummy"]


def test_default_layers_judge_types() -> None:
    """The first layer is M13, the second is Phi, the last is Dummy."""
    layers = _default_layers()
    assert isinstance(layers[0].judge, AttributionHead)
    assert isinstance(layers[1].judge, LocalPhiJudge)
    assert isinstance(layers[-1].judge, DummyJudge)


# ---- Routing logic ----------------------------------------------------


def test_first_tier_block_short_circuits() -> None:
    """A BLOCK from the first tier ends the routing immediately."""
    block = _FixedJudge("BLOCK", 0.9, reason="immediate block")
    next_tier = _FixedJudge("ALLOW", 1.0)
    judge = HybridJudge(layers=[
        HybridLayer("first", block),
        HybridLayer("second", next_tier),
    ])
    v = judge.evaluate_full("", atv=None, inp=None)
    assert v.decision == "BLOCK"
    assert "hybrid[first]" in v.reason
    assert next_tier.calls == 0  # never consulted


def test_first_tier_high_confidence_allow_short_circuits() -> None:
    """An ALLOW with confidence ≥ allow_threshold ends routing."""
    confident_allow = _FixedJudge("ALLOW", 0.85)
    next_tier = _FixedJudge("BLOCK", 1.0)
    judge = HybridJudge(layers=[
        HybridLayer("first", confident_allow, allow_threshold=0.50),
        HybridLayer("second", next_tier),
    ])
    v = judge.evaluate_full("")
    assert v.decision == "ALLOW"
    assert "hybrid[first]" in v.reason
    assert next_tier.calls == 0


def test_first_tier_low_confidence_allow_escalates() -> None:
    """ALLOW with confidence < allow_threshold escalates."""
    weak_allow = _FixedJudge("ALLOW", 0.10)
    next_tier = _FixedJudge("BLOCK", 0.9, reason="escalated catch")
    judge = HybridJudge(layers=[
        HybridLayer("first", weak_allow, allow_threshold=0.50),
        HybridLayer("second", next_tier),
    ])
    v = judge.evaluate_full("")
    assert v.decision == "BLOCK"
    assert "hybrid[second]" in v.reason
    assert next_tier.calls == 1


def test_require_approval_short_circuits_too() -> None:
    """REQUIRE_APPROVAL is also a deciding verdict (never escalated)."""
    approval = _FixedJudge("REQUIRE_APPROVAL", 0.6)
    next_tier = _FixedJudge("ALLOW", 1.0)
    judge = HybridJudge(layers=[
        HybridLayer("first", approval),
        HybridLayer("second", next_tier),
    ])
    v = judge.evaluate_full("")
    assert v.decision == "REQUIRE_APPROVAL"
    assert next_tier.calls == 0


def test_all_tiers_low_confidence_falls_through_to_last() -> None:
    """If every tier returns low-confidence ALLOW, the last tier wins."""
    a = _FixedJudge("ALLOW", 0.05, reason="weak A")
    b = _FixedJudge("ALLOW", 0.05, reason="weak B")
    c = _FixedJudge("ALLOW", 0.05, reason="weak C")
    judge = HybridJudge(layers=[
        HybridLayer("a", a, allow_threshold=0.50),
        HybridLayer("b", b, allow_threshold=0.50),
        HybridLayer("c", c, allow_threshold=0.0),
    ])
    v = judge.evaluate_full("")
    assert v.decision == "ALLOW"
    assert v.reason.startswith("hybrid[c]")
    assert a.calls == 1 and b.calls == 1 and c.calls == 1


def test_layer_traces_record_each_consulted_tier() -> None:
    a = _FixedJudge("ALLOW", 0.10)
    b = _FixedJudge("BLOCK", 0.9)
    c = _FixedJudge("ALLOW", 1.0)  # never consulted
    judge = HybridJudge(layers=[
        HybridLayer("a", a, allow_threshold=0.50),
        HybridLayer("b", b),
        HybridLayer("c", c),
    ])
    v = judge.evaluate_full("")
    assert len(v.layer_traces) == 2
    assert v.layer_traces[0].startswith("a:")
    assert v.layer_traces[1].startswith("b:")
    assert "ALLOW" in v.layer_traces[0]
    assert "BLOCK" in v.layer_traces[1]


def test_model_hash_is_deciding_layer_hash() -> None:
    a = _FixedJudge("ALLOW", 0.10)
    b = _FixedJudge("BLOCK", 0.9)
    judge = HybridJudge(layers=[
        HybridLayer("a", a, allow_threshold=0.50),
        HybridLayer("b", b),
    ])
    v = judge.evaluate_full("")
    assert v.model_hash == "fixed-BLOCK"  # from the deciding tier


def test_cumulative_latency_recorded() -> None:
    a = _FixedJudge("ALLOW", 0.10)
    b = _FixedJudge("ALLOW", 1.0)
    judge = HybridJudge(layers=[
        HybridLayer("a", a, allow_threshold=0.50),
        HybridLayer("b", b),
    ])
    v = judge.evaluate_full("")
    assert v.latency_ms is not None
    assert v.latency_ms >= 0.0  # at least the cumulative tier latency


def test_empty_layers_raises() -> None:
    with pytest.raises(ValueError, match="at least one layer"):
        HybridJudge(layers=[])


# ---- Real-judge integration -------------------------------------------


def test_real_default_stack_blocks_destructive_args() -> None:
    """End-to-end with the real default stack: rm -rf must BLOCK."""
    judge = HybridJudge()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /tmp/foo"})
    atv = build_atv(inp)
    v = judge.evaluate_full("", atv=atv, inp=inp)
    assert v.decision in {"BLOCK", "REQUIRE_APPROVAL"}
    # Check that at least M13 was the first deciding layer
    assert v.layer_traces[0].startswith("m13_attribution:")


def test_real_default_stack_allows_innocent_read_quickly() -> None:
    """End-to-end: an innocent read_file is decided by the first
    high-confidence ALLOW (M13 commits at conf >= 0.30). One tier
    consulted, sub-millisecond latency."""
    judge = HybridJudge()
    inp = _atv_input(
        tool="read_file",
        args={"file_path": "/tmp/x.txt"},
        in_tokens=10.0,
        out_tokens=5.0,
    )
    atv = build_atv(inp)
    v = judge.evaluate_full("", atv=atv, inp=inp)
    assert v.decision == "ALLOW"
    # M13 is decisive (its threshold 0.30 is hit by innocent reads).
    assert v.reason.startswith("hybrid[m13_attribution]")
    assert len(v.layer_traces) == 1


def _strip_latency_from_trace(trace: str) -> str:
    """layer_traces include the per-tier latency as ``(N.Nms)`` —
    that's wall-clock and inherently non-deterministic. Strip it for
    comparison purposes."""
    import re

    return re.sub(r"\s*\(\d+(?:\.\d+)?ms\)\s*", "", trace)


def test_real_default_stack_deterministic() -> None:
    """Same input across two calls must produce identical (decision,
    confidence, model_hash, reason). layer_traces match modulo the
    per-tier wall-clock latency suffix."""
    judge = HybridJudge()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /tmp/foo"})
    atv = build_atv(inp)
    v1 = judge.evaluate_full("", atv=atv, inp=inp)
    v2 = judge.evaluate_full("", atv=atv, inp=inp)
    assert v1.decision == v2.decision
    assert v1.confidence == v2.confidence
    assert v1.model_hash == v2.model_hash
    assert v1.reason == v2.reason
    # layer_traces match after stripping the latency suffix.
    assert [_strip_latency_from_trace(t) for t in v1.layer_traces] == [
        _strip_latency_from_trace(t) for t in v2.layer_traces
    ]


# ---- get_judge integration --------------------------------------------


def test_get_judge_returns_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis.config import settings
    from aegis.judge import get_judge

    monkeypatch.setattr(settings, "aegis_judge_provider", "hybrid")
    j = get_judge()
    assert isinstance(j, HybridJudge)
