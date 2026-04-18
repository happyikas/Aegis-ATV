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
        step310_args,
        step320_blast,
        step330_human,
        step335_cost,
        step340_policy,
    )

    return [
        step310_args.run,
        step320_blast.run,
        step330_human.run,
        step335_cost.run,
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
