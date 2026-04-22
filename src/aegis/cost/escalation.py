"""Cost-divergence escalation (patent ¶[0088] + Claim 27).

When any of the three divergence metrics exceeds a role-specific
baseline by a configured factor, the escalation fires INDEPENDENTLY of
the sLLM Judgment Engine verdict. This catches:
  (i)   instrumentation compromise / runtime tampering,
  (ii)  unauthorized model substitution,
  (iii) co-located workload contamination of HW accounting,
  (iv)  calibration staleness in the FLOPs-per-token table.

For T2 MVP the role-specific baseline is a configured default;
M11 will eventually feed live baselines from Burn-in's Shadow phase.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.cost.divergence import DivergenceMetrics

# Default per-metric baseline (T2 conservative). Real values come from
# Burn-in Shadow phase per role.
DEFAULT_BASELINE: dict[str, float] = {
    "token_to_flops": 0.10,    # 10% relative gap is normal noise
    "memory_cost":    0.10,
    "dollar_cost":    0.10,
}

# Multiplicative threshold above baseline — exceedance triggers escalation.
ESCALATION_MULTIPLIER: float = 3.0


@dataclass(frozen=True)
class EscalationDecision:
    triggered: bool
    metric: str | None
    observed: float
    baseline: float
    threshold: float
    reason: str


def evaluate_escalation(
    divergence: DivergenceMetrics,
    *,
    role_baseline: dict[str, float] | None = None,
    multiplier: float = ESCALATION_MULTIPLIER,
) -> EscalationDecision:
    """Inspect each of the three metrics; return the first triggering
    EscalationDecision (or a not-triggered one).
    """
    baselines = {**DEFAULT_BASELINE, **(role_baseline or {})}
    candidates = [
        ("token_to_flops", divergence.token_to_flops),
        ("memory_cost",    divergence.memory_cost),
        ("dollar_cost",    divergence.dollar_cost),
    ]
    for name, observed in candidates:
        baseline = baselines.get(name, 0.10)
        threshold = baseline * multiplier
        if observed > threshold:
            return EscalationDecision(
                triggered=True,
                metric=name,
                observed=observed,
                baseline=baseline,
                threshold=threshold,
                reason=(
                    f"cost-divergence escalation: {name} = {observed:.3f} > "
                    f"threshold {threshold:.3f} ({multiplier:.1f}× baseline {baseline:.3f}). "
                    "Possible runtime tampering, model substitution, or stale calibration."
                ),
            )
    return EscalationDecision(
        triggered=False, metric=None, observed=0.0,
        baseline=0.0, threshold=0.0, reason="no cost divergence above threshold",
    )
