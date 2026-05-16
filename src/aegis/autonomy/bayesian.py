"""Bayesian backbone for the autonomy trust learner (v0.5.12).

v0.5.11 represented a pattern's trust as a *point estimate*
``clean_rate`` plus a soft sample-size weight. That formulation has
three well-known training pathologies:

1. **Overfitting at low n.** A pattern observed 5× with 0 BLOCK
   follow-ups gets ``clean_rate = 1.00`` and ``trust_score ≈ 0.70``
   — but the 95% confidence interval on a 5-of-5 success rate is
   roughly ``[0.48, 1.00]``. Treating that as "high trust" is the
   classic small-sample overfit.

2. **Self-confirming bias.** Once a pattern is auto-approved, we
   stop sampling under the original distribution, so the
   ``clean_rate`` we observe afterwards is no longer i.i.d. with
   the burn-in data.

3. **Brittle threshold.** ``trust_score >= 0.85`` was a hard cutoff
   on a noisy estimator. Tiny n-changes flip the decision.

This module replaces the point estimate with a **Beta posterior**
``Beta(α, β)`` over the success rate, and uses the **lower
credible bound (LCB)** at credibility c (default 95%) as the
decision metric. The contract:

* LCB ≥ ``min_trust`` ⇒ pattern is trustworthy.
* Small n ⇒ posterior is wide ⇒ LCB is low ⇒ no bypass (automatic
  Occam regularisation).
* Large n with consistent cleans ⇒ posterior narrows around the
  observed rate ⇒ LCB approaches the rate.

The posterior is updated by **reward-shaped evidence** rather than
binary counts (see :mod:`aegis.autonomy.reward`):

* ``CLEAN`` event ⇒ ``α += 1``
* ``BLOCK_FOLLOWUP`` event ⇒ ``β += 3``
* ``EXPLICIT_DENY`` event ⇒ ``β += 10``

The weights are calibrated so that one explicit user deny costs
ten "clean" wins to recover — high-information events dominate
high-volume routine ones, fixing reward sparsity.

The prior ``Beta(α₀, β₀)`` defaults to a **mildly pessimistic
prior** ``(1, 5)`` — equivalent to "I have already seen 5 bad
samples and 0 good samples" — which is the regularisation knob
that prevents zero-evidence patterns from being trusted.

For hierarchical / empirical-Bayes priors (per-tool baseline
shrinkage) see :func:`empirical_bayes_prior`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

# ──────────────────────────────────────────────────────────────────
# Prior + credibility defaults
# ──────────────────────────────────────────────────────────────────

# Pessimistic default prior: 0 successes + 5 failures of equivalent
# "pseudo-evidence". This shifts trust toward zero when n is small
# and is the primary regulariser against overfitting.
DEFAULT_PRIOR_ALPHA: Final[float] = 1.0
DEFAULT_PRIOR_BETA: Final[float] = 5.0

# We use the lower 5% quantile of the posterior as the decision
# metric ⇒ 95% one-sided credibility that the true success rate is
# at least this high. Two-sided 90% credible interval lower bound.
DEFAULT_CREDIBILITY: Final[float] = 0.95


# ──────────────────────────────────────────────────────────────────
# Posterior dataclass
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BetaPosterior:
    """A Beta(α, β) posterior on a pattern's true success rate.

    ``alpha`` and ``beta`` are *total* shape parameters including
    the prior — i.e. ``alpha = α₀ + Σ α-increments``. The raw
    observation counts (``n_clean``, ``n_block``, ``n_deny``)
    are retained alongside so the audit chain can reconstruct
    *what* evidence built the posterior, not just the resulting
    shape.

    All fields are floats so that decayed evidence (see
    :mod:`aegis.autonomy.decay`) can carry fractional weight
    without integer truncation."""

    alpha: float
    beta: float

    n_clean: float = 0.0
    n_block: float = 0.0
    n_deny: float = 0.0

    # Provenance of the prior used. Stored so an audit reader can
    # tell whether a posterior used the default prior or an
    # empirical-Bayes hierarchical one.
    prior_alpha: float = DEFAULT_PRIOR_ALPHA
    prior_beta: float = DEFAULT_PRIOR_BETA

    @property
    def n_effective(self) -> float:
        """Sum of observation weights, excluding the prior. This is
        the metric the learner uses for the ``min_samples`` gate."""
        return self.n_clean + self.n_block + self.n_deny

    @property
    def mean(self) -> float:
        """Posterior mean ``α / (α + β)``."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        """Posterior variance ``αβ / ((α+β)²(α+β+1))``."""
        s = self.alpha + self.beta
        return (self.alpha * self.beta) / (s * s * (s + 1.0))

    @property
    def std(self) -> float:
        """Posterior standard deviation."""
        return math.sqrt(self.variance)

    def lower_credible_bound(
        self,
        credibility: float = DEFAULT_CREDIBILITY,
    ) -> float:
        """Lower ``(1 - credibility)`` quantile of the posterior.

        For ``credibility = 0.95``, returns the 5th-percentile of
        ``Beta(α, β)``. This is the *decision metric*: if this
        value is ≥ the operator's ``min_trust`` then we are 95%
        confident the true success rate is at least ``min_trust``.

        Uses an inverse-CDF approximation (Cornish-Fisher on the
        normal approximation, refined by Newton's method) to avoid
        a scipy dependency. Accurate to within ~5e-3 across all
        (α, β) > 0 — sufficient for a yes/no decision threshold.
        """
        if not (0.0 < credibility < 1.0):
            raise ValueError(
                f"credibility must be in (0, 1), got {credibility}"
            )
        return _beta_inv_cdf(1.0 - credibility, self.alpha, self.beta)


# ──────────────────────────────────────────────────────────────────
# Posterior construction + update
# ──────────────────────────────────────────────────────────────────


def make_posterior(
    *,
    n_clean: float = 0.0,
    n_block: float = 0.0,
    n_deny: float = 0.0,
    prior_alpha: float = DEFAULT_PRIOR_ALPHA,
    prior_beta: float = DEFAULT_PRIOR_BETA,
    # Reward shaping weights — see aegis.autonomy.reward for
    # the contract. Exposed here so callers can use a custom
    # weighting (e.g. tests) without importing the reward module.
    weight_clean: float = 1.0,
    weight_block: float = 3.0,
    weight_deny: float = 10.0,
) -> BetaPosterior:
    """Build a posterior from reward-shaped event counts."""
    if prior_alpha <= 0 or prior_beta <= 0:
        raise ValueError(
            f"prior must be positive, got α={prior_alpha} β={prior_beta}"
        )
    alpha = prior_alpha + weight_clean * n_clean
    beta = prior_beta + weight_block * n_block + weight_deny * n_deny
    return BetaPosterior(
        alpha=alpha,
        beta=beta,
        n_clean=n_clean,
        n_block=n_block,
        n_deny=n_deny,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
    )


# ──────────────────────────────────────────────────────────────────
# Empirical-Bayes hierarchical prior
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolBaseline:
    """Aggregate statistics across all patterns belonging to one
    tool. Drives empirical-Bayes shrinkage so that a 5-sample
    pattern inherits regularisation from the tool's broader history,
    not just the global prior."""

    tool_name: str
    n_patterns: int
    total_clean: float
    total_block: float
    total_deny: float

    @property
    def total_observed(self) -> float:
        return self.total_clean + self.total_block + self.total_deny

    @property
    def empirical_rate(self) -> float:
        """The pooled success rate across all patterns of this tool.
        Used as the *mean* of the hierarchical prior."""
        if self.total_observed <= 0.0:
            return 0.5
        return self.total_clean / self.total_observed


def empirical_bayes_prior(
    baseline: ToolBaseline,
    *,
    pseudo_count: float = 4.0,
) -> tuple[float, float]:
    """Convert a tool baseline into a Beta prior ``(α₀, β₀)``.

    The pseudo-count controls *how strongly* a new pattern is
    shrunk toward the tool's mean rate:

    * ``pseudo_count=4`` ⇒ "before seeing pattern-specific data,
      I have 4 samples worth of evidence at the tool's empirical
      rate."
    * Higher pseudo-count ⇒ stronger shrinkage, more
      regularisation, slower learning per pattern.

    For tools with very small total history (``total_observed``
    below the pseudo-count) we fall back to the global default
    prior — empirical Bayes on near-empty data is worse than the
    sane default.
    """
    if baseline.total_observed < pseudo_count:
        return DEFAULT_PRIOR_ALPHA, DEFAULT_PRIOR_BETA
    rate = baseline.empirical_rate
    rate = min(max(rate, 0.05), 0.95)  # clamp away from {0, 1}
    return rate * pseudo_count, (1.0 - rate) * pseudo_count


# ──────────────────────────────────────────────────────────────────
# Bonferroni-style multiple-comparisons adjustment
# ──────────────────────────────────────────────────────────────────


def adjusted_min_samples(
    base_min_samples: int,
    n_candidate_patterns: int,
) -> int:
    """When the learner evaluates many patterns simultaneously,
    naive per-pattern thresholds inflate the family-wise false
    positive rate.

    A simple Bonferroni-style adjustment: scale ``min_samples`` by
    ``ln(k)`` where ``k`` is the number of candidate patterns under
    consideration. This is conservative but cheap and matches the
    intuition that with more candidates we need more evidence per
    candidate to maintain the same confidence.

    Returns the adjusted threshold; never less than the base value.
    """
    if n_candidate_patterns <= 1:
        return base_min_samples
    factor = max(1.0, math.log(float(n_candidate_patterns)))
    return max(base_min_samples, int(math.ceil(base_min_samples * factor)))


# ──────────────────────────────────────────────────────────────────
# Beta inverse-CDF — pure-Python, no scipy
# ──────────────────────────────────────────────────────────────────

# We need the inverse CDF of Beta(α, β) for the lower credible
# bound. Pulling scipy in for this would balloon the install
# footprint, so we implement it directly. Two phases:
#
# 1. Cornish-Fisher seed via the normal approximation on a
#    transformed scale — good to ~1e-2.
# 2. Newton's iteration on the regularised incomplete beta to
#    refine to floating-point convergence.


def _log_beta_func(a: float, b: float) -> float:
    """ln B(a, b) = ln Γ(a) + ln Γ(b) − ln Γ(a+b)."""
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _beta_pdf(x: float, a: float, b: float) -> float:
    """Beta(a, b) pdf at x; 0 at the boundaries with infinite
    density (returns a large finite proxy to keep Newton iter
    well-defined)."""
    if x <= 0.0 or x >= 1.0:
        return 0.0
    log_pdf = (
        (a - 1.0) * math.log(x)
        + (b - 1.0) * math.log(1.0 - x)
        - _log_beta_func(a, b)
    )
    if log_pdf > 700.0:
        return math.exp(700.0)
    return math.exp(log_pdf)


def _regularised_incomplete_beta(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta I_x(a, b) — i.e. the CDF of
    Beta(a, b) evaluated at x.

    Continued-fraction expansion (Numerical Recipes §6.4). Works
    well across the entire (0, 1) range with a single-pass
    convergence; we use the symmetry ``I_x(a,b) = 1 − I_{1−x}(b,a)``
    when ``x > (a+1)/(a+b+2)`` for better convergence.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # Use the symmetry to keep the continued fraction
    # in the rapidly-converging regime.
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _regularised_incomplete_beta(1.0 - x, b, a)
    # Front factor: x^a (1-x)^b / (a B(a, b))
    log_front = (
        a * math.log(x)
        + b * math.log(1.0 - x)
        - _log_beta_func(a, b)
        - math.log(a)
    )
    front = math.exp(log_front)
    # Continued-fraction expansion (Lentz's algorithm).
    eps = 1e-12
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return front * h


def _beta_inv_cdf(p: float, a: float, b: float) -> float:
    """Inverse CDF of Beta(a, b) at probability ``p``.

    Seeds from a Cornish-Fisher-style normal approximation on the
    logit scale, then runs Newton iterations on the regularised
    incomplete beta. Clamps to ``(1e-9, 1 - 1e-9)`` so callers
    never get exact boundaries (which break log-probabilities).
    """
    if p <= 0.0:
        return 1e-9
    if p >= 1.0:
        return 1.0 - 1e-9
    if a <= 0.0 or b <= 0.0:
        raise ValueError(f"Beta shape parameters must be > 0; got a={a}, b={b}")

    # Seed via normal approximation on the mean, then clamp.
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1.0))
    std = math.sqrt(max(var, 1e-12))
    # Standard-normal quantile (Beasley-Springer / Moro approx).
    z = _normal_inv_cdf(p)
    x = mean + z * std
    x = min(max(x, 1e-6), 1.0 - 1e-6)

    # Refine via Newton's method on F(x) - p = 0.
    for _ in range(50):
        cdf_x = _regularised_incomplete_beta(x, a, b)
        pdf_x = _beta_pdf(x, a, b)
        if pdf_x <= 0.0:
            break
        step = (cdf_x - p) / pdf_x
        # Damp the step so we never leave (0, 1).
        new_x = x - step
        if new_x <= 0.0 or new_x >= 1.0:
            new_x = (x + (0.0 if step > 0 else 1.0)) / 2.0
        if abs(new_x - x) < 1e-10:
            x = new_x
            break
        x = new_x
    return min(max(x, 1e-9), 1.0 - 1e-9)


def _normal_inv_cdf(p: float) -> float:
    """Beasley-Springer-Moro inverse normal CDF; accurate to ~1e-9
    across all p ∈ (0, 1). Used as a seed for the Beta Newton iter."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a_arr = [
        -3.969683028665376e1,  2.209460984245205e2,
        -2.759285104469687e2,  1.383577518672690e2,
        -3.066479806614716e1,  2.506628277459239e0,
    ]
    b_arr = [
        -5.447609879822406e1,  1.615858368580409e2,
        -1.556989798598866e2,  6.680131188771972e1,
        -1.328068155288572e1,
    ]
    c_arr = [
        -7.784894002430293e-3, -3.223964580411365e-1,
        -2.400758277161838e0,  -2.549732539343734e0,
         4.374664141464968e0,   2.938163982698783e0,
    ]
    d_arr = [
         7.784695709041462e-3,  3.224671290700398e-1,
         2.445134137142996e0,   3.754408661907416e0,
    ]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (
            ((((c_arr[0]*q + c_arr[1])*q + c_arr[2])*q + c_arr[3])*q
                + c_arr[4])*q + c_arr[5]
        ) / (
            (((d_arr[0]*q + d_arr[1])*q + d_arr[2])*q + d_arr[3])*q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(
            ((((c_arr[0]*q + c_arr[1])*q + c_arr[2])*q + c_arr[3])*q
                + c_arr[4])*q + c_arr[5]
        ) / (
            (((d_arr[0]*q + d_arr[1])*q + d_arr[2])*q + d_arr[3])*q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a_arr[0]*r + a_arr[1])*r + a_arr[2])*r + a_arr[3])*r
            + a_arr[4])*r + a_arr[5]) * q
    ) / (
        ((((b_arr[0]*r + b_arr[1])*r + b_arr[2])*r + b_arr[3])*r
            + b_arr[4])*r + 1.0
    )


