"""Action Firewall orchestrator (PLAN 6.4).

Each step is a callable ``(atv, inp, ctx) -> StepResult``. The orchestrator
walks them in order; the first BLOCK or REQUIRE_APPROVAL short-circuits.
``FirewallContext`` is a per-request scratch space so a step (e.g. step320)
can publish a value (e.g. blast radius) for later steps to consume.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from aegis.schema import ATVInput, Verdict


@dataclass
class FirewallContext:
    blast_radius: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """A single step's outcome.

    ``verdict`` semantics:
        * ``None`` — no decision; advance to the next step.
        * ``"BLOCK"`` / ``"REQUIRE_APPROVAL"`` — short-circuit the pipeline.
        * ``"ALLOW"`` — explicit allow; we still continue to the next step
          unless the orchestrator is told otherwise. (Reserved for future use.)
    """

    verdict: str | None
    reason: str
    trace: str


StepFn = Callable[[np.ndarray, ATVInput, FirewallContext], StepResult]


def default_steps() -> list[StepFn]:
    """Return the canonical ordered step list. Lazy-imported to avoid cycles."""
    from aegis.firewall import (
        step305_safe_allowlist,
        step308_identity,
        step309_instruction_drift,
        step310_args,
        step311_donor_rules,
        step312_normalize,
        step315_aid_auth,
        step320_blast,
        step330_human,
        step335_cost,
        step336_loop,
        step337_hw_anomaly,
        step340_policy,
    )

    return [
        step305_safe_allowlist.run,  # v2.1 Day-1 #1 — flag known-safe calls so step340 skips sLLM round-trip
        step308_identity.run,        # v4.2 — agent identity verification (Claim 56)
        step309_instruction_drift.run,  # v2.2 Day-1 #3 — block when CLAUDE.md/AGENTS.md/.mcp.json drifted from baseline
        step310_args.run,
        step311_donor_rules.run,  # D11 + v2.1.2 — donor pattern rule pack + cloud/sql_unbounded
        step312_normalize.run,   # DOGFOOD Rec #3 — canonicalize tool args before downstream steps
        step315_aid_auth.run,    # M14 — AID-region authorization + circuit breaker
        step320_blast.run,
        step330_human.run,
        step335_cost.run,
        step336_loop.run,        # v2.1.3 Day-1 #6 — loop + redundant call saver
        step337_hw_anomaly.run,  # v2.4 — HW band anomaly gate (no-op when AEGIS_HW_PROVIDER!=sim)
        step340_policy.run,
    ]


def run_firewall(
    atv: np.ndarray,
    inp: ATVInput,
    atv_id: str = "",
    steps: Sequence[StepFn] | None = None,
) -> Verdict:
    chosen = list(steps) if steps is not None else default_steps()
    ctx = FirewallContext()
    traces: dict[str, str] = {}
    for fn in chosen:
        result = fn(atv, inp, ctx)
        traces[f"{fn.__module__}.{fn.__name__}"] = result.trace
        if result.verdict in ("BLOCK", "REQUIRE_APPROVAL"):
            return Verdict(
                decision=result.verdict,  # type: ignore[arg-type]
                reason=result.reason,
                atv_id=atv_id,
                step_traces=traces,
            )
    return Verdict(
        decision="ALLOW",
        reason="all firewall steps passed",
        atv_id=atv_id,
        step_traces=traces,
    )
