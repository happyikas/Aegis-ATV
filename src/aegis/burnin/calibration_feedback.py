"""Calibration feedback loop — close the predicted-vs-actual loop by
turning accumulated retrospective records into recommended threshold
adjustments (Phase D, v2.7.2).

Background
----------

* PR-ψ-calibration (#69) added burn-in derived percentile thresholds
  for the M13-confidence and session-drift gate signals.
* PR-ψ-retrospective (#70) added PostToolUse comparison of predicted
  advice vs actual tool outcome (`accurate` / `missed_signal` /
  `false_alarm` / `not_applicable`).

Phase D consumes (1) using the data captured in (2): walk the audit
chain, correlate gate-trigger signals with retrospective accuracy,
and recommend updated thresholds. The simple approach used here:

  1. Extract per-signal accuracy from existing audit.
  2. Re-extract the M13 + drift percentile distributions from the
     same audit (via the existing extractor in
     :mod:`aegis.burnin.advisor_calibration`). This naturally adapts
     to the user's traffic — the more data they have, the better the
     thresholds match their distribution.

The adjustment logic stops there for v2.7.2. Future phases may also
shift *which* percentile we treat as the trigger (e.g. p10 → p5 when
false_alarms dominate), but that requires the gate code to be aware
of the percentile choice — defer to v2.8.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis.burnin.advisor_calibration import (
    AdvisorCalibration,
    extract_calibration_from_audit,
    load_calibration_or_default,
)

# Trigger-reason prefixes / fragments mapped to a stable signal name.
# Order matters — the first prefix that matches wins.
_SIGNAL_TAGS: list[tuple[str, str]] = [
    ("verdict=", "verdict_non_allow"),
    ("cost-divergence", "m12_cost_divergence"),
    ("loop/redundancy", "step336_loop"),
    ("cost approaching", "step335_budget"),
    ("HW anomaly", "step337_hw_anomaly"),
    ("M13 score", "m13_low_calibrated"),
    ("session drift", "drift_high_calibrated"),
    ("AEGIS_ADVISOR_ALWAYS", "always_bypass"),
]


def _classify_signal(reason: str) -> str:
    for prefix, name in _SIGNAL_TAGS:
        if prefix in reason:
            return name
    return "unknown"


# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PerSignalAccuracy:
    """Accuracy of one gate-trigger signal across the audit."""

    signal: str                  # tag from :data:`_SIGNAL_TAGS`
    n_fired: int = 0
    n_accurate: int = 0
    n_false_alarm: int = 0
    n_missed_signal: int = 0     # post failures where this signal fired
    n_not_applicable: int = 0    # gate fired but no retrospective match

    @property
    def precision(self) -> float:
        """``n_accurate / (n_accurate + n_false_alarm)``. ``0.0`` when
        the denominator is zero (no retrospective evidence yet)."""
        denom = self.n_accurate + self.n_false_alarm
        return self.n_accurate / denom if denom else 0.0


@dataclass(frozen=True)
class CalibrationFeedbackReport:
    """Aggregate of one feedback-analysis pass."""

    audit_path: str
    n_pre: int
    n_post: int
    n_with_retrospective: int

    overall_accuracy: dict[str, int] = field(default_factory=dict)
    per_signal: tuple[PerSignalAccuracy, ...] = field(default_factory=tuple)

    current_calibration: AdvisorCalibration | None = None
    recommended_calibration: AdvisorCalibration | None = None
    calibration_changed: bool = False

    notes: tuple[str, ...] = field(default_factory=tuple)


# ──────────────────────────────────────────────────────────────────────
# Audit walker
# ──────────────────────────────────────────────────────────────────────


def _stream_records(audit_path: Path) -> list[dict[str, Any]]:
    """Read all audit records once. The file is bounded, so this is
    fine for the analysis path (off the hot path)."""
    if not audit_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with audit_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _index_pre_records(
    pre_records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build invocation_id → PreToolUse record map for fast match in
    `analyse_audit`. Last-write-wins on collisions (rare in practice
    since invocation_id is unique per tool call)."""
    out: dict[str, dict[str, Any]] = {}
    for rec in pre_records:
        inv = rec.get("invocation_id")
        if isinstance(inv, str) and inv:
            out[inv] = rec
    return out


