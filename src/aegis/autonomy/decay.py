"""Exponential decay of historical evidence (v0.5.12).

v0.5.11 treated every observation in the burn-in window as
equally informative. That ignores two well-documented training
pathologies:

* **Catastrophic staleness.** A pattern observed 50× three months
  ago but 0× in the last week is *not* "trusted" — the operator's
  workflow may have changed. Equal weighting cements old habits.
* **Concept drift insensitivity.** If the agent or repo shifts
  (new code style, new tooling), recent observations carry more
  information than old ones. Equal weighting suppresses the drift
  signal until it overwhelms history.

This module exponentially down-weights observations by age, with
a configurable half-life. Default half-life is 30 days: an
observation from 30 days ago counts as ½; 60 days ago, ¼; 90
days ago, ⅛. The half-life is the operator's primary knob for
"how much do I trust last quarter vs last week".

The decay is applied at *learning* time only, not at runtime.
The trust table snapshot is the result of one weighted-sum pass
over the burn-in window. We deliberately do NOT decay the
posterior continuously after learning — that would invalidate the
audit property "this trust table was the function of this exact
input record set". Re-learning is the operator-explicit way to
refresh.

Math: given an observation at ``ts_ns_obs`` and a learning anchor
``ts_ns_anchor`` (typically ``now``), the weight is::

    weight = exp(-ln(2) * Δdays / half_life_days)

where ``Δdays = (ts_ns_anchor - ts_ns_obs) / 1e9 / 86400``. Old
observations approach 0 weight asymptotically but never reach it
exactly; the learner can apply a floor (e.g. drop weights below
0.01) to bound the trust-table size.
"""

from __future__ import annotations

import math
from typing import Final

_LN2: Final[float] = math.log(2.0)
_NS_PER_DAY: Final[float] = 86_400.0 * 1e9

# Sensible defaults — exposed so callers can tune.
DEFAULT_HALF_LIFE_DAYS: Final[float] = 30.0
DEFAULT_MIN_WEIGHT: Final[float] = 0.01  # 7 half-lives ≈ 0.78%


def decay_weight(
    *,
    ts_ns_observed: int,
    ts_ns_anchor: int,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Return the decay weight ``∈ (0, 1]`` for one observation.

    ``ts_ns_observed`` is when the observation occurred.
    ``ts_ns_anchor`` is the reference (typically learn-call ``now``).
    A future-dated observation (anchor < observed) is treated as
    weight 1.0 — we never *up*-weight to avoid amplifying noise.

    ``half_life_days`` must be > 0. Passing ``math.inf`` disables
    decay entirely (returns 1.0 for all observations); useful for
    bug-for-bug compat with the v0.5.11 trust learner."""
    if half_life_days <= 0.0:
        raise ValueError(
            f"half_life_days must be positive; got {half_life_days}"
        )
    if math.isinf(half_life_days):
        return 1.0
    if ts_ns_anchor <= ts_ns_observed:
        return 1.0
    delta_days = (ts_ns_anchor - ts_ns_observed) / _NS_PER_DAY
    weight = math.exp(-_LN2 * delta_days / half_life_days)
    return max(0.0, min(1.0, weight))


def should_drop(
    weight: float,
    *,
    min_weight: float = DEFAULT_MIN_WEIGHT,
) -> bool:
    """Return True if a weight is small enough to skip.

    Used by the learner to keep the trust-table size bounded:
    extremely old observations contribute essentially nothing,
    and dropping them keeps the per-pattern bucket O(recent)
    rather than O(all-time)."""
    return weight < min_weight


def half_life_from_env(default: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Read ``AEGIS_AUTONOMY_HALF_LIFE_DAYS`` from the environment.

    Falls back to ``default`` on missing / empty / unparseable.
    Defensive: never raises so the import-time autonomy bootstrap
    can't crash a hook process."""
    import os
    raw = os.environ.get("AEGIS_AUTONOMY_HALF_LIFE_DAYS", "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        if v <= 0.0:
            return default
        return v
    except ValueError:
        return default


__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_MIN_WEIGHT",
    "decay_weight",
    "half_life_from_env",
    "should_drop",
]
