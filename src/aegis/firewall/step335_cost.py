"""Step 335 — Forecasted Cost (PLAN 6.4).

Per-tenant budget table (MVP: in-process dict). Real product would query a
DB. Any of bytes/dollars over budget OR confidence below 0.3 escalates to
REQUIRE_APPROVAL.
"""

from __future__ import annotations

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

TENANT_BUDGETS: dict[str, dict[str, float]] = {
    "demo-tenant": {"bytes": 1e9, "dollars": 1.0, "time_ms": 60_000},
}

DEFAULT_BUDGET: dict[str, float] = {"bytes": 1e9, "dollars": 1.0, "time_ms": 60_000}
LOW_CONFIDENCE_THRESHOLD = 0.3


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    budget = TENANT_BUDGETS.get(inp.header.tenant_id, DEFAULT_BUDGET)
    ce = inp.cost_estimate

    if ce.exp_bytes_write > budget["bytes"]:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"exp_bytes_write {ce.exp_bytes_write:.0f} > budget {budget['bytes']:.0f}",
            "step335: byte budget exceeded",
        )
    if ce.exp_dollars > budget["dollars"]:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"exp_dollars {ce.exp_dollars:.4f} > budget {budget['dollars']:.4f}",
            "step335: dollar budget exceeded",
        )
    if ce.confidence < LOW_CONFIDENCE_THRESHOLD:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"cost confidence too low: {ce.confidence:.2f}",
            "step335: low cost confidence",
        )

    return StepResult(
        None,
        "",
        f"step335: ok (bytes={ce.exp_bytes_write:.0f}, $={ce.exp_dollars:.4f}, "
        f"conf={ce.confidence:.2f})",
    )
