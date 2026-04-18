"""Tests for Step 335 — cost / budget gating."""

from __future__ import annotations

from aegis.firewall.core import FirewallContext
from aegis.firewall.step335_cost import TENANT_BUDGETS, run
from aegis.schema import CostEfficiency
from tests.unit._firewall_helpers import ZERO_ATV, make_input


def test_within_budget_passes() -> None:
    ce = CostEfficiency(exp_bytes_write=1024, exp_dollars=0.01, confidence=0.9)
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict is None


def test_byte_budget_overrun_requires_approval() -> None:
    over = TENANT_BUDGETS["demo-tenant"]["bytes"] * 2
    ce = CostEfficiency(exp_bytes_write=over, confidence=0.9)
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict == "REQUIRE_APPROVAL"
    assert "byte" in r.reason.lower() or "bytes" in r.reason.lower()


def test_dollar_budget_overrun_requires_approval() -> None:
    over = TENANT_BUDGETS["demo-tenant"]["dollars"] * 2
    ce = CostEfficiency(exp_dollars=over, confidence=0.9)
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict == "REQUIRE_APPROVAL"
    assert "dollar" in r.reason.lower() or "$" in r.reason


def test_low_confidence_requires_approval() -> None:
    ce = CostEfficiency(confidence=0.1)
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict == "REQUIRE_APPROVAL"
    assert "confidence" in r.reason.lower()


def test_unknown_tenant_uses_default_budget() -> None:
    ce = CostEfficiency(exp_dollars=0.01, confidence=0.9)
    r = run(ZERO_ATV, make_input(tenant_id="brand-new-tenant", cost=ce), FirewallContext())
    assert r.verdict is None
