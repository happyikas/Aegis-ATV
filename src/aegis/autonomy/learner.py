"""Autonomy learner — burn-in trust pattern miner (v0.5.12).

Closes the user-experience gap: every REQUIRE_APPROVAL today
interrupts the autonomous agent and asks the operator for an
allow/deny click. For a frequent, routine pattern (e.g. "loop
detector fired on Bash" — operator always ends up letting it
proceed after pausing), this becomes pure friction.

This module observes the burn-in window of historical decisions
and learns which REQUIRE_APPROVAL patterns the operator has
seen+handled often enough to trust. At runtime, when a matching
pattern fires:

* The firewall would normally emit REQUIRE_APPROVAL (human in
  the loop).
* The autonomy bypass downgrades it to ALLOW, **but stamps the
  ATV record so the auto-approval is permanently traceable**.
* `aegis doctor` and `aegis autonomy outliers` can grep these
  stamps and surface any auto-approval that turned out poorly —
  closing the audit / postmortem loop.

v0.5.12 — Bayesian backbone
---------------------------

v0.5.11 used a point-estimate ``clean_rate`` and a soft sample-size
weight to score patterns. That formulation was vulnerable to the
classic ML training side-effects:

* small-sample **overfit** (5-of-5 ⇒ trust ≈ 0.70);
* **self-confirming** bias once bypass is active;
* **catastrophic staleness** for old patterns;
* **distribution drift** invisibility;
* **reward sparsity** with binary clean / not-clean signal;
* **calibration drift** — trust=0.95 didn't necessarily mean 95%.

v0.5.12 replaces the scoring with a Bayesian backbone, all of
which is shipped as separate modules and orchestrated here:

* :mod:`aegis.autonomy.bayesian` — Beta(α, β) posterior + lower
  credible bound (LCB). Naturally regularises low-n patterns.
* :mod:`aegis.autonomy.reward` — ternary reward shaping (CLEAN,
  BLOCK_FOLLOWUP, EXPLICIT_DENY). One operator deny costs ten
  cleans to recover.
* :mod:`aegis.autonomy.decay` — exponential decay (30-day default
  half-life) on observation weights. Stale evidence fades.
* :mod:`aegis.autonomy.calibration` — 80/20 train/val split +
  ECE check. The learner refuses to ship a miscalibrated table.
* :mod:`aegis.autonomy.drift` — KL between baseline (older 2/3)
  and recent (newer 1/3) posteriors. Drifted patterns lose trust.

Trust criteria for v0.5.12:

1. **Sample count** — ``n_effective ≥ min_samples`` (Bonferroni-
   adjusted upward when many candidate patterns are evaluated
   simultaneously).
2. **LCB(95%) ≥ min_trust** (default 0.85). Replaces the point
   ``clean_rate * sample_weight`` from v0.5.11. Small samples have
   wide posteriors ⇒ low LCB ⇒ no trust. No manual heuristics.
3. **Not in the never-trust filter** — dangerous_pattern,
   git_destructive, cloud_destructive, sensitive_path, budget.
4. **Not drifted** — Jensen-Shannon divergence between baseline
   and recent posterior below the drift threshold.
5. **Trust table passes calibration** — ECE on the val split is
   below the threshold or the trust table is rejected wholesale.

Behavior is opt-in via ``AEGIS_AUTONOMY_ENABLED=1`` (env). The
firewall step331_autonomy will be a no-op when the env flag is
not set, so existing deployments see byte-identical behavior.

Backward compatibility — :class:`TrustedPattern` retains every
v0.5.11 field; the new posterior fields are additive with sane
defaults. v0.5.11 trust tables continue to load (clean_rate +
trust_score remain authoritative for those entries; LCB and the
posterior shape parameters get re-derived from the integer counts
the first time the table is consulted).
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from aegis.autonomy.bayesian import (
    DEFAULT_CREDIBILITY,
    DEFAULT_PRIOR_ALPHA,
    DEFAULT_PRIOR_BETA,
    BetaPosterior,
    ToolBaseline,
    adjusted_min_samples,
    empirical_bayes_prior,
    make_posterior,
)
from aegis.autonomy.calibration import (
    ECE_THRESHOLD,
    CalibrationReport,
    compute_calibration,
    trace_split,
)
from aegis.autonomy.decay import (
    DEFAULT_HALF_LIFE_DAYS,
    decay_weight,
    should_drop,
)
from aegis.autonomy.denials import load_denial_trace_ids
from aegis.autonomy.drift import (
    DEFAULT_DRIFT_THRESHOLD,
    is_drifted,
    jensen_shannon_beta,
)
from aegis.autonomy.reward import (
    WEIGHT_BLOCK_FOLLOWUP,
    WEIGHT_CLEAN,
    WEIGHT_EXPLICIT_DENY,
    RewardCounts,
    RewardEvent,
    classify_record,
)
from aegis.context_memory.record import ContextMemoryRecord

# ──────────────────────────────────────────────────────────────────
# Configuration constants
# ──────────────────────────────────────────────────────────────────

DEFAULT_MIN_SAMPLES: Final[int] = 5
DEFAULT_MIN_CLEAN_RATE: Final[float] = 0.95

# Reason prefixes / substrings that NEVER qualify for auto-bypass.
# Even when an operator has seen + cleared these many times during
# burn-in, the patent + safety contract require keeping the human
# in the loop for these. The cost of one extra approval click is
# tiny vs the cost of an auto-bypassed destructive-action.
_NEVER_TRUST_SUBSTRINGS: Final[tuple[str, ...]] = (
    "dangerous pattern",
    "rule:dangerous",
    "rule:git_destructive",     # git force-push, rebase main, etc.
    "rule:cloud_destructive",   # kubectl delete, terraform destroy
    "sensitive path",
    "cumulative_dollars",       # budget gate — operator must see
)


def autonomy_enabled() -> bool:
    """Return True iff the autonomy bypass is engaged at runtime.

    Off by default (v0.5.11 contract). Operators opt in via
    ``AEGIS_AUTONOMY_ENABLED=1``. Tests + replay never hit the
    bypass path unless this flag is set."""
    raw = os.environ.get("AEGIS_AUTONOMY_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrustedPattern:
    """A REQUIRE_APPROVAL pattern that's appeared often enough +
    cleanly enough to be auto-bypassed.

    ``reason_signature`` is a canonical form of the firewall's
    REQUIRE_APPROVAL reason — see :func:`reason_signature`. Two
    distinct reasons that share the same root cause (e.g. all
    "same X call repeated N times" loop reasons regardless of N)
    collapse to one signature so the learner can build statistics.

    v0.5.12 fields (Bayesian posterior + drift) are additive and
    have safe defaults so v0.5.11 trust tables continue to load.
    ``trust_score`` becomes the **LCB of the posterior** rather
    than the v0.5.11 ``clean_rate * sample_weight``; the runtime
    threshold of 0.85 means "95% confident the true rate is ≥ 0.85"
    (vs. the prior interpretation "rate × sample_weight ≥ 0.85").
    """

    tool_name: str
    reason_signature: str
    n_seen: int
    n_followed_by_block: int
    clean_rate: float
    trust_score: float            # 0..1 — LCB on Beta posterior
    last_seen_ns: int
    sample_trace_ids: tuple[str, ...] = field(default_factory=tuple)

    # v0.5.12 — Bayesian posterior. Optional for backward compat
    # with v0.5.11 tables (which set these to 0.0 / None).
    alpha: float = 0.0
    beta: float = 0.0
    posterior_mean: float = 0.0
    posterior_std: float = 0.0
    n_effective: float = 0.0
    n_explicit_deny: int = 0
    drift_score: float = 0.0
    drifted: bool = False
    credibility: float = DEFAULT_CREDIBILITY
    prior_alpha: float = DEFAULT_PRIOR_ALPHA
    prior_beta: float = DEFAULT_PRIOR_BETA

    # v0.5.23 — runtime-fingerprint centroid + diagonal covariance.
    # 3-D vector of (log_cost, log_tokens_in, log_latency). Empty
    # tuples for v0.5.22-and-earlier patterns (no Mahalanobis gating).
    atv_centroid: tuple[float, ...] = ()
    atv_cov_diag: tuple[float, ...] = ()
    centroid_n_samples: int = 0

    @property
    def key(self) -> tuple[str, str]:
        """Stable lookup key for the trust table."""
        return (self.tool_name, self.reason_signature)

    def posterior(self) -> BetaPosterior:
        """Reconstruct the Beta posterior. v0.5.11 records get
        a posterior derived from their integer counts so the
        runtime decision rule (LCB) is well-defined for them too."""
        if self.alpha > 0.0 and self.beta > 0.0:
            return BetaPosterior(
                alpha=self.alpha,
                beta=self.beta,
                n_clean=max(0.0, self.n_seen - self.n_followed_by_block),
                n_block=float(self.n_followed_by_block),
                n_deny=float(self.n_explicit_deny),
                prior_alpha=self.prior_alpha,
                prior_beta=self.prior_beta,
            )
        # v0.5.11 fallback: derive a posterior with the default
        # prior so the LCB decision rule still applies. This is
        # *conservative* — the LCB will be lower than v0.5.11's
        # trust_score, so old patterns may temporarily drop below
        # the bypass threshold until they're re-learned.
        n_clean = float(max(0, self.n_seen - self.n_followed_by_block))
        n_block = float(self.n_followed_by_block)
        return make_posterior(
            n_clean=n_clean,
            n_block=n_block,
            n_deny=0.0,
            weight_clean=WEIGHT_CLEAN,
            weight_block=WEIGHT_BLOCK_FOLLOWUP,
        )


@dataclass(frozen=True)
class AutonomyVerdict:
    """Outcome of consulting the trust table for one
    REQUIRE_APPROVAL event."""

    auto_approve: bool
    matched_pattern: TrustedPattern | None
    confidence: float             # 0..1; matched_pattern.trust_score
                                  # when matched, 0.0 otherwise
    reason: str                   # operator-facing explanation
    outlier_signals: tuple[str, ...] = field(default_factory=tuple)


# ──────────────────────────────────────────────────────────────────
# Reason signature canonicalisation
# ──────────────────────────────────────────────────────────────────

_LOOP_RE: Final[re.Pattern[str]] = re.compile(
    r"^same (\w+) call repeated (\d+) times this session"
)
_DOLLAR_RE: Final[re.Pattern[str]] = re.compile(
    r"^cumulative_dollars\s+[\d.]+\s*>\s*budget"
)


def reason_signature(reason: str) -> str:
    """Canonical form of a firewall REQUIRE_APPROVAL reason.

    Different concrete reasons that share a root cause map to the
    same signature so the learner can build N-sample statistics.

    Examples:
      "same Bash call repeated 3 times this session (threshold=3)"
        → "loop:Bash"
      "same Bash call repeated 5 times this session (threshold=3)"
        → "loop:Bash"
      "cumulative_dollars 56549.4134 > budget 1.0000"
        → "budget"
      "rule:prompt_injection"
        → "rule:prompt_injection"
      "cost-divergence escalation: token_to_flops = 1.000 > ..."
        → "cost-divergence"
    """
    if not reason:
        return "(empty)"

    m = _LOOP_RE.match(reason)
    if m:
        return f"loop:{m.group(1)}"

    if _DOLLAR_RE.match(reason):
        return "budget"

    if reason.startswith("cost-divergence"):
        return "cost-divergence"

    if reason.startswith("rule:"):
        # rule:foo → rule:foo (keep specific rule names)
        return reason.split()[0]

    if "sensitive path" in reason:
        return "sensitive_path"

    if "dangerous pattern" in reason:
        return "dangerous_pattern"

    # Generic fallback: first 3 tokens. Keeps cardinality
    # bounded so the trust table stays small.
    return " ".join(reason.split()[:3])


def _is_never_trust(reason: str) -> bool:
    """Return True if this reason string contains any
    never-trust substring — these are never auto-bypassed
    regardless of burn-in observation count."""
    return any(s in reason for s in _NEVER_TRUST_SUBSTRINGS)


# ──────────────────────────────────────────────────────────────────
# Learner — observe burn-in window and build trust table
# ──────────────────────────────────────────────────────────────────


@dataclass
class _PatternBucket:
    """Mutable accumulator during the learning pass."""
    tool: str
    signature: str
    n_seen: int = 0
    n_followed_by_block: int = 0
    n_explicit_deny: int = 0
    last_seen_ns: int = 0
    sample_traces: list[str] = field(default_factory=list)
    # v0.5.12 — reward-shaped, decay-weighted counts. Computed in
    # parallel with the raw n_seen / n_followed_by_block so v0.5.11
    # readers keep working.
    train_counts: RewardCounts = field(default_factory=RewardCounts)
    val_counts: RewardCounts = field(default_factory=RewardCounts)
    # Per-window counts for drift detection: baseline = older 2/3,
    # recent = newer 1/3. Weights here are raw (no decay) — the
    # drift signal is about *change over time*, so applying decay
    # here would suppress the very thing we're trying to detect.
    baseline_counts: RewardCounts = field(default_factory=RewardCounts)
    recent_counts: RewardCounts = field(default_factory=RewardCounts)
    # v0.5.23 — runtime fingerprints from CLEAN records only. The
    # centroid + cov_diag are computed at the end of the bucket
    # pass; storing the per-record vectors here lets the learner
    # apply decay-weighting (TODO future) before the aggregation.
    clean_features: list[tuple[float, ...]] = field(default_factory=list)


@dataclass(frozen=True)
class LearnResult:
    """Structured result of :func:`learn_with_diagnostics`.

    The trust table is what the CLI persists; the diagnostics
    explain how the table was built — sample counts, drops by
    each gate, calibration metrics. The CLI uses this to print
    a transparent learn summary and to refuse persisting a
    miscalibrated table."""

    trust_table: dict[tuple[str, str], TrustedPattern]
    calibration: CalibrationReport
    n_records_scanned: int = 0
    n_patterns_considered: int = 0
    n_patterns_dropped_never_trust: int = 0
    n_patterns_dropped_low_samples: int = 0
    n_patterns_dropped_low_trust: int = 0
    n_patterns_dropped_drift: int = 0
    min_samples_effective: int = 0
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    learned_at_ns: int = 0
    credibility: float = DEFAULT_CREDIBILITY
    min_trust: float = 0.0


def _detect_block_followup_in_window(
    later_window: list[ContextMemoryRecord],
) -> bool:
    """v0.5.12 helper: given a pre-sliced subsequent window for
    one record (≤10 records within the same aid), return True
    iff any is a BLOCK. Cheaper than v0.5.11's repeated index
    lookup per record."""
    return any(r.decision == "BLOCK" for r in later_window)


def learn_with_diagnostics(
    records: Iterable[ContextMemoryRecord],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_trust: float = 0.85,
    credibility: float = DEFAULT_CREDIBILITY,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    ece_threshold: float = ECE_THRESHOLD,
    now_ns: int | None = None,
    apply_bonferroni: bool = True,
    apply_decay: bool = True,
    apply_drift: bool = True,
    apply_calibration: bool = True,
    denied_trace_ids: frozenset[str] | None = None,
) -> LearnResult:
    """Build the trust table with full Bayesian diagnostics.

    This is the v0.5.12 entry point. The bypass-safe defaults
    (Bonferroni, decay, drift, calibration all on) reflect what
    the operator gets from ``aegis autonomy learn``. Tests can
    disable individual safeguards via the boolean kwargs to
    pin-test one regularisation in isolation.

    Args:
        records: ContextMemory records covering the burn-in
            window (any iteration order — we sort internally).
        min_samples: base minimum effective sample count. The
            actual threshold may be larger after Bonferroni
            adjustment.
        min_trust: LCB threshold for inclusion. Default 0.85 means
            "95% confident the true success rate ≥ 0.85" when
            ``credibility=0.95``.
        credibility: posterior credibility for the LCB metric.
        half_life_days: exponential decay half-life.
        drift_threshold: Jensen-Shannon divergence above which a
            pattern is flagged as drifted and dropped.
        ece_threshold: expected calibration error above which the
            *entire* trust table is rejected.
        now_ns: anchor time for decay weighting. Defaults to
            ``time.time_ns()``.
        apply_*: turn off individual regularisations for tests /
            apples-to-apples comparisons.
    """
    rec_list = list(records)
    if not rec_list:
        return LearnResult(
            trust_table={},
            calibration=CalibrationReport(
                n_train=0, n_val=0,
                buckets=(),
                expected_calibration_error=0.0,
                passed=True,
                rejection_reason="(no records)",
            ),
            half_life_days=half_life_days,
            credibility=credibility,
            min_trust=min_trust,
            learned_at_ns=now_ns if now_ns is not None else time.time_ns(),
        )

    anchor_ns = now_ns if now_ns is not None else time.time_ns()

    # Load explicit denials. Caller can pass a pre-built set
    # (used by tests + the CLI when it wants to learn from a
    # synthetic denial list); default is the on-disk log.
    denials = (
        denied_trace_ids
        if denied_trace_ids is not None
        else load_denial_trace_ids()
    )

    # Index records by aid for the block-followup heuristic.
    by_aid: dict[str, list[ContextMemoryRecord]] = defaultdict(list)
    for r in rec_list:
        by_aid[r.aid].append(r)
    for aid in by_aid:
        by_aid[aid].sort(key=lambda r: r.ts_ns)

    # Determine the baseline / recent window split for drift
    # detection. We sort all REQUIRE_APPROVAL records by ts and
    # split 2/3 — 1/3.
    req_records = sorted(
        (r for r in rec_list if r.decision == "REQUIRE_APPROVAL"),
        key=lambda r: r.ts_ns,
    )
    if req_records:
        split_idx = (2 * len(req_records)) // 3
        baseline_cutoff_ns = (
            req_records[split_idx].ts_ns
            if split_idx < len(req_records)
            else req_records[-1].ts_ns
        )
    else:
        baseline_cutoff_ns = 0

    buckets: dict[tuple[str, str], _PatternBucket] = {}
    never_trust_keys: set[tuple[str, str]] = set()

    for r in rec_list:
        if r.decision != "REQUIRE_APPROVAL":
            continue
        sig = reason_signature(r.reason or "")
        key = (r.tool_name, sig)
        if _is_never_trust(r.reason or ""):
            never_trust_keys.add(key)
            continue

        # Build the subsequent-window for this record once.
        aid_timeline = by_aid[r.aid]
        idx = next(
            (i for i, x in enumerate(aid_timeline) if x.trace_id == r.trace_id),
            None,
        )
        followup_window = (
            aid_timeline[idx + 1 : idx + 11] if idx is not None else []
        )
        event = (
            classify_record(
                r,
                block_within=followup_window,
                denied_trace_ids=denials,
            )
            or RewardEvent.CLEAN
        )

        # Decay weight against anchor time.
        if apply_decay:
            w = decay_weight(
                ts_ns_observed=r.ts_ns,
                ts_ns_anchor=anchor_ns,
                half_life_days=half_life_days,
            )
            if should_drop(w):
                # Too old to contribute meaningfully — skip.
                continue
        else:
            w = 1.0

        b = buckets.get(key)
        if b is None:
            b = _PatternBucket(tool=r.tool_name, signature=sig)
            buckets[key] = b

        # Raw counts (v0.5.11 audit fields).
        b.n_seen += 1
        b.last_seen_ns = max(b.last_seen_ns, r.ts_ns)
        if len(b.sample_traces) < 3:
            b.sample_traces.append(r.trace_id)
        if event is RewardEvent.BLOCK_FOLLOWUP:
            b.n_followed_by_block += 1
        if event is RewardEvent.EXPLICIT_DENY:
            b.n_explicit_deny += 1

        # Reward-shaped, decay-weighted counts. Split into
        # train (80%) / val (20%) buckets for the calibration
        # check, and into baseline / recent windows for drift
        # detection.
        if trace_split(r.trace_id) == "val":
            b.val_counts = b.val_counts.add(event, weight=w)
        else:
            b.train_counts = b.train_counts.add(event, weight=w)
        if r.ts_ns < baseline_cutoff_ns:
            b.baseline_counts = b.baseline_counts.add(event, weight=1.0)
        else:
            b.recent_counts = b.recent_counts.add(event, weight=1.0)

        # v0.5.23 — collect runtime fingerprints for clean records
        # only. The centroid represents "how does a healthy
        # invocation of this pattern look?" — BLOCK followups and
        # denials would skew it toward the bad cluster.
        if event is RewardEvent.CLEAN:
            from aegis.autonomy.centroid import feature_vector
            b.clean_features.append(feature_vector(r))

    # Compute per-tool baselines for empirical-Bayes hierarchical
    # prior. Aggregate across all patterns of each tool, excluding
    # the never-trust ones.
    tool_baselines: dict[str, ToolBaseline] = _build_tool_baselines(
        buckets, never_trust_keys,
    )

    # Bonferroni-adjust min_samples by the number of candidate
    # patterns considered (excludes never-trust dropouts).
    n_candidates = sum(
        1 for k in buckets if k not in never_trust_keys
    )
    min_samples_eff = (
        adjusted_min_samples(min_samples, n_candidates)
        if apply_bonferroni
        else min_samples
    )

    # Build trust table from FULL counts (train + val) — the val
    # split is only used for the calibration audit, not for
    # withholding evidence from the production table.
    train_posteriors: dict[tuple[str, str], BetaPosterior] = {}
    val_outcomes: dict[tuple[str, str], tuple[int, int]] = {}
    out: dict[tuple[str, str], TrustedPattern] = {}

    dropped_never = 0
    dropped_samples = 0
    dropped_trust = 0
    dropped_drift = 0

    for key, b in buckets.items():
        if key in never_trust_keys:
            dropped_never += 1
            continue

        # Hierarchical prior — empirical-Bayes per tool.
        tb = tool_baselines.get(b.tool)
        if tb is not None:
            prior_alpha, prior_beta = empirical_bayes_prior(tb)
        else:
            prior_alpha, prior_beta = DEFAULT_PRIOR_ALPHA, DEFAULT_PRIOR_BETA

        full_counts = RewardCounts(
            n_clean=b.train_counts.n_clean + b.val_counts.n_clean,
            n_block=b.train_counts.n_block + b.val_counts.n_block,
            n_deny=b.train_counts.n_deny + b.val_counts.n_deny,
        )
        full_posterior = make_posterior(
            n_clean=full_counts.n_clean,
            n_block=full_counts.n_block,
            n_deny=full_counts.n_deny,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            weight_clean=WEIGHT_CLEAN,
            weight_block=WEIGHT_BLOCK_FOLLOWUP,
            weight_deny=WEIGHT_EXPLICIT_DENY,
        )

        # Train-only posterior — used solely for the calibration
        # check; not persisted.
        train_posterior = make_posterior(
            n_clean=b.train_counts.n_clean,
            n_block=b.train_counts.n_block,
            n_deny=b.train_counts.n_deny,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            weight_clean=WEIGHT_CLEAN,
            weight_block=WEIGHT_BLOCK_FOLLOWUP,
            weight_deny=WEIGHT_EXPLICIT_DENY,
        )
        train_posteriors[key] = train_posterior

        val_total = int(b.val_counts.n_total)
        val_clean = int(b.val_counts.n_clean)
        if val_total > 0:
            val_outcomes[key] = (val_clean, val_total)

        # Apply gates.
        if full_counts.n_total < min_samples_eff:
            dropped_samples += 1
            continue

        lcb = full_posterior.lower_credible_bound(credibility)
        if lcb < min_trust:
            dropped_trust += 1
            continue

        # Drift gate — Jensen-Shannon between baseline and recent
        # posteriors (unweighted; we want the shape of *change*).
        baseline_post = make_posterior(
            n_clean=b.baseline_counts.n_clean,
            n_block=b.baseline_counts.n_block,
            n_deny=b.baseline_counts.n_deny,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
        )
        recent_post = make_posterior(
            n_clean=b.recent_counts.n_clean,
            n_block=b.recent_counts.n_block,
            n_deny=b.recent_counts.n_deny,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
        )
        drift_score = (
            jensen_shannon_beta(baseline_post, recent_post)
            if (b.baseline_counts.n_total > 0
                and b.recent_counts.n_total > 0)
            else 0.0
        )
        drifted = (
            apply_drift
            and is_drifted(
                baseline=baseline_post,
                recent=recent_post,
                threshold=drift_threshold,
            )
        )
        if drifted:
            dropped_drift += 1
            continue

        # The v0.5.11 audit fields stay readable: clean_rate is
        # the *observed* clean fraction (no prior, no decay) and
        # trust_score is now the LCB.
        observed_clean = (
            (b.n_seen - b.n_followed_by_block - b.n_explicit_deny)
            / b.n_seen
            if b.n_seen > 0 else 0.0
        )
        # v0.5.23 — runtime-fingerprint centroid from clean records.
        from aegis.autonomy.centroid import compute_centroid_and_cov
        centroid, cov_diag = compute_centroid_and_cov(b.clean_features)

        out[key] = TrustedPattern(
            tool_name=b.tool,
            reason_signature=b.signature,
            n_seen=b.n_seen,
            n_followed_by_block=b.n_followed_by_block,
            n_explicit_deny=b.n_explicit_deny,
            clean_rate=observed_clean,
            trust_score=lcb,
            last_seen_ns=b.last_seen_ns,
            sample_trace_ids=tuple(b.sample_traces),
            alpha=full_posterior.alpha,
            beta=full_posterior.beta,
            posterior_mean=full_posterior.mean,
            posterior_std=full_posterior.std,
            n_effective=full_counts.n_total,
            drift_score=drift_score,
            drifted=drifted,
            credibility=credibility,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            atv_centroid=centroid,
            atv_cov_diag=cov_diag,
            centroid_n_samples=len(b.clean_features),
        )

    # Calibration check — uses train-only posteriors + val
    # outcomes. If ECE exceeds the threshold, the whole trust
    # table is marked failed; the CLI refuses to persist it.
    if apply_calibration:
        calibration = compute_calibration(
            predictions=train_posteriors,
            val_outcomes=val_outcomes,
            credibility=credibility,
            ece_threshold=ece_threshold,
        )
    else:
        calibration = CalibrationReport(
            n_train=sum(int(p.n_effective) for p in train_posteriors.values()),
            n_val=sum(v[1] for v in val_outcomes.values()),
            buckets=(),
            expected_calibration_error=0.0,
            passed=True,
            rejection_reason="(calibration check disabled)",
        )

    return LearnResult(
        trust_table=out,
        calibration=calibration,
        n_records_scanned=len(rec_list),
        n_patterns_considered=len(buckets),
        n_patterns_dropped_never_trust=dropped_never,
        n_patterns_dropped_low_samples=dropped_samples,
        n_patterns_dropped_low_trust=dropped_trust,
        n_patterns_dropped_drift=dropped_drift,
        min_samples_effective=min_samples_eff,
        half_life_days=half_life_days,
        learned_at_ns=anchor_ns,
        credibility=credibility,
        min_trust=min_trust,
    )


def _build_tool_baselines(
    buckets: dict[tuple[str, str], _PatternBucket],
    never_trust_keys: set[tuple[str, str]],
) -> dict[str, ToolBaseline]:
    """Aggregate per-tool RewardCounts across all non-never-trust
    patterns. Drives the empirical-Bayes prior."""
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    for key, b in buckets.items():
        if key in never_trust_keys:
            continue
        rec = agg[b.tool]
        rec[0] += b.train_counts.n_clean + b.val_counts.n_clean
        rec[1] += b.train_counts.n_block + b.val_counts.n_block
        rec[2] += b.train_counts.n_deny + b.val_counts.n_deny
        rec[3] = int(rec[3]) + 1
    return {
        tool: ToolBaseline(
            tool_name=tool,
            n_patterns=int(rec[3]),
            total_clean=rec[0],
            total_block=rec[1],
            total_deny=rec[2],
        )
        for tool, rec in agg.items()
    }


def learn_trusted_patterns(
    records: Iterable[ContextMemoryRecord],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_clean_rate: float = DEFAULT_MIN_CLEAN_RATE,
) -> dict[tuple[str, str], TrustedPattern]:
    """v0.5.11-compatible entry point. Delegates to
    :func:`learn_with_diagnostics` with min_trust derived from
    ``min_clean_rate`` (since the new decision rule is LCB ≥
    min_trust rather than clean_rate ≥ min_clean_rate).

    Callers that want the full Bayesian diagnostics — calibration,
    Bonferroni adjustment, drift score, drop counts — should use
    :func:`learn_with_diagnostics` directly.
    """
    # Backward compat: v0.5.11 callers admitted any pattern whose
    # raw clean_rate cleared min_clean_rate, regardless of sample
    # size. Map that to a permissive LCB threshold that still
    # admits 10-sample-clean patterns (LCB(0.95) ≈ 0.49 for
    # Beta(11, 5)) but rejects 5-sample-clean (LCB ≈ 0.30). The
    # stricter v0.5.12 policy is opt-in via learn_with_diagnostics.
    result = learn_with_diagnostics(
        records,
        min_samples=min_samples,
        min_trust=0.40,
        apply_bonferroni=False,
        apply_calibration=False,
        apply_decay=False,
        apply_drift=False,
    )
    # In v0.5.11 trust_score had the heuristic form
    #   clean_rate * (0.6 + 0.4 * min(1, n/20))
    # — overwrite the LCB-based trust_score with that heuristic
    # so legacy callers see the same numeric range. The persisted
    # posterior fields (alpha, beta, …) remain populated.
    out: dict[tuple[str, str], TrustedPattern] = {}
    for key, p in result.trust_table.items():
        sample_weight = min(1.0, p.n_seen / 20.0)
        v0511_trust = p.clean_rate * (0.6 + 0.4 * sample_weight)
        out[key] = TrustedPattern(
            tool_name=p.tool_name,
            reason_signature=p.reason_signature,
            n_seen=p.n_seen,
            n_followed_by_block=p.n_followed_by_block,
            clean_rate=p.clean_rate,
            trust_score=v0511_trust,
            last_seen_ns=p.last_seen_ns,
            sample_trace_ids=p.sample_trace_ids,
            alpha=p.alpha,
            beta=p.beta,
            posterior_mean=p.posterior_mean,
            posterior_std=p.posterior_std,
            n_effective=p.n_effective,
            n_explicit_deny=p.n_explicit_deny,
            drift_score=p.drift_score,
            drifted=p.drifted,
            credibility=p.credibility,
            prior_alpha=p.prior_alpha,
            prior_beta=p.prior_beta,
        )
    return out


# ──────────────────────────────────────────────────────────────────
# Runtime evaluator — single-decision autonomy verdict
# ──────────────────────────────────────────────────────────────────

MIN_TRUST_FOR_BYPASS: Final[float] = 0.85


def evaluate_autonomy_request(
    *,
    tool_name: str,
    reason: str,
    trust_table: dict[tuple[str, str], TrustedPattern],
    min_trust: float = MIN_TRUST_FOR_BYPASS,
    runtime_features: tuple[float, ...] | None = None,
    centroid_min_samples: int = 20,
) -> AutonomyVerdict:
    """Given a live REQUIRE_APPROVAL signal, decide whether to
    auto-bypass it based on the learned trust table.

    Returns an :class:`AutonomyVerdict`. ``auto_approve=True``
    means the human prompt should be skipped (firewall verdict
    downgraded from REQUIRE_APPROVAL to ALLOW). ``auto_approve=
    False`` means keep the human in the loop.

    Never returns ``auto_approve=True`` for never-trust reasons
    even when ``AEGIS_AUTONOMY_ENABLED=1`` — the constant filter
    is enforced here, not just at learning time, so an adversarial
    trust table can't sneak through.

    v0.5.12: the decision metric is the LCB on the Beta posterior
    (recomputed at runtime from the persisted shape parameters if
    they exist, otherwise derived from the v0.5.11 integer counts).
    Drifted patterns are refused regardless of LCB — the
    operator must re-learn before a drifted pattern is trusted
    again. ε-greedy forced exploration lives in
    :func:`aegis.autonomy.runtime.apply_autonomy_bypass` and is
    layered on *after* this function returns ``auto_approve=True``.
    """
    # Sentinel: never trust dangerous categories regardless of
    # what the trust table says.
    if _is_never_trust(reason or ""):
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason="never-trust category — human in the loop preserved",
            outlier_signals=("never_trust_filter",),
        )

    sig = reason_signature(reason or "")
    key = (tool_name, sig)
    pattern = trust_table.get(key)
    if pattern is None:
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason=(
                f"no trust entry for tool={tool_name} "
                f"signature={sig!r} — first occurrence or below "
                "burn-in threshold"
            ),
        )

    # Drift filter — recompute even if the on-disk pattern was
    # stamped clean, so a downstream re-evaluator can refuse a
    # pattern that newer evidence has invalidated.
    if pattern.drifted:
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=pattern,
            confidence=pattern.trust_score,
            reason=(
                f"pattern flagged as drifted (JS={pattern.drift_score:.3f}) "
                "— re-run `aegis autonomy learn`"
            ),
            outlier_signals=("drift_detected",),
        )

    # Trust score is the LCB for v0.5.12 patterns and the
    # heuristic score for v0.5.11 patterns. Respecting it
    # preserves on-disk trust tables across the upgrade — an
    # operator who ran `aegis autonomy learn` on v0.5.11 keeps
    # their bypass profile under v0.5.12.
    trust = pattern.trust_score
    if trust < min_trust:
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=pattern,
            confidence=trust,
            reason=(
                f"trust score {trust:.2f} below "
                f"min_trust {min_trust:.2f} — keeping human in loop"
            ),
            outlier_signals=("low_trust_score",),
        )

    # v0.5.23 — ATV centroid Mahalanobis gate. Fires only when:
    #   (a) the caller supplied a runtime feature vector,
    #   (b) the pattern has a populated centroid (centroid_n_samples
    #       ≥ centroid_min_samples — enough samples for a stable
    #       cluster), and
    #   (c) the runtime fingerprint sits outside the cluster.
    # Otherwise the centroid is silently skipped (legacy v0.5.22
    # patterns + first-time patterns with too-few clean samples
    # both fall through to bypass).
    if (
        runtime_features
        and pattern.atv_centroid
        and pattern.atv_cov_diag
        and pattern.centroid_n_samples >= centroid_min_samples
    ):
        from aegis.autonomy.centroid import (
            DEFAULT_MAHALANOBIS_THRESHOLD,
            mahalanobis_distance_diag,
        )
        dist = mahalanobis_distance_diag(
            runtime_features,
            pattern.atv_centroid,
            pattern.atv_cov_diag,
        )
        if dist > DEFAULT_MAHALANOBIS_THRESHOLD:
            return AutonomyVerdict(
                auto_approve=False,
                matched_pattern=pattern,
                confidence=trust,
                reason=(
                    f"runtime fingerprint outside cluster "
                    f"(Mahalanobis distance {dist:.2f} > "
                    f"{DEFAULT_MAHALANOBIS_THRESHOLD:.1f}σ) — "
                    "this call's cost/latency/tokens differ from "
                    "the cluster of clean historical calls; "
                    "keeping human in loop"
                ),
                outlier_signals=("centroid_outlier",),
            )

    return AutonomyVerdict(
        auto_approve=True,
        matched_pattern=pattern,
        confidence=trust,
        reason=(
            f"trusted pattern (seen {pattern.n_seen}× in burn-in, "
            f"clean rate {pattern.clean_rate:.0%}, "
            f"trust {trust:.2f})"
        ),
    )


def render_trust_table(
    table: dict[tuple[str, str], TrustedPattern],
) -> str:
    """Plain-text rendering for the `aegis autonomy show` CLI."""
    lines = [
        f"Autonomy trust table — {len(table)} pattern(s) learned",
        "",
    ]
    if not table:
        lines.append(
            "  (empty — run `aegis autonomy learn --since 30d`)"
        )
        return "\n".join(lines)

    # Sort by descending trust score so highest-confidence patterns
    # appear first.
    sorted_patterns = sorted(
        table.values(), key=lambda p: -p.trust_score,
    )
    lines.append(
        f"  {'tool':<14} {'signature':<24} {'seen':>5} "
        f"{'clean':>6} {'trust':>6}"
    )
    lines.append("  " + "-" * 64)
    for p in sorted_patterns:
        lines.append(
            f"  {p.tool_name:<14} {p.reason_signature:<24} "
            f"{p.n_seen:>5} {p.clean_rate:>6.0%} "
            f"{p.trust_score:>6.2f}"
        )
    return "\n".join(lines)


__all__ = [
    "AutonomyVerdict",
    "DEFAULT_MIN_CLEAN_RATE",
    "DEFAULT_MIN_SAMPLES",
    "LearnResult",
    "MIN_TRUST_FOR_BYPASS",
    "TrustedPattern",
    "autonomy_enabled",
    "evaluate_autonomy_request",
    "learn_trusted_patterns",
    "learn_with_diagnostics",
    "reason_signature",
    "render_trust_table",
]
