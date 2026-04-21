"""Step 335 — Forecasted-Cost Gating (patent ¶[0059] + Claim 33).

Reads the ``budget_burn_rate``, ``forecasted_cost_to_completion``, and
``cumulative_dollars`` slots of the cost_efficiency_metrics subfield
and compares against per-tenant budget ceilings:

    forecast > ceiling          → REQUIRE_APPROVAL  (over-ceiling)
    cumulative > ceiling        → REQUIRE_APPROVAL  (already exceeded)
    forecast > ceiling × ALERT  → pass-with-warning trace
    otherwise                   → pass

In hardware-codesigned (T3) embodiments, the cost-gate verdict
additionally considers the cost-linkage divergence metrics within the
hw_cost_attestation subfield. T2 sees only the SW side.
"""

from __future__ import annotations

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

# Per-tenant budget ceilings, in dollars per task. Real product reads from a
# config service or database.
TENANT_BUDGETS: dict[str, dict[str, float]] = {
    "demo-tenant": {"dollars": 1.0},
}
DEFAULT_BUDGET: dict[str, float] = {"dollars": 1.0}

# Ratio of ceiling at which step 335 emits a 'approaching' warning.
APPROACHING_FRACTION = 0.8


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    budget = TENANT_BUDGETS.get(inp.header.tenant_id, DEFAULT_BUDGET)
    ceiling = float(budget.get("dollars", DEFAULT_BUDGET["dollars"]))

    ce = inp.cost_estimate
    forecast = float(ce.forecasted_cost_to_completion)
    cumulative = float(ce.cumulative_dollars)
    burn = float(ce.budget_burn_rate)

    # Already over the ceiling — escalate.
    if cumulative > ceiling:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"cumulative_dollars {cumulative:.4f} > budget {ceiling:.4f}",
            "step335: cumulative cost exceeded ceiling",
        )

    # Forecast predicts overrun — patent: gate on forecast, not just cumulative.
    if forecast > 0 and forecast > ceiling:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"forecasted_cost_to_completion {forecast:.4f} > budget {ceiling:.4f}",
            "step335: forecast over ceiling",
        )

    # Approaching — pass but record the alert in the trace.
    if forecast > 0 and forecast > APPROACHING_FRACTION * ceiling:
        return StepResult(
            None,
            "",
            f"step335: approaching ceiling (forecast {forecast:.4f} / "
            f"ceiling {ceiling:.4f}, burn {burn:.2f})",
        )

    return StepResult(
        None,
        "",
        f"step335: ok (cum={cumulative:.4f}, forecast={forecast:.4f}, "
        f"ceiling={ceiling:.4f}, burn={burn:.2f})",
    )
