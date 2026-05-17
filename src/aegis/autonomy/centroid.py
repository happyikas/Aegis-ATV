"""Per-pattern ATV centroid + Mahalanobis distance gate (v0.5.23).

Autonomy idea #2 from the original roadmap: ``trust_score`` is
based on (tool, reason_signature) — two coarse features. The
Bayesian backbone makes that more rigorous, but it still ignores
the *runtime fingerprint* of a call: how long does it take, how
much does it cost, how many tokens does it consume?

A call that's nominally on a trusted pattern but whose runtime
fingerprint is wildly outside the cluster of clean historical
calls is suspicious. v0.5.23 adds an embedding-distance gate to
catch this case: per-pattern centroid + diagonal covariance in a
3-D feature space (log cost / log tokens-in / log latency).
Mahalanobis-diagonal distance > threshold (3σ default) ⇒ refuse
bypass regardless of trust score.

### Why a low-D fingerprint rather than the full 2080-D ATV

* The 2080-D ATV vector isn't persisted in ContextMemory — only
  the ``atv_sha3`` hash is. Lifting full vectors into the
  autonomy learner would require a schema migration that this
  PR avoids.
* The 3-D fingerprint (cost, tokens, latency) captures most of
  the runtime variance that matters for "this call is unusually
  expensive / slow / large". Token-efficiency and cost-divergence
  patterns dominate the autonomy outliers operators care about.
* Diagonal covariance keeps storage O(d) rather than O(d²). For
  3 dimensions per pattern, that's 6 floats — negligible vs the
  rest of the trust-table entry.

A future v0.6 PR can lift the full 2080-D ATV via an opt-in
storage extension; the v0.5.23 surface stays the same.

### Distance metric

Mahalanobis-diagonal: ``d² = Σ ((x_i - μ_i)² / max(σ_i², ε))``.
Diagonal because we don't track off-diagonal covariance.
``ε = 1e-6`` floors a sigma=0 from a degenerate (all-identical)
sample so the distance is well-defined.

### Defensive contract

* Never raises — the autonomy bypass is on the hot path.
* Returns ``inf`` (always refuses) when the centroid is missing /
  empty / NaN-laden — safer to keep the human in the loop than
  to silently accept a degenerate centroid.
* Accepts zero-variance dimensions by flooring sigma at ``ε``.
"""

from __future__ import annotations

import math
from typing import Final

from aegis.context_memory.record import ContextMemoryRecord

# Feature vector dimensionality. Stored separately from the per-
# pattern arrays so a future v0.6 extension can stage a wider
# fingerprint without breaking existing trust tables.
FEATURE_DIM: Final[int] = 3
"""Number of dimensions in the runtime fingerprint:
``(log_cost, log_tokens_in, log_latency)``."""

DEFAULT_MAHALANOBIS_THRESHOLD: Final[float] = 3.0
"""Distance above which a call is considered "outside the
cluster" and the bypass is refused. 3σ is the conventional
boundary; raise to 4-5 for looser gating, lower to 2 for
strict."""

_SIGMA_FLOOR: Final[float] = 1e-6


def feature_vector(record: ContextMemoryRecord) -> tuple[float, ...]:
    """Extract the runtime fingerprint for one record. Returns a
    3-tuple of ``(log_cost, log_tokens_in, log_latency)``.

    All dimensions are log-transformed because the underlying
    distributions are heavy-tailed (a small constant is added to
    keep ``log(0)`` finite). Records with missing data contribute
    zeros to that dimension — they don't poison the centroid as
    long as the missing-rate is uniform across the pattern."""
    cost = float(record.cost_usd or 0.0)
    tokens_in = int(record.tokens_in or 0)
    latency_ms = float(record.latency_ms or 0.0)
    return (
        math.log(cost + 1e-6),
        math.log(tokens_in + 1.0),
        math.log(latency_ms + 1.0),
    )


def feature_vector_from_signals(
    *,
    cost_usd: float = 0.0,
    tokens_in: int = 0,
    latency_ms: float = 0.0,
) -> tuple[float, ...]:
    """Runtime construction at the hook. Same transform as
    :func:`feature_vector` but takes raw signals so the hook
    doesn't have to fabricate a synthetic ContextMemoryRecord."""
    return (
        math.log(float(cost_usd or 0.0) + 1e-6),
        math.log(int(tokens_in or 0) + 1.0),
        math.log(float(latency_ms or 0.0) + 1.0),
    )


def compute_centroid_and_cov(
    features: list[tuple[float, ...]],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Per-dimension mean + variance from a list of feature
    vectors. Returns ``(centroid, cov_diag)``.

    Empty input returns ``((), ())``. Single-sample input returns
    the centroid + a zero-variance vector — the caller is
    expected to skip Mahalanobis gating until at least
    ``MIN_SAMPLES_FOR_CENTROID`` samples have been collected.
    Variance uses the population formula (divide by ``n``, not
    ``n-1``) because the centroid is the maximum-likelihood
    estimate, not an inference about a larger population."""
    if not features:
        return (), ()
    d = len(features[0])
    # All rows must agree on dimensionality; mismatched rows are
    # silently dropped to keep this defensive.
    rows = [f for f in features if len(f) == d]
    n = len(rows)
    if n == 0:
        return (), ()
    means = [0.0] * d
    for row in rows:
        for i, v in enumerate(row):
            means[i] += v
    means = [m / n for m in means]
    cov_diag = [0.0] * d
    for row in rows:
        for i, v in enumerate(row):
            dv = v - means[i]
            cov_diag[i] += dv * dv
    cov_diag = [c / n for c in cov_diag]
    return tuple(means), tuple(cov_diag)


def mahalanobis_distance_diag(
    point: tuple[float, ...],
    centroid: tuple[float, ...],
    cov_diag: tuple[float, ...],
) -> float:
    """Mahalanobis-diagonal distance from ``point`` to ``centroid``.

    Returns ``inf`` if any input is malformed (empty centroid,
    mismatched dimensions, NaN entries). The bypass treats ``inf``
    as "refuse" so a degenerate centroid keeps the human in the
    loop rather than silently accepting."""
    if not point or not centroid or not cov_diag:
        return math.inf
    if len(point) != len(centroid) or len(centroid) != len(cov_diag):
        return math.inf
    total = 0.0
    for p, c, v in zip(point, centroid, cov_diag, strict=True):
        if any(math.isnan(x) for x in (p, c, v)):
            return math.inf
        sigma_sq = max(v, _SIGMA_FLOOR)
        delta = p - c
        total += (delta * delta) / sigma_sq
    return math.sqrt(total)


def is_outside_cluster(
    point: tuple[float, ...],
    centroid: tuple[float, ...],
    cov_diag: tuple[float, ...],
    *,
    threshold: float = DEFAULT_MAHALANOBIS_THRESHOLD,
) -> bool:
    """Convenience: distance > threshold ⇒ outside.

    Returns False (do not refuse) if either the centroid is empty
    (no fingerprint collected yet for this pattern) or the
    feature point itself is empty — i.e. v0.5.22 trust tables
    that predate the centroid extension. The gate fires only
    when the centroid is populated AND the point is well-defined."""
    if not centroid or not cov_diag or not point:
        return False
    dist = mahalanobis_distance_diag(point, centroid, cov_diag)
    return dist > threshold


__all__ = [
    "DEFAULT_MAHALANOBIS_THRESHOLD",
    "FEATURE_DIM",
    "compute_centroid_and_cov",
    "feature_vector",
    "feature_vector_from_signals",
    "is_outside_cluster",
    "mahalanobis_distance_diag",
]