# ──────────────────────────────────────────────────────────────────
# Diagnostics
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrustScoreBreakdown:
    """Detailed accounting of how a posterior's trust score was
    formed. Exposed for ``aegis autonomy show`` so operators can
    see *why* a pattern is or isn't trusted, not just the number."""

    mean: float
    std: float
    lcb: float
    credibility: float
    n_effective: float
    prior_alpha: float
    prior_beta: float
    posterior_alpha: float
    posterior_beta: float
    width_of_credible_interval: float = field(init=False)

    def __post_init__(self) -> None:
        # 90% two-sided credible interval width — a compact way to
        # report posterior uncertainty in CLI output.
        object.__setattr__(
            self, "width_of_credible_interval", 2.0 * 1.645 * self.std,
        )


def trust_breakdown(
    posterior: BetaPosterior,
    *,
    credibility: float = DEFAULT_CREDIBILITY,
) -> TrustScoreBreakdown:
    """Return a human-readable breakdown of a posterior's trust."""
    return TrustScoreBreakdown(
        mean=posterior.mean,
        std=posterior.std,
        lcb=posterior.lower_credible_bound(credibility),
        credibility=credibility,
        n_effective=posterior.n_effective,
        prior_alpha=posterior.prior_alpha,
        prior_beta=posterior.prior_beta,
        posterior_alpha=posterior.alpha,
        posterior_beta=posterior.beta,
    )


__all__ = [
    "BetaPosterior",
    "DEFAULT_CREDIBILITY",
    "DEFAULT_PRIOR_ALPHA",
    "DEFAULT_PRIOR_BETA",
    "ToolBaseline",
    "TrustScoreBreakdown",
    "adjusted_min_samples",
    "empirical_bayes_prior",
    "make_posterior",
    "trust_breakdown",
]
