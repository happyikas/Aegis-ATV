"""v0.5.27 — ``aegis autonomy explain <trace_id>``.

With six independent safety floors (never-trust + reversibility +
drift + ATV centroid + andon + session-prior + ε-greedy)
shipped across v0.5.11–0.5.25, the operator's natural next
question is: **"why did this specific call get blocked /
bypassed?"** Walking the runtime logic manually requires reading
across five modules.

This module composes a single forensic report by replaying the
gates against current state for one historical record.

Two views per report:

1. **Original audit** — what the on-disk record's ``step_traces``
   tell us actually happened at the time of the call.
2. **Current simulation** — what the same call would resolve to
   if replayed now (trust table / centroid / andon counter /
   session-prior may have changed).

The simulation is read-only — no counters incremented, no state
mutated. The operator can re-run :func:`explain_trace` repeatedly
without side-effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aegis.autonomy.andon import andon_threshold_from_env
from aegis.autonomy.andon import load_state as load_andon
from aegis.autonomy.centroid import (
    DEFAULT_MAHALANOBIS_THRESHOLD,
    feature_vector,
    mahalanobis_distance_diag,
)
from aegis.autonomy.learner import (
    MIN_TRUST_FOR_BYPASS,
    _is_never_trust,
    autonomy_enabled,
    reason_signature,
)
from aegis.autonomy.runtime import (
    DEFAULT_EPSILON,
    _epsilon_from_env,
    _should_explore,
    load_trust_table,
)
from aegis.autonomy.session_prior import (
    session_min_trust,
)
from aegis.context_memory import context_memory_path, read_window
from aegis.context_memory.record import ContextMemoryRecord
from aegis.policies.reversibility import classify_reversibility

# ──────────────────────────────────────────────────────────────────
# Report shapes
# ──────────────────────────────────────────────────────────────────


_PASS_GLYPH = "🟢"
_FAIL_GLYPH = "🔴"
_SKIP_GLYPH = "⏭️ "
_INFO_GLYPH = "ℹ️ "


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict against the replayed call."""

    name: str
    """Short gate label — e.g. ``reversibility``, ``trust_score``."""

    status: str
    """``PASS`` / ``FAIL`` / ``SKIP`` / ``INFO``. PASS means this
    gate did not refuse the bypass. FAIL means this gate alone
    would refuse. SKIP means the gate did not apply (e.g.
    centroid skipped on under-sampled patterns). INFO is
    informational (e.g. the chosen ``min_trust`` threshold)."""

    detail: str
    """One-line human-readable explanation."""

    glyph: str = ""
    """Auto-assigned at construction."""

    def __post_init__(self) -> None:
        if self.glyph:
            return
        g = {
            "PASS": _PASS_GLYPH,
            "FAIL": _FAIL_GLYPH,
            "SKIP": _SKIP_GLYPH,
            "INFO": _INFO_GLYPH,
        }.get(self.status, "?")
        object.__setattr__(self, "glyph", g)


@dataclass(frozen=True)
class ExplainReport:
    """Composite forensic output for one trace_id."""

    trace_id: str
    found: bool
    record: ContextMemoryRecord | None = None

    # ── Original (from step_traces stamps) ────────────────────
    original_decision: str = ""
    original_stamps: dict[str, str] = field(default_factory=dict)

    # ── Simulated (gate-by-gate against current state) ────────
    gates: tuple[GateResult, ...] = field(default_factory=tuple)
    final_simulated_outcome: str = ""
    """One of: ``would-bypass``, ``would-refuse``, ``not-eligible``,
    ``master-off``."""


# ──────────────────────────────────────────────────────────────────
# Record lookup
# ──────────────────────────────────────────────────────────────────


def _find_record(
    trace_id: str,
    *,
    cm_path: Path | None = None,
    since_seconds: int = 30 * 86400,
) -> ContextMemoryRecord | None:
    """Locate a ContextMemoryRecord by trace_id. Scans the last
    30 days by default; the operator can widen via the CLI.

    Linear scan because ContextMemory is JSONL (no index). For
    forensic explain on a known trace_id, this is acceptable —
    typically <100ms on a 100k-record store."""
    import time
    now_ns = time.time_ns()
    since_ns = now_ns - since_seconds * 1_000_000_000
    target = cm_path if cm_path is not None else context_memory_path()
    if not target.exists():
        return None
    records = read_window(since_ns=since_ns, path=target)
    for r in records:
        if r.trace_id == trace_id:
            return r
    return None


