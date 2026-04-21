"""Tests for Step 335 — forecasted-cost gating (patent ¶[0059] + Claim 33)."""

from __future__ import annotations

from aegis.firewall.core import FirewallContext
from aegis.firewall.step335_cost import TENANT_BUDGETS, run
from aegis.schema import CostEfficiencyMetrics
from tests.unit._firewall_helpers import ZERO_ATV, make_input

CEILING = TENANT_BUDGETS["demo-tenant"]["dollars"]


def test_within_budget_passes() -> None:
    ce = CostEfficiencyMetrics(
        cumulative_dollars=0.10,
        forecasted_cost_to_completion=0.20,
        budget_burn_rate=0.05,
    )
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict is None


def test_cumulative_over_ceiling_requires_approval() -> None:
    ce = CostEfficiencyMetrics(cumulative_dollars=CEILING * 1.5)
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict == "REQUIRE_APPROVAL"
    assert "cumulative" in r.reason.lower() or "exceed" in r.reason.lower()


def test_forecast_over_ceiling_requires_approval() -> None:
    """Patent: forecast over ceiling escalates BEFORE cumulative actually exceeds."""
    ce = CostEfficiencyMetrics(
        cumulative_dollars=0.20,
        forecasted_cost_to_completion=CEILING * 2,
    )
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict == "REQUIRE_APPROVAL"
    assert "forecast" in r.reason.lower()


def test_approaching_ceiling_warns_but_passes() -> None:
    # Forecast = 0.85 of ceiling → above APPROACHING_FRACTION (0.8) but below 1.0
    ce = CostEfficiencyMetrics(
        cumulative_dollars=0.20,
        forecasted_cost_to_completion=CEILING * 0.85,
    )
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict is None
    assert "approaching" in r.trace.lower()


def test_zero_forecast_does_not_alert() -> None:
    ce = CostEfficiencyMetrics(cumulative_dollars=0.10)
    r = run(ZERO_ATV, make_input(cost=ce), FirewallContext())
    assert r.verdict is None
    assert "approaching" not in r.trace.lower()


def test_unknown_tenant_uses_default_budget() -> None:
    ce = CostEfficiencyMetrics(cumulative_dollars=0.05)
    r = run(ZERO_ATV, make_input(tenant_id="brand-new-tenant", cost=ce), FirewallContext())
    assert r.verdict is None
