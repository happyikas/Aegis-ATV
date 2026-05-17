"""Aegis autonomy — human-in-the-loop minimizer (v0.5.12).

The patent + product intent for autonomous coding agents is to
maximise throughput by removing every approval click that isn't
load-bearing. This package learns the operator's allow/deny
patterns during a burn-in window and uses them to auto-bypass
routine REQUIRE_APPROVAL events at runtime — while keeping every
bypassed event traceable in the ATV log + surfacing outliers via
`aegis doctor` and `aegis autonomy outliers`.

v0.5.12 — Bayesian backbone
===========================

The trust formulation moved from a point-estimate ``clean_rate``
to a full Bayesian Beta posterior with reward shaping, decay,
drift detection, and calibration. See the individual modules:

* :mod:`aegis.autonomy.bayesian` — Beta(α, β) posterior + LCB
* :mod:`aegis.autonomy.reward`   — ternary reward shaping
* :mod:`aegis.autonomy.decay`    — exponential decay (30-day τ)
* :mod:`aegis.autonomy.drift`    — Jensen-Shannon drift score
* :mod:`aegis.autonomy.calibration` — train/val split + ECE
* :mod:`aegis.autonomy.learner`  — orchestrator + trust table
* :mod:`aegis.autonomy.runtime`  — Verdict shim + ε-greedy
* :mod:`aegis.autonomy.outliers` — postmortem walker

Public surface
==============

* :class:`TrustedPattern` — one learned pattern entry.
* :class:`AutonomyVerdict` — runtime decision for one approval.
* :class:`OutlierEvent` — auto-approval that turned out poorly.
* :class:`BetaPosterior` — Beta(α, β) over a pattern's true rate.
* :class:`LearnResult` — diagnostic-rich learn output.
* :class:`CalibrationReport` — ECE on train/val split.
* :func:`learn_trusted_patterns` — burn-in -> trust table.
* :func:`learn_with_diagnostics` — burn-in -> full diagnostics.
* :func:`evaluate_autonomy_request` — runtime check.
* :func:`detect_outliers` — postmortem walk.
* :func:`reason_signature` — canonicalise a firewall reason.
* :func:`autonomy_enabled` — runtime feature flag.

Safety contract
===============

* Never-trust filter — destructive / sensitive-path reasons are
  always kept in the human loop, regardless of burn-in count.
* Opt-in via ``AEGIS_AUTONOMY_ENABLED=1``; default off.
* Bypassed events stamp a marker in step_traces so audit replay
  can re-derive every approval skip.
* ε-greedy forced exploration (``AEGIS_AUTONOMY_EPSILON``, default
  0.05) keeps the operator in the loop for a small fraction of
  trusted patterns so drift and IPW coverage remain healthy.
* Calibration gate — a trust table whose ECE exceeds the
  threshold is *refused at learn time*. The previous snapshot
  stays authoritative.
* Drift gate — patterns whose recent third diverged from their
  baseline are dropped from the table.
"""