# ──────────────────────────────────────────────────────────────────
# Gate walker
# ──────────────────────────────────────────────────────────────────


_AUTONOMY_STAMP_KEYS = (
    "aegis.autonomy.step331.run",
    "aegis.autonomy.step331.explore",
    "aegis.autonomy.step331.andon",
    "aegis.autonomy.step331.session_prior",
    "aegis.autonomy.user_deny",
)


def _extract_original_stamps(record: ContextMemoryRecord) -> dict[str, str]:
    """Pull the autonomy stamps the runtime wrote into step_traces
    at the time of the call."""
    traces = record.step_traces or {}
    return {
        k: traces[k] for k in _AUTONOMY_STAMP_KEYS if k in traces
    }


def _walk_gates(record: ContextMemoryRecord) -> tuple[list[GateResult], str]:
    """Re-run the autonomy gates against current state. Returns
    ``(gates, outcome)``. Each gate result is appended in execution
    order. The outcome is the first FAIL's gate (sticky) or
    ``would-bypass`` if all PASS."""
    gates: list[GateResult] = []

    # 1. Master switch.
    enabled = autonomy_enabled()
    if not enabled:
        gates.append(GateResult(
            name="master-switch",
            status="FAIL",
            detail=(
                "AEGIS_AUTONOMY_ENABLED is off — autonomy bypass "
                "is a no-op"
            ),
        ))
        return gates, "master-off"
    gates.append(GateResult(
        name="master-switch",
        status="PASS",
        detail="AEGIS_AUTONOMY_ENABLED=1",
    ))

    # 2. Verdict candidate (only REQUIRE_APPROVAL is eligible).
    if record.decision != "REQUIRE_APPROVAL":
        gates.append(GateResult(
            name="verdict-eligible",
            status="SKIP",
            detail=(
                f"decision={record.decision!r} — bypass only applies "
                "to REQUIRE_APPROVAL"
            ),
        ))
        return gates, "not-eligible"
    gates.append(GateResult(
        name="verdict-eligible",
        status="PASS",
        detail="decision=REQUIRE_APPROVAL — eligible for bypass",
    ))

    # 3. Reversibility (irreversible → never bypass).
    # We don't have the original tool_args_json in ContextMemory,
    # so the check uses only the tool name. The operator can pass
    # --args to override; the CLI handles that.
    revcls = classify_reversibility(record.tool_name, "")
    if revcls.level == "irreversible":
        gates.append(GateResult(
            name="reversibility",
            status="FAIL",
            detail=(
                f"level={revcls.level} (matched: {revcls.why}). "
                "Irreversible actions never auto-bypass."
            ),
        ))
        return gates, "would-refuse"
    gates.append(GateResult(
        name="reversibility",
        status="PASS",
        detail=f"level={revcls.level}",
    ))

    # 3.5. Session-prior label (informational; surfaced early so
    # the operator always sees the active label even when later
    # gates fail).
    effective_min, prior = session_min_trust(MIN_TRUST_FOR_BYPASS)
    if not prior.is_default():
        gates.append(GateResult(
            name="session-prior",
            status="INFO",
            detail=(
                f"label={prior.label!r}; min_trust scaled to "
                f"{effective_min:.2f}"
            ),
        ))
    else:
        gates.append(GateResult(
            name="session-prior",
            status="INFO",
            detail=(
                f"no session-prior set; min_trust={effective_min:.2f}"
            ),
        ))

    # 4. Never-trust substring filter.
    if _is_never_trust(record.reason or ""):
        gates.append(GateResult(
            name="never-trust-filter",
            status="FAIL",
            detail=(
                f"reason matches never-trust substring "
                f"({record.reason!r}); always kept in human loop"
            ),
        ))
        return gates, "would-refuse"
    gates.append(GateResult(
        name="never-trust-filter",
        status="PASS",
        detail="reason doesn't match never-trust list",
    ))

    # 5. Pattern lookup in current trust table.
    table = load_trust_table()
    sig = reason_signature(record.reason or "")
    key = (record.tool_name, sig)
    pattern = table.get(key)
    if pattern is None:
        gates.append(GateResult(
            name="pattern-lookup",
            status="FAIL",
            detail=(
                f"no trust entry for ({record.tool_name!r}, {sig!r}). "
                "Run `aegis autonomy learn` after enough observations."
            ),
        ))
        return gates, "would-refuse"
    gates.append(GateResult(
        name="pattern-lookup",
        status="PASS",
        detail=(
            f"matched ({record.tool_name}, {sig}); "
            f"n_seen={pattern.n_seen} trust={pattern.trust_score:.2f}"
        ),
    ))

    # 6. Drift flag (refused regardless of trust).
    if pattern.drifted:
        gates.append(GateResult(
            name="drift-flag",
            status="FAIL",
            detail=(
                f"pattern flagged drifted (JS={pattern.drift_score:.3f})"
            ),
        ))
        return gates, "would-refuse"
    gates.append(GateResult(
        name="drift-flag",
        status="PASS",
        detail=f"not drifted (JS={pattern.drift_score:.3f})",
    ))

    # 7. Trust score vs effective min_trust (session-prior may
    # have scaled it — surfaced earlier as an INFO gate).
    if pattern.trust_score < effective_min:
        gates.append(GateResult(
            name="trust-score",
            status="FAIL",
            detail=(
                f"trust={pattern.trust_score:.2f} < "
                f"min_trust={effective_min:.2f}"
            ),
        ))
        return gates, "would-refuse"
    gates.append(GateResult(
        name="trust-score",
        status="PASS",
        detail=(
            f"trust={pattern.trust_score:.2f} ≥ "
            f"min_trust={effective_min:.2f}"
        ),
    ))

    # 8. Centroid Mahalanobis check (skipped on under-sampled).
    if (
        pattern.atv_centroid
        and pattern.atv_cov_diag
        and pattern.centroid_n_samples >= 20
    ):
        fv = feature_vector(record)
        dist = mahalanobis_distance_diag(
            fv, pattern.atv_centroid, pattern.atv_cov_diag,
        )
        if dist > DEFAULT_MAHALANOBIS_THRESHOLD:
            gates.append(GateResult(
                name="centroid",
                status="FAIL",
                detail=(
                    f"Mahalanobis distance {dist:.2f} > "
                    f"{DEFAULT_MAHALANOBIS_THRESHOLD:.1f}σ — "
                    "runtime fingerprint outside cluster"
                ),
            ))
            return gates, "would-refuse"
        gates.append(GateResult(
            name="centroid",
            status="PASS",
            detail=(
                f"Mahalanobis distance {dist:.2f} ≤ "
                f"{DEFAULT_MAHALANOBIS_THRESHOLD:.1f}σ — "
                f"in cluster (n_samples={pattern.centroid_n_samples})"
            ),
        ))
    else:
        gates.append(GateResult(
            name="centroid",
            status="SKIP",
            detail=(
                f"centroid_n_samples={pattern.centroid_n_samples} < 20 "
                "(not enough clean samples for Mahalanobis gate)"
            ),
        ))

    # 9. Andon tripwire.
    andon = load_andon()
    threshold = andon_threshold_from_env()
    if threshold > 0 and andon.consecutive_bypasses >= threshold:
        gates.append(GateResult(
            name="andon-tripwire",
            status="FAIL",
            detail=(
                f"consecutive={andon.consecutive_bypasses} ≥ "
                f"threshold={threshold} — tripwire would fire"
            ),
        ))
        return gates, "would-refuse"
    if threshold == 0:
        gates.append(GateResult(
            name="andon-tripwire",
            status="SKIP",
            detail="threshold=0 (disabled)",
        ))
    else:
        gates.append(GateResult(
            name="andon-tripwire",
            status="PASS",
            detail=(
                f"consecutive={andon.consecutive_bypasses} < "
                f"threshold={threshold}"
            ),
        ))

    # 10. ε-greedy. We use trace_id as the proxy for atv_id (the
    # latter isn't persisted in ContextMemory) — same determinism
    # idiom, different seed.
    eps = _epsilon_from_env(DEFAULT_EPSILON)
    if _should_explore(atv_id=record.trace_id, epsilon=eps):
        gates.append(GateResult(
            name="epsilon-greedy",
            status="FAIL",
            detail=(
                f"trace_id falls in the explore bucket at ε={eps:.3f} "
                "(forced human review for drift / IPW coverage)"
            ),
        ))
        return gates, "would-refuse"
    gates.append(GateResult(
        name="epsilon-greedy",
        status="PASS",
        detail=(
            f"trace_id falls in the exploit bucket at ε={eps:.3f}"
        ),
    ))

    return gates, "would-bypass"


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


