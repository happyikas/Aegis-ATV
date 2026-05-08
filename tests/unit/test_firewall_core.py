"""Tests for the firewall orchestrator."""

from __future__ import annotations

from aegis.firewall.core import FirewallContext, StepResult, run_firewall
from aegis.schema import CostEfficiencyMetrics
from tests.unit._firewall_helpers import ZERO_ATV, make_input


def _ok_a(_a: object, _i: object, _c: FirewallContext) -> StepResult:
    return StepResult(None, "", "stub-ok-a")


def _ok_b(_a: object, _i: object, _c: FirewallContext) -> StepResult:
    return StepResult(None, "", "stub-ok-b")


def _ok_c(_a: object, _i: object, _c: FirewallContext) -> StepResult:
    return StepResult(None, "", "stub-ok-c")


def _block(_a: object, _i: object, _c: FirewallContext) -> StepResult:
    return StepResult("BLOCK", "boom", "stub-block")


def _approval(_a: object, _i: object, _c: FirewallContext) -> StepResult:
    return StepResult("REQUIRE_APPROVAL", "needs human", "stub-approval")


def test_all_pass_yields_allow() -> None:
    v = run_firewall(ZERO_ATV, make_input(), atv_id="x", steps=[_ok_a, _ok_b, _ok_c])
    assert v.decision == "ALLOW"
    assert v.atv_id == "x"
    assert len(v.step_traces) == 3


def test_block_short_circuits() -> None:
    v = run_firewall(ZERO_ATV, make_input(), atv_id="x", steps=[_ok_a, _block, _ok_c])
    assert v.decision == "BLOCK"
    assert "boom" in v.reason
    assert len(v.step_traces) == 2  # third step never ran


def test_approval_short_circuits() -> None:
    v = run_firewall(ZERO_ATV, make_input(), atv_id="x", steps=[_approval, _block])
    assert v.decision == "REQUIRE_APPROVAL"
    assert "human" in v.reason


def test_default_pipeline_clean_request_allows() -> None:
    inp = make_input(
        tool_name="read_file",
        tool_args_json='{"path":"./data/report.txt"}',
        cost=CostEfficiencyMetrics(cumulative_dollars=0.01, forecasted_cost_to_completion=0.05),
    )
    v = run_firewall(ZERO_ATV, inp, atv_id="abc")
    assert v.decision == "ALLOW"


def test_default_pipeline_blocks_dangerous_args() -> None:
    inp = make_input(tool_args_json='{"cmd":"rm -rf /"}')
    v = run_firewall(ZERO_ATV, inp, atv_id="abc")
    assert v.decision == "BLOCK"


def test_default_pipeline_high_blast_requires_approval() -> None:
    inp = make_input(tool_name="transfer_funds", tool_args_json='{"to":"x","amount":1}')
    v = run_firewall(ZERO_ATV, inp, atv_id="abc")
    assert v.decision == "REQUIRE_APPROVAL"


# ── PR-D: AEGIS_STEP_TIMING_ENABLED — per-step latency profiling ────


def test_step_timings_off_by_default(monkeypatch) -> None:
    """Without AEGIS_STEP_TIMING_ENABLED, Verdict.step_timings_us is None
    so the field is omitted from JSON serialisation. This keeps the
    default audit log lean — no extra bytes per record for users who
    haven't opted in."""
    monkeypatch.delenv("AEGIS_STEP_TIMING_ENABLED", raising=False)
    v = run_firewall(
        ZERO_ATV, make_input(), atv_id="x",
        steps=[_ok_a, _ok_b, _ok_c],
    )
    assert v.step_timings_us is None


def test_step_timings_populated_when_enabled(monkeypatch) -> None:
    """With the flag, every step that ran gets a microsecond timing
    keyed by the same module.fn name as step_traces."""
    monkeypatch.setenv("AEGIS_STEP_TIMING_ENABLED", "1")
    v = run_firewall(
        ZERO_ATV, make_input(), atv_id="x",
        steps=[_ok_a, _ok_b, _ok_c],
    )
    assert v.step_timings_us is not None
    # All three steps ran → three timing entries.
    assert len(v.step_timings_us) == 3
    # Keys mirror step_traces — caller can stitch trace + timing.
    assert set(v.step_timings_us.keys()) == set(v.step_traces.keys())
    # Values are non-negative integers (microseconds rounded).
    for k, us in v.step_timings_us.items():
        assert isinstance(us, int), f"{k} timing must be int, got {type(us)}"
        assert us >= 0, f"{k} timing went negative: {us}"


def test_step_timings_short_circuit_only_records_run_steps(
    monkeypatch,
) -> None:
    """When step 2 BLOCKs, step 3 must NOT appear in step_timings_us —
    the orchestrator returns immediately. Symmetry with step_traces."""
    monkeypatch.setenv("AEGIS_STEP_TIMING_ENABLED", "1")
    v = run_firewall(
        ZERO_ATV, make_input(), atv_id="x",
        steps=[_ok_a, _block, _ok_c],
    )
    assert v.decision == "BLOCK"
    assert v.step_timings_us is not None
    assert len(v.step_timings_us) == 2  # ok_a + _block, not _ok_c
    assert set(v.step_timings_us.keys()) == set(v.step_traces.keys())


def test_step_timings_truthy_values_for_all_modes(monkeypatch) -> None:
    """The env var accepts 1/true/yes/on case-insensitively."""
    for value in ("1", "true", "True", "TRUE", "yes", "on"):
        monkeypatch.setenv("AEGIS_STEP_TIMING_ENABLED", value)
        v = run_firewall(
            ZERO_ATV, make_input(), atv_id="x",
            steps=[_ok_a],
        )
        assert v.step_timings_us is not None, (
            f"step_timings_us should be populated for env value {value!r}"
        )


def test_step_timings_falsy_values_keep_field_off(monkeypatch) -> None:
    for value in ("0", "false", "False", "no", "off", ""):
        monkeypatch.setenv("AEGIS_STEP_TIMING_ENABLED", value)
        v = run_firewall(
            ZERO_ATV, make_input(), atv_id="x",
            steps=[_ok_a],
        )
        assert v.step_timings_us is None, (
            f"step_timings_us should stay None for env value {value!r}"
        )
