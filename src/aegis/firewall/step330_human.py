"""Step 330 — Human Oversight (PLAN 6.4)."""

from __future__ import annotations

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

HIGH_BLAST_THRESHOLD = 7


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    blast = ctx.blast_radius if ctx.blast_radius is not None else 5
    if blast >= HIGH_BLAST_THRESHOLD:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"blast radius {blast} >= {HIGH_BLAST_THRESHOLD}",
            f"step330: human approval required (blast={blast})",
        )
    return StepResult(None, "", f"step330: ok (blast={blast})")