def explain_trace(
    trace_id: str,
    *,
    cm_path: Path | None = None,
    since_seconds: int = 30 * 86400,
) -> ExplainReport:
    """Return a structured explanation of why this trace_id was
    bypassed / refused, both at the time of the call and under
    current state."""
    if not trace_id:
        return ExplainReport(trace_id="", found=False)
    record = _find_record(
        trace_id, cm_path=cm_path, since_seconds=since_seconds,
    )
    if record is None:
        return ExplainReport(trace_id=trace_id, found=False)
    gates, outcome = _walk_gates(record)
    return ExplainReport(
        trace_id=trace_id,
        found=True,
        record=record,
        original_decision=record.decision,
        original_stamps=_extract_original_stamps(record),
        gates=tuple(gates),
        final_simulated_outcome=outcome,
    )


# ──────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────


def render_explain(report: ExplainReport) -> str:
    """Plain-text render — used by the CLI."""
    lines: list[str] = []
    if not report.found:
        return (
            f"  ✗ no ContextMemory record found for trace_id="
            f"{report.trace_id!r}.\n"
            "  Try `aegis report --since 30d` to enumerate "
            "recent traces."
        )

    rec = report.record
    assert rec is not None
    lines.append(f"Trace explain — {report.trace_id}")
    lines.append("─" * 56)
    lines.append(f"  tool:         {rec.tool_name}")
    lines.append(f"  decision:     {rec.decision}")
    lines.append(f"  reason:       {(rec.reason or '(none)')[:80]}")
    lines.append(f"  cost:         ${rec.cost_usd or 0.0:.6f}")
    lines.append(f"  tokens_in:    {rec.tokens_in or 0:,}")
    lines.append(f"  latency_ms:   {rec.latency_ms or 0.0:.1f}")
    lines.append("")

    if report.original_stamps:
        lines.append("Original autonomy stamps (from step_traces):")
        for k, v in report.original_stamps.items():
            short = v if len(v) <= 80 else v[:77] + "..."
            lines.append(f"  · {k}: {short}")
        lines.append("")
    else:
        lines.append(
            "Original autonomy stamps: (none — no autonomy "
            "decision recorded at the time of this call)"
        )
        lines.append("")

    lines.append("Gate-by-gate walkthrough (replayed against current state):")
    lines.append("")
    for i, gate in enumerate(report.gates, 1):
        lines.append(
            f"  {i:>2}. {gate.glyph} {gate.name:<20} "
            f"[{gate.status:<4}]  {gate.detail}"
        )
    lines.append("")
    outcome_str = {
        "would-bypass": "🟢 bypass WOULD engage — verdict ALLOW",
        "would-refuse": "🔴 bypass WOULD be refused — verdict REQUIRE_APPROVAL",
        "not-eligible": "⏭️  not eligible for bypass (decision wasn't "
                        "REQUIRE_APPROVAL)",
        "master-off":   "⏭️  AEGIS_AUTONOMY_ENABLED is off — bypass is a no-op",
    }.get(report.final_simulated_outcome, report.final_simulated_outcome)
    lines.append(f"  → {outcome_str}")
    return "\n".join(lines)


__all__ = [
    "ExplainReport",
    "GateResult",
    "explain_trace",
    "render_explain",
]
