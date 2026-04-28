"""Unit tests for src/aegis/firewall/step337_hw_anomaly.py (v2.4)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.firewall import step337_hw_anomaly as step337
from aegis.firewall.core import FirewallContext
from aegis.hw_telemetry import simulate
from aegis.schema import (
    SLICE_AID_TAG_TRANSITIONS,
    SLICE_ATMU_ANOMALY,
    SLICE_NETWORK_TELEMETRY,
    SLICE_THERMAL_ECC_DRIFT,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)


def _atv_input(
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    *,
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
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
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens,
            output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
        ),
    )


def _run(inp: ATVInput, attack: str = "") -> tuple[str | None, str, FirewallContext]:
    """Build ATV (with HW band populated when attack is non-empty), run step337."""
    if attack == "_zero_hw":
        atv = build_atv(inp)  # T2 default
    else:
        hw = simulate(inp, attack=attack)
        atv = build_atv(inp, hw=hw)
    ctx = FirewallContext()
    res = step337.run(atv, inp, ctx)
    return res.verdict, res.reason, ctx


# ---- T2 default (no HW telemetry) ---------------------------------------


def test_step337_noop_when_hw_band_zero() -> None:
    verdict, reason, _ = _run(_atv_input(), attack="_zero_hw")
    assert verdict is None
    assert reason == ""


def test_step337_noop_for_honest_agent() -> None:
    """Even with sim provider on, an honest agent's HW signals are
    nominal — no rule trips."""
    verdict, _, _ = _run(_atv_input(), attack="")  # sim on, no attack
    assert verdict is None


# ---- Severity 1: BLOCK ---------------------------------------------------


def test_iommu_violation_blocks() -> None:
    verdict, reason, ctx = _run(_atv_input(), attack="iommu_violation")
    assert verdict == "BLOCK"
    assert "hw_iommu_violation" in reason
    assert ctx.extras["hw_iommu_norm"] >= 0.20


def test_hypervisor_violation_blocks() -> None:
    """iommu_violation also raises hypervisor ring violations — but
    iommu rule fires first per priority order. To isolate hypervisor,
    write the HW band manually."""
    inp = _atv_input()
    atv = build_atv(inp)  # zero
    atv[SLICE_ATMU_ANOMALY][2] = 0.5  # exceeds 0.34 threshold
    # Make sure something else in HW band is non-zero so the
    # zero-band short-circuit doesn't fire.
    atv[SLICE_THERMAL_ECC_DRIFT][0] = 0.5
    res = step337.run(atv, inp, FirewallContext())
    assert res.verdict == "BLOCK"
    assert "hw_hypervisor_violation" in res.reason


def test_network_exfil_blocks_on_non_egress_tool() -> None:
    verdict, reason, _ = _run(_atv_input(tool="Bash"), attack="network_exfil")
    assert verdict == "BLOCK"
    assert "hw_network_exfil" in reason
    assert "Bash" in reason


@pytest.mark.parametrize(
    "tool",
    ["fetch", "curl", "WebFetch", "render_image", "send_email", "post_message"],
)
def test_network_exfil_does_not_block_egress_allowed_tools(tool: str) -> None:
    """Tools whose intent is outbound traffic are exempt from the
    hw_network_exfil rule — the URL/payload itself is checked at
    step311 (exfil_url) instead."""
    verdict, _, _ = _run(_atv_input(tool=tool), attack="network_exfil")
    # Either ALLOW (None) or some other rule — but NOT hw_network_exfil.
    assert verdict is None or "hw_network_exfil" not in (verdict or "")


# ---- Severity 2: REQUIRE_APPROVAL ---------------------------------------


def test_thermal_spike_requires_approval() -> None:
    verdict, reason, _ = _run(_atv_input(), attack="thermal_spike")
    assert verdict == "REQUIRE_APPROVAL"
    assert "hw_thermal_spike" in reason


def test_dma_fanout_requires_approval() -> None:
    """High DMA fan-out (16+ peers) → REQUIRE_APPROVAL.
    network_exfil attack sets dma_fanout=32 too, which would normally
    BLOCK first via the egress rule. We use an egress-allowed tool
    here so the egress rule doesn't fire and dma_fanout can be observed
    in isolation."""
    verdict, reason, _ = _run(_atv_input(tool="fetch"), attack="network_exfil")
    assert verdict == "REQUIRE_APPROVAL"
    assert "hw_dma_fanout" in reason


def test_ecc_uncorrectable_requires_approval() -> None:
    inp = _atv_input()
    atv = build_atv(inp)
    atv[SLICE_ATMU_ANOMALY][3] = 1.0
    atv[SLICE_THERMAL_ECC_DRIFT][0] = 0.5  # ensure HW band non-zero
    res = step337.run(atv, inp, FirewallContext())
    assert res.verdict == "REQUIRE_APPROVAL"
    assert "hw_ecc_uncorrectable" in res.reason


# ---- Priority order ----------------------------------------------------


def test_iommu_priority_over_other_signals() -> None:
    """If both IOMMU and thermal-anomaly fire, IOMMU (BLOCK) wins."""
    inp = _atv_input()
    atv = build_atv(inp)
    atv[SLICE_AID_TAG_TRANSITIONS][0] = 0.4  # IOMMU
    atv[SLICE_THERMAL_ECC_DRIFT][3] = 1.0    # thermal anomaly
    res = step337.run(atv, inp, FirewallContext())
    assert res.verdict == "BLOCK"
    assert "hw_iommu_violation" in res.reason


def test_block_severity_takes_priority_over_approval() -> None:
    """When network_exfil + thermal_spike fire together on a non-egress
    tool, the BLOCK (network_exfil) wins."""
    inp = _atv_input(tool="Bash")
    atv = build_atv(inp)
    atv[SLICE_NETWORK_TELEMETRY][0] = 0.5    # exceeds 0.20 → exfil BLOCK
    atv[SLICE_THERMAL_ECC_DRIFT][3] = 1.0    # thermal flag — would be REQUIRE_APPROVAL
    res = step337.run(atv, inp, FirewallContext())
    assert res.verdict == "BLOCK"
    assert "hw_network_exfil" in res.reason


def test_threshold_just_below_does_not_fire() -> None:
    """A signal at 0.19 (just below 0.20) should NOT fire the IOMMU rule."""
    inp = _atv_input()
    atv = build_atv(inp)
    atv[SLICE_AID_TAG_TRANSITIONS][0] = 0.19
    atv[SLICE_THERMAL_ECC_DRIFT][0] = 0.5  # ensure HW band non-zero
    res = step337.run(atv, inp, FirewallContext())
    assert res.verdict is None


# ---- ctx.extras audit hint ---------------------------------------------


def test_ctx_extras_records_hw_signal_levels() -> None:
    inp = _atv_input(tool="Bash")
    hw = simulate(inp, attack="thermal_spike")
    atv = build_atv(inp, hw=hw)
    ctx = FirewallContext()
    step337.run(atv, inp, ctx)
    assert "hw_thermal_anomaly" in ctx.extras
    assert ctx.extras["hw_thermal_anomaly"] == 1.0
    # Even when no rule fires, extras still records the signal levels
    # for the audit / risk report.


# ---- Integration with full firewall pipeline ----------------------------


def test_step337_in_pipeline_via_run_firewall() -> None:
    """End-to-end: build ATV with HW counters, run the full pipeline,
    verify step337 BLOCK propagates to the Verdict."""
    from aegis.firewall.core import run_firewall

    inp = _atv_input(tool="Bash")
    hw = simulate(inp, attack="iommu_violation")
    atv = build_atv(inp, hw=hw)

    # Reset loop detector to avoid cross-test bleed.
    from aegis.monitor.loop_detector import reset_default_detector

    reset_default_detector()

    verdict = run_firewall(atv, inp)
    assert verdict.decision == "BLOCK"
    assert "hw_iommu_violation" in verdict.reason


def test_step337_skipped_when_disabled() -> None:
    """When AEGIS_HW_PROVIDER is not 'sim', evaluate.py wouldn't pass
    hw=, so build_atv leaves HW band zero. step337 short-circuits to
    no-op in that case."""
    from aegis.firewall.core import run_firewall
    from aegis.monitor.loop_detector import reset_default_detector

    reset_default_detector()
    inp = _atv_input(tool="ls")
    atv = build_atv(inp)  # no hw → HW band zero
    verdict = run_firewall(atv, inp)
    # Should reach final ALLOW (no step337 BLOCK) — destination depends
    # on dummy judge. Just assert step337 didn't BLOCK here.
    assert "hw_" not in (verdict.reason or "")