# ──────────────────────────────────────────────────────────────────────
# Core analysis
# ──────────────────────────────────────────────────────────────────────


def analyse_audit(audit_path: Path) -> CalibrationFeedbackReport:
    """Walk an ``audit.jsonl`` and produce a feedback report.

    Steps:

    1. Split records into PreToolUse vs PostToolUse.
    2. For each PostToolUse with a ``retrospective_advice`` block,
       find the matching PreToolUse record by ``invocation_id`` and
       map its ``advisor_gate.reason`` to a signal tag.
    3. Bucket per-signal counts (n_fired / n_accurate / n_false_alarm /
       n_missed_signal / n_not_applicable).
    4. Re-extract M13 + drift percentiles from the same audit. If the
       result differs from the currently-loaded calibration, mark
       ``calibration_changed=True`` so the operator (or CLI) can apply.

    Returns a fully-populated report even when the audit is empty —
    inspect ``n_pre`` / ``n_post`` / ``recommended_calibration`` to
    decide whether to act.
    """
    records = _stream_records(audit_path)
    pre_records = [r for r in records if r.get("hook") != "PostToolUse"]
    post_records = [r for r in records if r.get("hook") == "PostToolUse"]

    pre_by_invocation = _index_pre_records(pre_records)

    overall = Counter[str]()
    fired = Counter[str]()
    accurate = Counter[str]()
    false_alarm = Counter[str]()
    missed = Counter[str]()
    not_applicable = Counter[str]()
    n_with_retrospective = 0

    for post in post_records:
        explain = post.get("explain") or {}
        retro = explain.get("retrospective_advice")
        if not isinstance(retro, dict):
            continue
        n_with_retrospective += 1
        accuracy = str(retro.get("accuracy", "not_applicable"))
        overall[accuracy] += 1

        # Find matching PreToolUse record + its gate-trigger reason.
        inv = post.get("invocation_id")
        pre = pre_by_invocation.get(inv) if isinstance(inv, str) else None
        gate_reason = ""
        if pre is not None:
            pre_explain = pre.get("explain") or {}
            gate = pre_explain.get("advisor_gate") or {}
            if isinstance(gate, dict) and gate.get("invoked"):
                gate_reason = str(gate.get("reason", ""))

        signal = _classify_signal(gate_reason) if gate_reason else "gate_skipped"
        if signal == "gate_skipped":
            # Gate didn't fire. If the tool failed, that's a missed
            # opportunity for whatever signal would have caught it —
            # bucket under a synthetic "any" tag.
            if accuracy == "missed_signal":
                missed["any_when_skipped"] += 1
            continue

        fired[signal] += 1
        if accuracy == "accurate":
            accurate[signal] += 1
        elif accuracy == "false_alarm":
            false_alarm[signal] += 1
        elif accuracy == "missed_signal":
            missed[signal] += 1
        elif accuracy == "not_applicable":
            not_applicable[signal] += 1

    per_signal: list[PerSignalAccuracy] = []
    all_signals = set(fired) | set(accurate) | set(false_alarm) | set(missed) | set(not_applicable)
    for sig in sorted(all_signals):
        per_signal.append(PerSignalAccuracy(
            signal=sig,
            n_fired=fired.get(sig, 0),
            n_accurate=accurate.get(sig, 0),
            n_false_alarm=false_alarm.get(sig, 0),
            n_missed_signal=missed.get(sig, 0),
            n_not_applicable=not_applicable.get(sig, 0),
        ))

    # Re-extract calibration from the same audit. Returns None if the
    # audit doesn't yet have enough samples — in that case we keep
    # the default and don't recommend a change.
    current_cal = load_calibration_or_default()
    recommended_cal = extract_calibration_from_audit(audit_path)

    calibration_changed = False
    if recommended_cal is not None and recommended_cal.is_usable():
        # Considered "changed" when any percentile moves more than 1
        # unit in the last decimal — avoids spurious diffs from
        # floating-point drift.
        def _drifted(a: float, b: float) -> bool:
            return abs(a - b) > 0.005

        calibration_changed = any(
            _drifted(getattr(current_cal, attr), getattr(recommended_cal, attr))
            for attr in (
                "m13_score_p10", "m13_score_p25", "m13_score_p50",
                "topic_drift_p50", "topic_drift_p75",
                "topic_drift_p90", "topic_drift_p95",
            )
        )
    else:
        recommended_cal = None

    notes: list[str] = []
    if not pre_records:
        notes.append("audit is empty — no records to analyse")
    elif not post_records:
        notes.append(
            "no PostToolUse records — install the post-tool hook to "
            "enable retrospective analysis"
        )
    elif n_with_retrospective == 0:
        notes.append(
            "PostToolUse records found but none carry retrospective "
            "advice — ensure the hook is up-to-date (PR #70+)"
        )
    if recommended_cal is None:
        notes.append(
            "below MIN_SAMPLES_FOR_CALIBRATION on at least one metric "
            "— keeping the current calibration"
        )

    return CalibrationFeedbackReport(
        audit_path=str(audit_path),
        n_pre=len(pre_records),
        n_post=len(post_records),
        n_with_retrospective=n_with_retrospective,
        overall_accuracy=dict(overall),
        per_signal=tuple(per_signal),
        current_calibration=current_cal,
        recommended_calibration=recommended_cal,
        calibration_changed=calibration_changed,
        notes=tuple(notes),
    )


