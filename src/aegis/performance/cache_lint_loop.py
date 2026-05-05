"""Closed-loop verification for the prompt-cache lint (PR #50).

The cache_lint module diagnoses cache breaks and static-prompt
anti-patterns in a transcript. This module closes the feedback loop:

1. **Projection** — given a report, project what the metrics
   *would* be if every recommendation were applied. Conservative:
   uses the report's own ``theoretical_max_cache_hit_rate`` and
   ``potential_token_savings`` rather than inventing new numbers.

2. **Before / after comparison** — given two reports (a baseline
   transcript and a follow-up), compute:

   * **realisation rate** — observed savings ÷ projected savings,
     i.e. how much of the predicted improvement actually materialised
   * **breaks resolved** — patterns flagged in the baseline that no
     longer appear in the follow-up
   * **breaks persisting** — flagged in baseline AND follow-up
     (user didn't apply the recommendation, or applied it to the
     wrong place)
   * **new breaks** — appeared only in the follow-up (regression)

The comparison is the verification half of the closed loop: the
user runs cache_lint on their session, applies recommendations to
their CLAUDE.md / system prompt, runs another session, and points
this comparator at the two transcripts to confirm the fix landed.

Privacy posture
---------------
Inherits cache_lint's posture — pure metadata, no raw prompt body
or transcript content surfaced. Break attribution strings are
already truncated and PII-free.

Patent linkage
--------------
Closed-loop attestation (Claim 34) — the runtime's measured
post-fix metrics become inputs to the next ATV's cost band, so
the M13 attribution head can re-attribute future advisor outputs
against measured (rather than self-reported) reality.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aegis.performance.cache_lint import (
    CacheBreak,
    CacheLintReport,
    StaticLintFinding,
    analyze_transcript,
)

# ──────────────────────────────────────────────────────────────────────
# Projection — "what would the metrics be if I applied the recommendations?"
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ProjectedFix:
    """Counterfactual projection of a CacheLintReport's recommendations.

    Conservative: every value here is sourced from existing fields
    on the input report rather than invented. The advisor's own
    ``theoretical_max_cache_hit_rate`` is the projection target;
    we simply re-label it and tally how many distinct fixes need
    to be applied to reach it.
    """

    projected_cache_hit_rate: float
    projected_token_savings: int
    breaks_to_address: int
    static_findings_to_address: int
    error_severity_findings: int
    warning_severity_findings: int
    notes: list[str] = field(default_factory=list)


def project_fix(report: CacheLintReport) -> ProjectedFix:
    """Build a :class:`ProjectedFix` from a CacheLintReport.

    The projection rests on the report's own counterfactual:
    if every flagged break were eliminated, the cache_hit_rate
    would equal ``theoretical_max_cache_hit_rate`` and recover
    ``potential_token_savings`` per session. Static findings
    contribute *qualitatively* — each one is one user action
    (move below cache_control, replace with a stable value, etc.).
    """
    n_err = sum(1 for f in report.static_findings if f.severity == "error")
    n_warn = sum(1 for f in report.static_findings if f.severity == "warning")

    notes: list[str] = []
    if report.breaks:
        notes.append(
            f"{len(report.breaks)} break(s) → recover ~"
            f"{report.potential_token_savings:,} tokens / session"
        )
    if n_err:
        notes.append(
            f"{n_err} ERROR-severity static finding(s) — fix first; "
            "they cap the achievable hit rate"
        )
    if n_warn:
        notes.append(
            f"{n_warn} WARNING-severity static finding(s) — schedule "
            "for the next prompt-template revision"
        )
    if not notes:
        notes.append("No actionable recommendations — already optimal")

    return ProjectedFix(
        projected_cache_hit_rate=report.theoretical_max_cache_hit_rate,
        projected_token_savings=report.potential_token_savings,
        breaks_to_address=len(report.breaks),
        static_findings_to_address=len(report.static_findings),
        error_severity_findings=n_err,
        warning_severity_findings=n_warn,
        notes=notes,
    )


# ──────────────────────────────────────────────────────────────────────
# Before / after comparison
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ComparisonReport:
    """Side-by-side comparison of a baseline + follow-up cache_lint."""

    before: CacheLintReport
    after: CacheLintReport
    projected: ProjectedFix

    # Hit-rate deltas
    cache_hit_rate_delta: float                # after − before (signed)
    token_savings_realised: int                # actual recovery
    realisation_rate: float                    # realised / projected ∈ [0, ?]

    # Break-level breakdown
    breaks_resolved: list[CacheBreak] = field(default_factory=list)
    breaks_persisting: list[CacheBreak] = field(default_factory=list)
    new_breaks: list[CacheBreak] = field(default_factory=list)

    # Static-lint breakdown
    static_findings_resolved: list[StaticLintFinding] = field(default_factory=list)
    static_findings_persisting: list[StaticLintFinding] = field(default_factory=list)
    new_static_findings: list[StaticLintFinding] = field(default_factory=list)


def _break_signature(b: CacheBreak) -> tuple[int, str]:
    """Stable identity for a break — (turn_idx, attribution-prefix).

    Used to decide whether the same break shows up before AND after.
    Two breaks at the same turn with the same attribution prefix
    (first 40 chars) count as the same.
    """
    return (b.turn_idx, b.attribution[:40])


def _finding_signature(f: StaticLintFinding) -> tuple[str, str]:
    """Stable identity for a static finding — (pattern_name, excerpt).

    Char offsets aren't stable across edits (the user might add /
    remove text above the finding). Pattern + excerpt is robust.
    """
    return (f.pattern_name, f.matched_excerpt)


def compare_reports(
    *, before: CacheLintReport, after: CacheLintReport,
) -> ComparisonReport:
    """Diff two cache_lint reports and assess fix realisation."""
    projected = project_fix(before)

    # Hit-rate delta + realisation.
    delta = after.observed_cache_hit_rate - before.observed_cache_hit_rate
    # Token savings realised: difference in "tokens lost to breaks"
    # between before and after. Negative means more tokens are lost
    # now than before (regression).
    tokens_lost_before = sum(b.tokens_lost_estimate for b in before.breaks)
    tokens_lost_after = sum(b.tokens_lost_estimate for b in after.breaks)
    realised = tokens_lost_before - tokens_lost_after

    if projected.projected_token_savings > 0:
        realisation_rate = realised / projected.projected_token_savings
    else:
        realisation_rate = 1.0 if realised >= 0 else 0.0

    # Break-level diff.
    before_sigs = {_break_signature(b): b for b in before.breaks}
    after_sigs = {_break_signature(b): b for b in after.breaks}
    resolved = [b for sig, b in before_sigs.items() if sig not in after_sigs]
    persisting = [b for sig, b in before_sigs.items() if sig in after_sigs]
    new = [b for sig, b in after_sigs.items() if sig not in before_sigs]

    # Static-finding diff.
    before_finding_sigs = {
        _finding_signature(f): f for f in before.static_findings
    }
    after_finding_sigs = {
        _finding_signature(f): f for f in after.static_findings
    }
    static_resolved = [
        f for sig, f in before_finding_sigs.items() if sig not in after_finding_sigs
    ]
    static_persisting = [
        f for sig, f in before_finding_sigs.items() if sig in after_finding_sigs
    ]
    static_new = [
        f for sig, f in after_finding_sigs.items() if sig not in before_finding_sigs
    ]

    return ComparisonReport(
        before=before,
        after=after,
        projected=projected,
        cache_hit_rate_delta=delta,
        token_savings_realised=realised,
        realisation_rate=realisation_rate,
        breaks_resolved=resolved,
        breaks_persisting=persisting,
        new_breaks=new,
        static_findings_resolved=static_resolved,
        static_findings_persisting=static_persisting,
        new_static_findings=static_new,
    )


def compare_transcripts(
    *,
    before_path: Path,
    after_path: Path,
    before_system_prompt: str | None = None,
    after_system_prompt: str | None = None,
    break_threshold_pp: float = 30.0,
) -> ComparisonReport:
    """Convenience: run cache_lint on both transcripts and diff."""
    before_report = analyze_transcript(
        before_path,
        break_threshold_pp=break_threshold_pp,
        system_prompt=before_system_prompt,
    )
    after_report = analyze_transcript(
        after_path,
        break_threshold_pp=break_threshold_pp,
        system_prompt=after_system_prompt,
    )
    return compare_reports(before=before_report, after=after_report)


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


def projected_fix_to_dict(p: ProjectedFix) -> dict[str, Any]:
    return asdict(p)


def comparison_to_dict(c: ComparisonReport) -> dict[str, Any]:
    """Flat dict for `aegis cache-lint --compare-with ... --json`.

    The full inner reports are only summarised (n_turns, hit rate,
    n_breaks) — keeping the comparison output focused on the diff,
    not on duplicating both reports verbatim.
    """
    return {
        "cache_hit_rate_delta": c.cache_hit_rate_delta,
        "token_savings_realised": c.token_savings_realised,
        "realisation_rate": c.realisation_rate,
        "projected": asdict(c.projected),
        "before": {
            "n_turns": c.before.n_turns,
            "observed_cache_hit_rate": c.before.observed_cache_hit_rate,
            "n_breaks": len(c.before.breaks),
            "n_static_findings": len(c.before.static_findings),
        },
        "after": {
            "n_turns": c.after.n_turns,
            "observed_cache_hit_rate": c.after.observed_cache_hit_rate,
            "n_breaks": len(c.after.breaks),
            "n_static_findings": len(c.after.static_findings),
        },
        "breaks_resolved": [asdict(b) for b in c.breaks_resolved],
        "breaks_persisting": [asdict(b) for b in c.breaks_persisting],
        "new_breaks": [asdict(b) for b in c.new_breaks],
        "static_findings_resolved": [
            asdict(f) for f in c.static_findings_resolved
        ],
        "static_findings_persisting": [
            asdict(f) for f in c.static_findings_persisting
        ],
        "new_static_findings": [
            asdict(f) for f in c.new_static_findings
        ],
    }