from aegis.autonomy.andon import (
    DEFAULT_ANDON_THRESHOLD,
    AndonState,
    andon_state_path,
    andon_threshold_from_env,
    record_andon,
    record_bypass,
    reset_counter,
    should_fire_andon,
)
from aegis.autonomy.andon import (
    load_state as load_andon_state,
)
from aegis.autonomy.bayesian import (
    DEFAULT_CREDIBILITY,
    DEFAULT_PRIOR_ALPHA,
    DEFAULT_PRIOR_BETA,
    BetaPosterior,
    ToolBaseline,
    TrustScoreBreakdown,
    adjusted_min_samples,
    empirical_bayes_prior,
    make_posterior,
    trust_breakdown,
)
from aegis.autonomy.calibration import (
    ECE_THRESHOLD,
    HOLDOUT_FRACTION,
    BucketStat,
    CalibrationReport,
    compute_calibration,
    expected_calibration_error,
    trace_split,
)
from aegis.autonomy.decay import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_MIN_WEIGHT,
    decay_weight,
    half_life_from_env,
    should_drop,
)
from aegis.autonomy.denials import (
    DenialRecord,
    append_denial,
    denials_path,
    load_denial_trace_ids,
    load_denials,
)
from aegis.autonomy.drift import (
    DEFAULT_DRIFT_THRESHOLD,
    is_drifted,
    jensen_shannon_beta,
    kl_divergence_beta,
)
from aegis.autonomy.learner import (
    DEFAULT_MIN_CLEAN_RATE,
    DEFAULT_MIN_SAMPLES,
    MIN_TRUST_FOR_BYPASS,
    AutonomyVerdict,
    LearnResult,
    TrustedPattern,
    autonomy_enabled,
    evaluate_autonomy_request,
    learn_trusted_patterns,
    learn_with_diagnostics,
    reason_signature,
    render_trust_table,
)
from aegis.autonomy.outliers import (
    AUTONOMY_BYPASS_PREFIX,
    OutlierEvent,
    detect_outliers,
    render_outliers,
)
from aegis.autonomy.reward import (
    WEIGHT_BLOCK_FOLLOWUP,
    WEIGHT_CLEAN,
    WEIGHT_EXPLICIT_DENY,
    RewardCounts,
    RewardEvent,
    RewardSignal,
    classify_record,
    weight_for,
)
from aegis.autonomy.runtime import (
    DEFAULT_EPSILON,
    STEP_TRACE_ANDON_KEY,
    STEP_TRACE_ANDON_PREFIX,
    STEP_TRACE_EXPLORE_KEY,
    STEP_TRACE_EXPLORE_PREFIX,
    STEP_TRACE_KEY,
    STEP_TRACE_PREFIX,
    apply_autonomy_bypass,
    load_trust_table,
    save_trust_table,
    trust_table_metadata,
    trust_table_path,
)
from aegis.autonomy.session_prior import (
    DEFAULT_TTL_HOURS,
    RISK_LABELS,
    SessionPrior,
    end_session,
    load_session_prior,
    session_min_trust,
    session_prior_path,
    start_session,
)

__all__ = [
    "AUTONOMY_BYPASS_PREFIX",
    "AndonState",
    "AutonomyVerdict",
    "BetaPosterior",
    "BucketStat",
    "CalibrationReport",
    "DEFAULT_ANDON_THRESHOLD",
    "DEFAULT_CREDIBILITY",
    "DEFAULT_DRIFT_THRESHOLD",
    "DEFAULT_EPSILON",
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_MIN_CLEAN_RATE",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_MIN_WEIGHT",
    "DEFAULT_PRIOR_ALPHA",
    "DEFAULT_PRIOR_BETA",
    "DEFAULT_TTL_HOURS",
    "DenialRecord",
    "ECE_THRESHOLD",
    "HOLDOUT_FRACTION",
    "LearnResult",
    "MIN_TRUST_FOR_BYPASS",
    "OutlierEvent",
    "RewardCounts",
    "RewardEvent",
    "RISK_LABELS",
    "RewardSignal",
    "STEP_TRACE_ANDON_KEY",
    "STEP_TRACE_ANDON_PREFIX",
    "STEP_TRACE_EXPLORE_KEY",
    "STEP_TRACE_EXPLORE_PREFIX",
    "STEP_TRACE_KEY",
    "STEP_TRACE_PREFIX",
    "SessionPrior",
    "ToolBaseline",
    "TrustScoreBreakdown",
    "TrustedPattern",
    "WEIGHT_BLOCK_FOLLOWUP",
    "WEIGHT_CLEAN",
    "WEIGHT_EXPLICIT_DENY",
    "adjusted_min_samples",
    "andon_state_path",
    "andon_threshold_from_env",
    "append_denial",
    "apply_autonomy_bypass",
    "autonomy_enabled",
    "classify_record",
    "compute_calibration",
    "decay_weight",
    "denials_path",
    "detect_outliers",
    "empirical_bayes_prior",
    "end_session",
    "evaluate_autonomy_request",
    "expected_calibration_error",
    "half_life_from_env",
    "is_drifted",
    "jensen_shannon_beta",
    "kl_divergence_beta",
    "learn_trusted_patterns",
    "learn_with_diagnostics",
    "load_andon_state",
    "load_denial_trace_ids",
    "load_denials",
    "load_session_prior",
    "load_trust_table",
    "make_posterior",
    "reason_signature",
    "record_andon",
    "record_bypass",
    "render_outliers",
    "render_trust_table",
    "reset_counter",
    "save_trust_table",
    "session_min_trust",
    "session_prior_path",
    "should_drop",
    "should_fire_andon",
    "start_session",
    "trace_split",
    "trust_breakdown",
    "trust_table_metadata",
    "trust_table_path",
    "weight_for",
]
