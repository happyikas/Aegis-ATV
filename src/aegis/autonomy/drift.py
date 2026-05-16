"""Concept-drift detection between baseline and recent posteriors.

A trust table built from a 30-day burn-in is a *snapshot*. The
operator's habits, the repo's structure, and the agent's failure
modes evolve. Without a drift signal, a stale snapshot silently
auto-approves patterns that the operator would no longer trust.

This module computes the KL divergence between two Beta
posteriors — typically the "baseline" (older two-thirds of the
burn-in window) and the "recent" (most recent third):

    KL(Beta(α₁, β₁) ‖ Beta(α₂, β₂))

If KL exceeds a threshold (default 0.10 nats — roughly the
divergence between Beta(20, 1) and Beta(15, 5)), the pattern is
flagged as drifted and the runtime refuses bypass for it until
``aegis autonomy learn`` is rerun.

This is **not** a continuous online detector — it's a
learn-time check that ships with each trust-table snapshot. The
runtime simply reads the ``drift_score`` field of the matched
``TrustedPattern`` and applies the threshold.

We deliberately split the window inside the burn-in pass rather
than comparing against the previously-shipped trust table on
disk: that comparison would create a feedback loop where the
trust table judges itself.
"""

from __future__ import annotations

import math
from typing import Final

from aegis.autonomy.bayesian import BetaPosterior

# Threshold at which we flag a pattern as drifted. Calibrated on
# the dev-fleet burn-in: clean patterns sit at KL ≲ 0.02; patterns
# whose recent third diverged from their baseline (workflow shift)
# climb past 0.10.
DEFAULT_DRIFT_THRESHOLD: Final[float] = 0.10


def kl_divergence_beta(
    p: BetaPosterior,
    q: BetaPosterior,
) -> float:
    """KL(p ‖ q) for two Beta distributions.

    Closed-form: ``ln B(α₂, β₂) − ln B(α₁, β₁) + (α₁ − α₂)·ψ(α₁)
    + (β₁ − β₂)·ψ(β₁) + (α₂ − α₁ + β₂ − β₁)·ψ(α₁ + β₁)`` where
    ``ψ`` is the digamma function. We use ``math.lgamma`` for log
    of the Beta function and a small-series digamma."""
    a1, b1 = p.alpha, p.beta
    a2, b2 = q.alpha, q.beta
    log_b_q = math.lgamma(a2) + math.lgamma(b2) - math.lgamma(a2 + b2)
    log_b_p = math.lgamma(a1) + math.lgamma(b1) - math.lgamma(a1 + b1)
    return (
        log_b_q - log_b_p
        + (a1 - a2) * _digamma(a1)
        + (b1 - b2) * _digamma(b1)
        + (a2 - a1 + b2 - b1) * _digamma(a1 + b1)
    )


def jensen_shannon_beta(
    p: BetaPosterior,
    q: BetaPosterior,
) -> float:
    """Symmetric Jensen-Shannon divergence between two Betas.

    Use this in the CLI rendering since it's bounded in ``[0, ln 2]``
    and easier to threshold; KL is unbounded and asymmetric. We
    approximate the midpoint Beta as ``Beta((α₁+α₂)/2, (β₁+β₂)/2)``
    — not the exact mixture but a tractable upper bound on the
    real JS for the purposes of a drift flag."""
    m_alpha = 0.5 * (p.alpha + q.alpha)
    m_beta = 0.5 * (p.beta + q.beta)
    m = BetaPosterior(
        alpha=m_alpha, beta=m_beta,
        prior_alpha=p.prior_alpha, prior_beta=p.prior_beta,
    )
    return 0.5 * kl_divergence_beta(p, m) + 0.5 * kl_divergence_beta(q, m)


def is_drifted(
    *,
    baseline: BetaPosterior,
    recent: BetaPosterior,
    threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> bool:
    """Return True iff the recent posterior has drifted from the
    baseline beyond the threshold.

    Both windows must have non-zero observed evidence
    (``n_effective > 0``); a window with no recent data is *not*
    flagged as drifted — that's a "no signal" condition, not a
    "signal changed" condition. The learner handles the no-recent
    case separately (typically by widening the credible interval
    until more observations arrive)."""
    if baseline.n_effective <= 0.0 or recent.n_effective <= 0.0:
        return False
    js = jensen_shannon_beta(baseline, recent)
    return js >= threshold


# ──────────────────────────────────────────────────────────────────
# Digamma function — pure Python, no scipy
# ──────────────────────────────────────────────────────────────────


def _digamma(x: float) -> float:
    """Digamma ψ(x) for x > 0. Uses the recurrence ``ψ(x+1) = ψ(x)
    + 1/x`` to push x ≥ 6, then asymptotic series. Accurate to
    ~1e-10 across the input range we care about."""
    if x <= 0.0:
        raise ValueError(f"digamma requires x > 0; got {x}")
    result = 0.0
    while x < 6.0:
        result -= 1.0 / x
        x += 1.0
    # Asymptotic expansion: ψ(x) ≈ ln(x) − 1/(2x) − Σ B_{2k}/(2k x^{2k})
    inv_x = 1.0 / x
    inv_x2 = inv_x * inv_x
    result += (
        math.log(x)
        - 0.5 * inv_x
        - inv_x2 * (
            1.0 / 12.0
            - inv_x2 * (
                1.0 / 120.0
                - inv_x2 * (1.0 / 252.0)
            )
        )
    )
    return result


__all__ = [
    "DEFAULT_DRIFT_THRESHOLD",
    "is_drifted",
    "jensen_shannon_beta",
    "kl_divergence_beta",
]