# ──────────────────────────────────────────────────────────────────────
# Application
# ──────────────────────────────────────────────────────────────────────


def apply_recommended_calibration(
    report: CalibrationFeedbackReport,
    *,
    output_path: Path | None = None,
) -> Path | None:
    """Persist the report's ``recommended_calibration`` to disk.

    Writes to ``output_path`` if given, else the default location
    (``models/advisor_calibration_v1.json`` — same as
    :func:`aegis.burnin.advisor_calibration.default_calibration_path`).
    Returns the resolved path, or ``None`` when the report doesn't
    carry a recommendation (e.g. insufficient samples)."""
    if report.recommended_calibration is None:
        return None
    from aegis.burnin.advisor_calibration import (
        default_calibration_path,
        save_calibration,
    )

    target = output_path or default_calibration_path()
    save_calibration(report.recommended_calibration, target)
    return target


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


def render_feedback_report(r: CalibrationFeedbackReport) -> str:
    """Operator-readable summary."""
    lines: list[str] = [
        "AdvisorCalibration Feedback Report",
        f"  audit:                 {r.audit_path}",
        f"  PreToolUse records:    {r.n_pre}",
        f"  PostToolUse records:   {r.n_post}",
        f"  with retrospective:    {r.n_with_retrospective}",
        "",
        "Overall accuracy:",
    ]
    if r.overall_accuracy:
        for k in ("accurate", "missed_signal", "false_alarm", "not_applicable"):
            v = r.overall_accuracy.get(k, 0)
            lines.append(f"  {k:<18} {v}")
    else:
        lines.append("  (none)")

    lines += ["", "Per-signal accuracy:"]
    if r.per_signal:
        lines.append(
            "  signal                   fired  acc  fa  missed  na  precision"
        )
        for s in r.per_signal:
            lines.append(
                f"  {s.signal:<24} "
                f"{s.n_fired:>5}  {s.n_accurate:>3}  {s.n_false_alarm:>2}  "
                f"{s.n_missed_signal:>6}  {s.n_not_applicable:>2}  "
                f"{s.precision:.2f}"
            )
    else:
        lines.append("  (no fired signals yet)")

    cur = r.current_calibration
    rec = r.recommended_calibration
    lines += ["", "Calibration status:"]
    if cur is not None:
        lines.append(
            f"  current:     m13_p10={cur.m13_score_p10:.3f}  "
            f"drift_p95={cur.topic_drift_p95:.3f}  "
            f"(extracted_from={cur.extracted_from})"
        )
    if rec is not None:
        marker = "  ← CHANGED" if r.calibration_changed else "  (unchanged)"
        lines.append(
            f"  recommended: m13_p10={rec.m13_score_p10:.3f}  "
            f"drift_p95={rec.topic_drift_p95:.3f}  "
            f"(n_sessions={rec.n_sessions}){marker}"
        )
    elif r.calibration_changed:
        lines.append("  (no recommendation — see notes)")

    if r.notes:
        lines += ["", "Notes:"]
        for n in r.notes:
            lines.append(f"  - {n}")

    return "\n".join(lines)


__all__ = [
    "CalibrationFeedbackReport",
    "PerSignalAccuracy",
    "analyse_audit",
    "apply_recommended_calibration",
    "render_feedback_report",
]
