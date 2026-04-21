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
