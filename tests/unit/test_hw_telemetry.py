"""Unit tests for src/aegis/hw_telemetry/ + src/aegis/atv/hw_encoders.py (v2.3)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from aegis.atv.builder import build_atv
from aegis.atv.hw_encoders import (
    encode_aid_tag_transitions,
    encode_atmu_anomaly,
    encode_dma_fanout,
    encode_gpu_accelerator_state,
    encode_hw_cost_attestation,
    encode_hypervisor_signals,
    encode_linkage_consistency,
    encode_memory_timing_histograms,
    encode_network_telemetry,
    encode_thermal_ecc_drift,
    encode_watchdog_signals,
    fill_hw_band,
)
from aegis.cost.divergence import compute_divergence
from aegis.cost.escalation import evaluate_escalation
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.hw_telemetry import ATTACK_MODES, HWCounters, simulate, simulate_from_env
from aegis.schema import (
    SLICE_HW_BAND,
    SLICE_HW_COST_ATTESTATION,
    SLICE_LINKAGE_CONSISTENCY,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)


def _atv_input(
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    *,
    in_tokens: float = 100.0,
    out_tokens: float = 50.0,
    cum_tokens: float | None = None,
    cum_dollars: float | None = None,
    model: str = "claude-haiku-4-5",
    aid: str = "agent-test",
) -> ATVInput:
    """Test fixture with cum_dollars / cum_tokens auto-calibrated to FLOPS.

    The dollar_cost_divergence metric compares SW-claimed dollars against
    FLOPS × DEFAULT_DOLLAR_PER_FLOP. If the test passes mismatched values
    by accident (e.g. cum_dollars=0.001 with 1500 tokens), the honest-path
    simulator will trigger escalation even on a clean agent. Defaulting
    cum_dollars to ``expected_flops × DEFAULT_DOLLAR_PER_FLOP`` makes the
    fixture honest-by-default; tests that want a divergence pass an
    explicit value.
    """
    args = args or {"command": "ls"}
    if cum_tokens is None:
        cum_tokens = in_tokens + out_tokens
    if cum_dollars is None:
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
            cumulative_tokens=cum_tokens,
            cumulative_dollars=cum_dollars,
        ),
    )


# ---- Simulator ----------------------------------------------------------


def test_simulator_deterministic_same_input() -> None:
    a = simulate(_atv_input())
    b = simulate(_atv_input())
    assert a == b


def test_simulator_different_aid_diverges() -> None:
    a = simulate(_atv_input(aid="agent-A"))
    b = simulate(_atv_input(aid="agent-B"))
    # Same SW-expected baseline, different jitter seed → different counters.
    assert a.flops_observed != b.flops_observed
    assert a.hbm_bytes_observed != b.hbm_bytes_observed


def test_simulator_honest_path_low_divergence() -> None:
    """Without an attack, simulator HW values stay within ±10% of SW
    expectations → all three divergence metrics < 0.30 escalation
    threshold (DEFAULT_BASELINE 0.10 × ESCALATION_MULTIPLIER 3)."""
    inp = _atv_input(tool="Bash", in_tokens=1000, out_tokens=500)
    hw = simulate(inp)
    div = compute_divergence(
        inp.cost_estimate,
        model_name=inp.header.model_hash or "default",
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    decision = evaluate_escalation(div)
    assert decision.triggered is False, (
        f"Honest agent triggered escalation: {div} ({decision.reason})"
    )


def test_simulator_returns_attack_mode_audit_hint() -> None:
    hw = simulate(_atv_input(), attack="token_flops_mismatch,network_exfil")
    assert "token_flops_mismatch" in hw.attack_mode
    assert "network_exfil" in hw.attack_mode


def test_simulator_unknown_attack_silently_ignored() -> None:
    hw = simulate(_atv_input(), attack="not_a_real_attack")
    assert hw.attack_mode == ""


@pytest.mark.parametrize("mode", sorted(ATTACK_MODES))
def test_each_attack_mode_produces_observable_change(mode: str) -> None:
    """Every attack mode must change at least one counter relative to honest."""
    inp = _atv_input(in_tokens=1000, out_tokens=500)
    honest = simulate(inp)
    attacked = simulate(inp, attack=mode)
    assert honest != attacked, (
        f"attack={mode} produced identical counters to the honest baseline"
    )


def test_token_flops_mismatch_triggers_escalation() -> None:
    inp = _atv_input(in_tokens=1000, out_tokens=500)
    hw = simulate(inp, attack="token_flops_mismatch")
    div = compute_divergence(
        inp.cost_estimate,
        model_name=inp.header.model_hash or "default",
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    decision = evaluate_escalation(div)
    assert decision.triggered is True
    assert decision.metric == "token_to_flops"
    assert "token_to_flops" in decision.reason


def test_hbm_exfil_triggers_memory_cost_escalation() -> None:
    inp = _atv_input(in_tokens=1000, out_tokens=500, cum_tokens=1500)
    hw = simulate(inp, attack="hbm_exfil")
    div = compute_divergence(
        inp.cost_estimate,
        model_name=inp.header.model_hash or "default",
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    decision = evaluate_escalation(div)
    assert decision.triggered is True
    assert decision.metric in {"memory_cost", "token_to_flops"}


def test_cost_underreport_triggers_dollar_or_token_escalation() -> None:
    inp = _atv_input(in_tokens=1000, out_tokens=500, cum_dollars=0.0001)
    hw = simulate(inp, attack="cost_underreport")
    div = compute_divergence(
        inp.cost_estimate,
        model_name=inp.header.model_hash or "default",
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    decision = evaluate_escalation(div)
    assert decision.triggered is True


def test_simulate_from_env_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEGIS_HW_PROVIDER", raising=False)
    assert simulate_from_env(_atv_input()) is None


def test_simulate_from_env_on_when_sim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_HW_PROVIDER", "sim")
    monkeypatch.delenv("AEGIS_HW_INJECT_ATTACK", raising=False)
    hw = simulate_from_env(_atv_input())
    assert hw is not None
    assert isinstance(hw, HWCounters)


def test_simulate_from_env_picks_up_attack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_HW_PROVIDER", "sim")
    monkeypatch.setenv("AEGIS_HW_INJECT_ATTACK", "thermal_spike")
    hw = simulate_from_env(_atv_input())
    assert hw is not None
    assert "thermal_spike" in hw.attack_mode
    assert hw.thermal_celsius_p95 >= 90.0


# ---- Encoders -----------------------------------------------------------


def _honest_hw() -> HWCounters:
    return simulate(_atv_input())


def test_each_encoder_produces_correct_shape() -> None:
    hw = _honest_hw()
    inp = _atv_input()
    metrics = inp.cost_estimate
    model = inp.header.model_hash or "default"
    assert encode_memory_timing_histograms(hw).shape == (32,)
    assert encode_aid_tag_transitions(hw).shape == (24,)
    assert encode_atmu_anomaly(hw).shape == (16,)
    assert encode_dma_fanout(hw).shape == (16,)
    assert encode_thermal_ecc_drift(hw).shape == (16,)
    assert encode_watchdog_signals(hw).shape == (12,)
    assert encode_network_telemetry(hw).shape == (24,)
    assert encode_gpu_accelerator_state(hw).shape == (16,)
    assert encode_hypervisor_signals(hw).shape == (8,)
    assert encode_hw_cost_attestation(hw, metrics, model).shape == (16,)
    assert encode_linkage_consistency(hw, metrics, model).shape == (20,)


def test_encoder_values_clamped_to_unit_range() -> None:
    """Even with extreme attack values every slot stays in [0, 1]."""
    inp = _atv_input(in_tokens=1, out_tokens=1)
    hw = simulate(inp, attack="token_flops_mismatch,hbm_exfil,network_exfil")
    metrics = inp.cost_estimate
    model = inp.header.model_hash or "default"
    for arr in (
        encode_memory_timing_histograms(hw),
        encode_aid_tag_transitions(hw),
        encode_atmu_anomaly(hw),
        encode_dma_fanout(hw),
        encode_thermal_ecc_drift(hw),
        encode_network_telemetry(hw),
        encode_gpu_accelerator_state(hw),
        encode_hw_cost_attestation(hw, metrics, model),
    ):
        assert (arr >= 0.0).all() and (arr <= 1.0).all()


def test_hw_cost_attestation_carries_divergence_in_last_three_slots() -> None:
    """Per Claim 26, slots 13/14/15 are j-14/j-15/j-16 divergence values."""
    inp = _atv_input(in_tokens=1000, out_tokens=500, cum_tokens=1500)
    hw = simulate(inp, attack="token_flops_mismatch")
    arr = encode_hw_cost_attestation(hw, inp.cost_estimate, inp.header.model_hash or "default")
    assert arr[13] > 0.30, "j-14 (token_to_flops divergence) should be elevated"


def test_aid_tag_transitions_zero_when_no_violation() -> None:
    hw = _honest_hw()
    arr = encode_aid_tag_transitions(hw)
    assert arr[0] == 0.0
    assert arr[1] == 0.0


def test_aid_tag_transitions_lights_up_under_iommu_violation() -> None:
    inp = _atv_input()
    hw = simulate(inp, attack="iommu_violation")
    arr = encode_aid_tag_transitions(hw)
    assert arr[0] > 0.0
    assert arr[2:].sum() > 0.0  # spread across heatmap


def test_thermal_anomaly_flag_under_spike() -> None:
    inp = _atv_input()
    hw = simulate(inp, attack="thermal_spike")
    arr = encode_thermal_ecc_drift(hw)
    assert arr[3] == 1.0  # thermal_anomaly flag


def test_network_telemetry_lights_up_under_exfil() -> None:
    inp = _atv_input()
    hw_honest = simulate(inp)
    hw_attack = simulate(inp, attack="network_exfil")
    arr_h = encode_network_telemetry(hw_honest)
    arr_a = encode_network_telemetry(hw_attack)
    assert arr_a[0] > arr_h[0]  # bytes_out
    assert arr_a[3] > arr_h[3]  # log-scale total
    assert arr_a[4] > arr_h[4]  # egress/ingress ratio


def test_linkage_consistency_inverse_of_divergence() -> None:
    """Honest: linkage ≈ 1.0. Attack: linkage drops."""
    inp = _atv_input(in_tokens=1000, out_tokens=500, cum_tokens=1500)
    hw_honest = simulate(inp)
    hw_attack = simulate(inp, attack="token_flops_mismatch")
    metrics = inp.cost_estimate
    model = inp.header.model_hash or "default"
    h = encode_linkage_consistency(hw_honest, metrics, model)
    a = encode_linkage_consistency(hw_attack, metrics, model)
    assert h[0] >= 0.85
    assert a[0] < 0.5
    # Composite slot 3 must reflect the worst gap.
    assert a[3] < h[3]


# ---- builder integration ------------------------------------------------


def test_build_atv_default_zero_hw_band() -> None:
    inp = _atv_input()
    atv = build_atv(inp)
    assert (atv[SLICE_HW_BAND] == 0.0).all()


def test_build_atv_with_hw_populates_band() -> None:
    inp = _atv_input()
    hw = simulate(inp)
    atv = build_atv(inp, hw=hw)
    # At least one slot in the HW band should be non-zero.
    assert (atv[SLICE_HW_BAND] != 0.0).any()


def test_build_atv_hw_cost_attestation_slot_13_matches_divergence() -> None:
    """The encoded slot 13 must equal the standalone compute_divergence()
    result — proves the audit-record self-attestation contract holds."""
    inp = _atv_input(in_tokens=1000, out_tokens=500, cum_tokens=1500)
    hw = simulate(inp, attack="token_flops_mismatch")
    atv = build_atv(inp, hw=hw)
    div = compute_divergence(
        inp.cost_estimate,
        model_name=inp.header.model_hash or "default",
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    cost_slice = atv[SLICE_HW_COST_ATTESTATION]
    assert abs(float(cost_slice[13]) - float(div.token_to_flops)) < 1e-5


def test_build_atv_rejects_non_hwcounters() -> None:
    inp = _atv_input()
    with pytest.raises(TypeError, match="HWCounters"):
        build_atv(inp, hw="not a HWCounters")  # type: ignore[arg-type]


def test_fill_hw_band_explicit_helper() -> None:
    """fill_hw_band should be callable directly with a pre-zeroed array."""
    inp = _atv_input()
    hw = simulate(inp)
    atv = np.zeros(2080, dtype=np.float32)
    fill_hw_band(atv, inp, hw)
    assert (atv[SLICE_LINKAGE_CONSISTENCY][:1] > 0.0).any()
