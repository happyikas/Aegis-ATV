"""Train/validation calibration check for the trust table.

When a learning pipeline reports "trust = 0.95", you want that to
mean: "if you bypass 100 patterns at this trust level, ~95 will be
clean and ~5 will go wrong." That's *calibration*. A miscalibrated
classifier is dangerous because the operator's mental model
("0.95 = nearly always safe") no longer maps to reality.

This module implements two checks shipped with every learn pass:

1. **Holdout split (80/20).** Train posteriors on 80% of the
   burn-in window. On the held-out 20%, count how often a pattern
   trusted on train was *also* clean on val. If the empirical val
   accuracy at the trusted set is materially worse than the
   trained LCB predicted, we're overfit.

2. **Expected calibration error (ECE).** Bucket trust predictions
   by [0, 0.5), [0.5, 0.85), [0.85, 1.0]. For each bucket, compute
   ``|predicted_rate − empirical_rate|`` weighted by bucket size.
   ECE > 0.10 ⇒ the trust table is unreliable and the learner
   refuses to ship it.

Both checks are *integral to the learn pass*. If they fail, the
trust table is *not* written to disk; the previous snapshot stays
authoritative. The operator is told why the new snapshot was
rejected.

Split strategy: by ``trace_id`` hash mod 5 (0–3 = train, 4 = val).
Hash-based splitting (rather than temporal) avoids the leakage
where val data is systematically newer than train. Patterns
that don't appear in val simply don't contribute to the
calibration check — that's fine, they're just untestable on this
window.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Final

from aegis.autonomy.bayesian import BetaPosterior

# ──────────────────────────────────────────────────────────────────
# Public configuration
# ──────────────────────────────────────────────────────────────────

# 20% of records held out for calibration measurement. Trace_id
# hash mod 5 → bucket 4 is val.
HOLDOUT_FRACTION: Final[float] = 0.20

# Maximum tolerable expected calibration error. 0.10 = at most a
# 10-percentage-point gap between predicted and empirical
# success rate, averaged over buckets weighted by bucket size.
ECE_THRESHOLD: Final[float] = 0.10

# Trust buckets used for the ECE histogram. Aligned with the
# operator's mental anchors (0.50, 0.85).
_BUCKET_EDGES: Final[tuple[float, ...]] = (0.0, 0.5, 0.85, 1.0)


def trace_split(trace_id: str) -> str:
    """Return ``"train"`` or ``"val"`` for a given trace_id.

    Uses BLAKE2b mod 5 to derive the bucket; bucket 4 (20%) is val,
    the rest is train. Deterministic across runs so the same record
    set always splits the same way."""
    if not trace_id:
        return "train"
    h = hashlib.blake2b(trace_id.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(h, "big") % 5
    return "val" if bucket == 4 else "train"


# ──────────────────────────────────────────────────────────────────
# Calibration metrics
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BucketStat:
    """One row of the calibration histogram."""

    lo: float
    hi: float
    n: int = 0
    predicted_sum: float = 0.0
    empirical_clean: int = 0

    @property
    def predicted_mean(self) -> float:
        return self.predicted_sum / self.n if self.n > 0 else 0.0

    @property
    def empirical_rate(self) -> float:
        return self.empirical_clean / self.n if self.n > 0 else 0.0

    @property
    def calibration_gap(self) -> float:
        return abs(self.predicted_mean - self.empirical_rate)


@dataclass(frozen=True)
class CalibrationReport:
    """Result of running the holdout calibration check."""

    n_train: int
    n_val: int
    buckets: tuple[BucketStat, ...]
    expected_calibration_error: float
    passed: bool
    rejection_reason: str = field(default="")


def expected_calibration_error(buckets: tuple[BucketStat, ...]) -> float:
    """Weighted-average calibration gap.

    ECE = Σ (nₖ / N) × |predᵏ − empᵏ| over buckets k. Total N
    excludes empty buckets so a single-bucket result isn't penalised
    by inert zeros."""
    total = sum(b.n for b in buckets)
    if total == 0:
        return 0.0
    return sum(
        (b.n / total) * b.calibration_gap for b in buckets if b.n > 0
    )


def _bucket_for(score: float) -> int:
    """Return the index of the bucket that ``score`` falls into."""
    for i in range(len(_BUCKET_EDGES) - 1):
        if _BUCKET_EDGES[i] <= score < _BUCKET_EDGES[i + 1]:
            return i
    return len(_BUCKET_EDGES) - 2  # score == 1.0 falls in last bucket


def compute_calibration(
    *,
    predictions: dict[tuple[str, str], BetaPosterior],
    val_outcomes: dict[tuple[str, str], tuple[int, int]],
    credibility: float,
    ece_threshold: float = ECE_THRESHOLD,
) -> CalibrationReport:
    """Build the calibration report.

    Inputs:
        predictions: per-pattern train-set posteriors (the trust
            table candidate).
        val_outcomes: per-pattern ``(n_clean_val, n_total_val)``
            from the held-out 20%. Patterns missing from val are
            silently dropped from the calibration check.
        credibility: which posterior quantile counts as the
            "prediction" — should match the runtime's decision
            metric. Typically 0.95 (lower 5% bound).
        ece_threshold: maximum acceptable ECE; above this the
            report is marked ``passed = False``.

    The 'prediction' for each pattern is its LCB at the given
    credibility — this is what the runtime will compare against
    ``min_trust``. Calibration of the LCB (not the mean) is what
    matters for the bypass decision."""

    buckets: list[list[float | int]] = [
        [_BUCKET_EDGES[i], _BUCKET_EDGES[i + 1], 0, 0.0, 0]
        for i in range(len(_BUCKET_EDGES) - 1)
    ]

    total_val_records = 0
    total_train_records = 0

    for key, post in predictions.items():
        train_n = int(post.n_effective)
        total_train_records += train_n
        if key not in val_outcomes:
            continue
        n_clean_val, n_total_val = val_outcomes[key]
        if n_total_val <= 0:
            continue
        total_val_records += n_total_val
        # Each val record is one Bernoulli trial whose predicted
        # success rate is the LCB. Bucket the prediction once per
        # val record so populous patterns dominate ECE — which
        # matches what the runtime actually does.
        prediction = post.lower_credible_bound(credibility)
        idx = _bucket_for(prediction)
        bucket = buckets[idx]
        bucket[2] = int(bucket[2]) + n_total_val
        bucket[3] = float(bucket[3]) + prediction * n_total_val
        bucket[4] = int(bucket[4]) + n_clean_val

    bucket_stats = tuple(
        BucketStat(
            lo=float(b[0]),
            hi=float(b[1]),
            n=int(b[2]),
            predicted_sum=float(b[3]),
            empirical_clean=int(b[4]),
        )
        for b in buckets
    )
    ece = expected_calibration_error(bucket_stats)

    if total_val_records < 5:
        # Not enough val data to measure — pass by convention but
        # mark it. The CLI shows this as a warning.
        return CalibrationReport(
            n_train=total_train_records,
            n_val=total_val_records,
            buckets=bucket_stats,
            expected_calibration_error=ece,
            passed=True,
            rejection_reason="(val set < 5 records — calibration skipped)",
        )

    passed = ece <= ece_threshold
    return CalibrationReport(
        n_train=total_train_records,
        n_val=total_val_records,
        buckets=bucket_stats,
        expected_calibration_error=ece,
        passed=passed,
        rejection_reason=(
            ""
            if passed
            else f"ECE {ece:.3f} exceeds threshold {ece_threshold:.3f}"
        ),
    )


__all__ = [
    "BucketStat",
    "CalibrationReport",
    "ECE_THRESHOLD",
    "HOLDOUT_FRACTION",
    "compute_calibration",
    "expected_calibration_error",
    "trace_split",
]
