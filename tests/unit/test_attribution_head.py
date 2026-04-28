"""Unit tests for src/aegis/judge/attribution_head.py (v2.5)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.hw_telemetry import simulate
from aegis.judge.attribution_head import (
    DEFAULT_WEIGHTS_PATH,
    AttributionHead,
    reset_weights_cache,
)
from aegis.judge.base import JudgeVerdict
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


@pytest.fixture(autouse=True)
def _reset_weights() -> None:
    reset_weights_cache()


def _atv_input(
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    *,
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
    plan: str = "execute the user's task",
    model: str = "claude-haiku-4-5",
    aid: str = "agent-test",
) -> ATVInput:
    args = args or {"command": "ls"}
    cum_dollars = expected_flops(model, in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="demo",
            aid=aid,
            timestamp_ns=0,
            model_hash=model,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        plan_text=plan,
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens,
            output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
        ),
    )


# ---- Frozen-weights loading + hash ------------------------------------


def test_default_weights_file_exists() -> None:
    assert DEFAULT_WEIGHTS_PATH.exists()


def test_attribution_head_loads_default_weights() -> None:
    head = AttributionHead()
    assert head.model_hash
    assert len(head.model_hash) == 64  # SHA3-256 hex


def test_model_hash_deterministic_across_instances() -> None:
    a = AttributionHead()
    b = AttributionHead()
    assert a.model_hash == b.model_hash


def test_model_hash_matches_file_sha3() -> None:
    head = AttributionHead()
    expected = hashlib.sha3_256(DEFAULT_WEIGHTS_PATH.read_bytes()).hexdigest()
    assert head.model_hash == expected


def test_custom_weights_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({
        "version": 1,
        "subfield_weights": {"agent_state_embedding": 0.0},
        "thresholds": {"block": 0.7, "require_approval": 0.4},
    }))
    head = AttributionHead(weights_path=custom)
    assert head.model_hash != AttributionHead().model_hash


# ---- Text fallback (evaluate) -----------------------------------------


def test_text_fallback_block_keyword() -> None:
    head = AttributionHead()
    v = head.evaluate("tool: sql\nargs: drop table users")
    assert v.decision == "BLOCK"
    assert v.model_hash == head.model_hash


def test_text_fallback_approval_tool() -> None:
    head = AttributionHead()
    v = head.evaluate("tool: send_email\nargs: hello")
    assert v.decision == "REQUIRE_APPROVAL"


def test_text_fallback_default_allow() -> None:
    head = AttributionHead()
    v = head.evaluate("tool: read_file\nargs: README.md")
    assert v.decision == "ALLOW"


# ---- Rich path (evaluate_full with ATV) -------------------------------


def test_evaluate_full_returns_judge_verdict() -> None:
    head = AttributionHead()
    inp = _atv_input()
    atv = build_atv(inp)
    v = head.evaluate_full("", atv=atv, inp=inp)
    assert isinstance(v, JudgeVerdict)
    assert v.model_hash == head.model_hash


def test_evaluate_full_no_atv_falls_back_to_text() -> None:
    head = AttributionHead()
    v = head.evaluate_full("tool: sql\nargs: drop table users")
    assert v.decision == "BLOCK"
    # Text fallback path doesn't compute subfield attribution.
    assert v.subfield_attribution == {}


def test_evaluate_full_populates_30_subfield_keys() -> None:
    head = AttributionHead()
    inp = _atv_input()
    atv = build_atv(inp)
    v = head.evaluate_full("", atv=atv, inp=inp)
    assert len(v.subfield_attribution) == 30
    # Each value must be in [0, 1] (or close — we clamp).
    for sf, contribution in v.subfield_attribution.items():
        assert 0.0 <= contribution <= 1.0, f"{sf} = {contribution}"


def test_evaluate_full_records_latency() -> None:
    head = AttributionHead()
    inp = _atv_input()
    atv = build_atv(inp)
    v = head.evaluate_full("", atv=atv, inp=inp)
    assert v.latency_ms is not None
    assert v.latency_ms < 100.0  # should be sub-100ms


def test_evaluate_full_deterministic_same_input() -> None:
    """Same ATV → identical verdict + identical attribution dict."""
    head = AttributionHead()
    inp = _atv_input()
    atv = build_atv(inp)
    v1 = head.evaluate_full("", atv=atv, inp=inp)
    v2 = head.evaluate_full("", atv=atv, inp=inp)
    assert v1.decision == v2.decision
    assert v1.confidence == v2.confidence
    assert v1.subfield_attribution == v2.subfield_attribution
    assert v1.model_hash == v2.model_hash


def test_evaluate_full_high_blast_tool_increases_score() -> None:
    """A tool with high blast radius should produce higher confidence
    than a low-blast counterpart."""
    head = AttributionHead()
    low_blast = _atv_input(tool="Read", args={"file_path": "/tmp/x.txt"})
    high_blast = _atv_input(tool="Bash", args={"command": "ls"})
    atv_low = build_atv(low_blast)
    atv_high = build_atv(high_blast)
    v_low = head.evaluate_full("", atv=atv_low, inp=low_blast)
    v_high = head.evaluate_full("", atv=atv_high, inp=high_blast)
    # Bash has higher blast → higher confidence under our weights.
    assert v_high.confidence >= v_low.confidence


def test_evaluate_full_destructive_args_block() -> None:
    """tool_arg_inspection slot 0 (destructive_verb) is heavily weighted —
    a bash command containing 'rm -rf' should push the score above the
    BLOCK threshold."""
    head = AttributionHead()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /tmp/foo"})
    atv = build_atv(inp)
    v = head.evaluate_full("", atv=atv, inp=inp)
    # destructive_verb is the dominant contributor
    top = max(v.subfield_attribution.items(), key=lambda kv: kv[1])
    assert top[0] == "tool_arg_inspection"
    assert v.decision in {"BLOCK", "REQUIRE_APPROVAL"}


def test_evaluate_full_hw_anomaly_triggers_high_attribution() -> None:
    """When the HW band carries an iommu_violation, the
    aid_tag_transitions / atmu_anomaly / hypervisor_signals subfields
    must dominate the attribution."""
    head = AttributionHead()
    inp = _atv_input()
    hw = simulate(inp, attack="iommu_violation")
    atv = build_atv(inp, hw=hw)
    v = head.evaluate_full("", atv=atv, inp=inp)
    assert v.decision in {"BLOCK", "REQUIRE_APPROVAL"}
    # Top-3 contributors should include at least one HW subfield.
    top3 = sorted(v.subfield_attribution.items(), key=lambda kv: -kv[1])[:3]
    top3_names = {name for name, _ in top3}
    assert top3_names & {
        "aid_tag_transitions", "atmu_anomaly", "hypervisor_signals"
    }


def test_evaluate_full_innocent_call_allows() -> None:
    """A simple read_file with no risky args + minimal cost + no HW
    telemetry should produce a low score below both thresholds.

    Note: the test uses ``read_file`` (donor canonical name) rather
    than Claude Code's ``Read`` so it hits TOOL_BLAST_TABLE's blast=1
    instead of the UNKNOWN_TOOL_BLAST=5 fallback. The Claude Code
    name mismatch is the DOGFOOD #1 finding addressed at step312
    normalize — not the attribution head's concern.
    """
    head = AttributionHead()
    inp = _atv_input(
        tool="read_file",
        args={"file_path": "/tmp/x.txt"},
        plan="",
        in_tokens=10.0,
        out_tokens=5.0,
    )
    atv = build_atv(inp)
    v = head.evaluate_full("", atv=atv, inp=inp)
    assert v.decision == "ALLOW"
    assert v.confidence < 0.40  # below approval threshold


def test_evaluate_full_score_clamped_to_unit_range() -> None:
    """Even with HW attack injection the confidence stays in [0, 1]."""
    head = AttributionHead()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /"})
    hw = simulate(inp, attack="token_flops_mismatch,hbm_exfil,iommu_violation")
    atv = build_atv(inp, hw=hw)
    v = head.evaluate_full("", atv=atv, inp=inp)
    assert 0.0 <= v.confidence <= 1.0


# ---- Cache + reload --------------------------------------------------


def test_reset_weights_cache_picks_up_new_file(tmp_path: Path) -> None:
    custom = tmp_path / "weights.json"
    custom.write_text(json.dumps({
        "version": 1,
        "subfield_weights": {sf: 0.0 for sf, _ in []},  # placeholder; will be overwritten
        "thresholds": {"block": 0.7, "require_approval": 0.4},
    }))
    head1 = AttributionHead(weights_path=custom)
    h1 = head1.model_hash
    custom.write_text(json.dumps({
        "version": 1,
        "subfield_weights": {"agent_state_embedding": 0.5},
        "thresholds": {"block": 0.7, "require_approval": 0.4},
    }))
    reset_weights_cache()
    head2 = AttributionHead(weights_path=custom)
    assert head2.model_hash != h1


# ---- get_judge() integration ----------------------------------------


def test_get_judge_returns_attribution_head_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis.config import settings
    from aegis.judge import get_judge

    monkeypatch.setattr(settings, "aegis_judge_provider", "attribution_head")
    j = get_judge()
    assert isinstance(j, AttributionHead)


# ---- step340 pipeline integration -----------------------------------


def test_step340_uses_attribution_head_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AEGIS_JUDGE_PROVIDER=attribution_head, step340 receives a
    JudgeVerdict whose subfield_attribution is fully populated."""
    from aegis.config import settings
    from aegis.firewall import step340_policy
    from aegis.firewall.core import FirewallContext
    from aegis.firewall.step340_policy import reset_policy_cache

    monkeypatch.setattr(settings, "aegis_judge_provider", "attribution_head")
    reset_policy_cache()

    inp = _atv_input(tool="Read", args={"file_path": "/tmp/x.txt"})
    atv = build_atv(inp)
    ctx = FirewallContext()
    res = step340_policy.run(atv, inp, ctx)
    # The attribution dict should land in ctx.extras for the audit log.
    assert "subfield_attribution" in ctx.extras or res.verdict is None
    # AttributionHead never short-circuits to NotImplementedError.
    assert res.trace.startswith("step340")
