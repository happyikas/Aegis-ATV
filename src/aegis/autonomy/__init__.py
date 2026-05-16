"""Aegis autonomy — human-in-the-loop minimizer (v0.5.11).

The patent + product intent for autonomous coding agents is to
maximise throughput by removing every approval click that isn't
load-bearing. This package learns the operator's allow/deny
patterns during a burn-in window and uses them to auto-bypass
routine REQUIRE_APPROVAL events at runtime — while keeping every
bypassed event traceable in the ATV log + surfacing outliers via
`aegis doctor` and `aegis autonomy outliers`.

Public surface
==============

* :class:`TrustedPattern` — one learned pattern entry.
* :class:`AutonomyVerdict` — runtime decision for one approval.
* :class:`OutlierEvent` — auto-approval that turned out poorly.
* :func:`learn_trusted_patterns` — burn-in -> trust table.
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
* Outlier detection runs on each `aegis doctor` invocation when
  the autonomy bypass is engaged — false-positive trust patterns
  surface within a session, not weeks later.
"""

from aegis.autonomy.learner import (
    DEFAULT_MIN_CLEAN_RATE,
    DEFAULT_MIN_SAMPLES,
    MIN_TRUST_FOR_BYPASS,
    AutonomyVerdict,
    TrustedPattern,
    autonomy_enabled,
    evaluate_autonomy_request,
    learn_trusted_patterns,
    reason_signature,
    render_trust_table,
)
from aegis.autonomy.outliers import (
    AUTONOMY_BYPASS_PREFIX,
    OutlierEvent,
    detect_outliers,
    render_outliers,
)
from aegis.autonomy.runtime import (
    STEP_TRACE_KEY,
    STEP_TRACE_PREFIX,
    apply_autonomy_bypass,
    load_trust_table,
    save_trust_table,
    trust_table_metadata,
    trust_table_path,
)

__all__ = [
    "AUTONOMY_BYPASS_PREFIX",
    "AutonomyVerdict",
    "DEFAULT_MIN_CLEAN_RATE",
    "DEFAULT_MIN_SAMPLES",
    "MIN_TRUST_FOR_BYPASS",
    "OutlierEvent",
    "STEP_TRACE_KEY",
    "STEP_TRACE_PREFIX",
    "TrustedPattern",
    "apply_autonomy_bypass",
    "autonomy_enabled",
    "detect_outliers",
    "evaluate_autonomy_request",
    "learn_trusted_patterns",
    "load_trust_table",
    "reason_signature",
    "render_outliers",
    "render_trust_table",
    "save_trust_table",
    "trust_table_metadata",
    "trust_table_path",
]
