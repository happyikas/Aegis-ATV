"""ActionAdvice — sLLM output schema (PR-ζ-schema, Phase A finale).

The Aegis CCTV → sLLM → next-action pipeline (per the patent design
intent) needs a structured contract for what the sLLM returns.
This module defines that contract:

* :class:`ActionAdvice` — the dataclass an advisor produces.
* :func:`compose_advice_heuristic` — a deterministic composer that
  builds an ActionAdvice from a TemporalContext (PR-θ) + anomaly
  tags (PR-ε). It's a *heuristic* placeholder — PR-ζ-head will
  swap the body for an actual sLLM call. The SCHEMA stays the
  same; only the brain changes.
* :func:`render_advice` — operator-readable rendering.
* JSON I/O helpers for persisting advices into the audit chain.

What this is NOT (yet)
----------------------
* Not the sLLM call — that's PR-ζ-head.
* Not a verdict replacement — current step340 keeps producing
  :class:`aegis.schema.Verdict`. ActionAdvice is the *next-action
  recommendation* surface, intended to coexist with verdicts.
* Not wired into the firewall — the heuristic here is for
  testing the schema and providing a fallback when the sLLM is
  unavailable.

Why dataclass first, sLLM later
-------------------------------
Locking the I/O contract before the model call means:
* Tests can verify the schema invariants today
* Multiple advisors (heuristic / Phi-3 / Haiku / future learned
  head) all produce the same shape
* Audit-chain serialization spec is stable across model swaps
* Cross-checking ATV-attribution-head + sLLM verdict (the
  hallucination control) needs both to speak the same shape

The patent claim "sLLM understands the scene and recommends the
next action" requires the recommendation to BE structured —
arbitrary free text wouldn't let downstream firewall enforcement
act on it.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext
    from aegis.burnin.anomaly import AnomalyTag


# Stable advisor kind tags. Adding a new advisor → append here +
# update consumers (audit verifier, etc.).
AdvisorKind = Literal["heuristic", "sllm-phi3", "sllm-haiku", "learned-head"]

Decision = Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL", "DEFER"]


# Heuristic version — bump on any rule change so audit can pin
# advices to a specific composer revision.
_HEURISTIC_VERSION = "compose_advice_heuristic_v1"
_HEURISTIC_HASH = hashlib.sha3_256(_HEURISTIC_VERSION.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# The schema
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionAdvice:
    """Structured output of the sLLM (or heuristic) advisor.

    Decision-class fields (``decision``, ``reason``, ``confidence``)
    are verdict-compatible — a downstream firewall can map an
    ActionAdvice to a :class:`aegis.schema.Verdict` losslessly. The
    advisory fields (``next_action_hint``, ``alternative_tool``,
    ``cited_anomalies``, ``cited_turns_rel``) are the value-added
    surface that goes beyond a binary gate.

    Attributes
    ----------
    decision:
        ``ALLOW`` / ``BLOCK`` / ``REQUIRE_APPROVAL`` / ``DEFER``.
        ``DEFER`` is new (vs Verdict) — meaning "don't decide yet,
        agent should clarify or wait".
    reason:
        Human-readable explanation. Should reference cited
        anomalies / turns when the decision is non-ALLOW.
    confidence:
        Self-reported in [0, 1]. The runtime / firewall can fall
        back to its own policy below a threshold.
    next_action_hint:
        Free-text suggestion of what the agent should do next.
        Examples: "Ask the user to confirm intent before retrying."
        / "Read the file again with a smaller offset before editing."
        ``None`` when there's no specific hint beyond the verdict.
    alternative_tool:
        Suggested tool name to try instead of the one being gated.
        ``None`` when no alternative is identifiable.
    cited_anomalies:
        Names of :class:`AnomalyTag.metric` that drove this advice.
        Audit traceability.
    cited_turns_rel:
        ``turn_index_rel`` values from the TemporalContext that the
        advisor explicitly considered. Audit traceability.
    advisor_kind:
        Which advisor produced this. ``"heuristic"`` for the
        composer in this module; sLLM advisors set their own.
    advisor_hash:
        SHA3-256 of the advisor implementation version. Pin to a
        specific revision for replay / forensics.
    produced_at_ns:
        Wall-clock when this advice was emitted.
    """

    decision: Decision
    reason: str
    confidence: float

    next_action_hint: str | None = None
    alternative_tool: str | None = None

    cited_anomalies: tuple[str, ...] = field(default_factory=tuple)
    cited_turns_rel: tuple[int, ...] = field(default_factory=tuple)

    advisor_kind: AdvisorKind = "heuristic"
    advisor_hash: str = ""
    produced_at_ns: int = 0

    def __post_init__(self) -> None:
        # Defensive: confidence must be in [0, 1]. Frozen dataclass
        # → use object.__setattr__ to clamp.
        if self.confidence < 0.0 or self.confidence > 1.0:
            object.__setattr__(
                self,
                "confidence",
                float(min(1.0, max(0.0, self.confidence))),
            )


# ──────────────────────────────────────────────────────────────────────
# Heuristic composer
# ──────────────────────────────────────────────────────────────────────


def _decision_from_anomalies(
    base_decision: Decision,
    tags: list[AnomalyTag],
) -> tuple[Decision, float]:
    """Escalate the base decision based on anomaly severities.

    Rules (in order):
    * any ``alert`` → REQUIRE_APPROVAL, confidence 0.85
    * ≥ 2 ``warning`` → REQUIRE_APPROVAL, confidence 0.75
    * 1 ``warning`` → keep base, confidence 0.70
    * info-only or none → keep base, confidence 0.90
    """
    n_alert = sum(1 for t in tags if t.severity == "alert")
    n_warning = sum(1 for t in tags if t.severity == "warning")

    if n_alert >= 1:
        return "REQUIRE_APPROVAL", 0.85
    if n_warning >= 2:
        return "REQUIRE_APPROVAL", 0.75
    if n_warning == 1:
        return base_decision, 0.70
    return base_decision, 0.90


def _alternative_tool_for(
    tool_name: str,
    tags: list[AnomalyTag],
) -> str | None:
    """Heuristic alternative-tool suggestion based on the current
    tool and the anomaly mix. Conservative: returns ``None`` rather
    than guess when nothing fits.
    """
    if not tool_name or tool_name == "(unknown)":
        return None

    has_backtrack = any("backtrack" in t.metric for t in tags)
    has_error = any("error" in t.metric for t in tags)
    has_redundant = any("redundant" in t.metric for t in tags)

    # Edit followed by backtrack → re-Read first to verify state.
    if tool_name == "Edit" and has_backtrack:
        return "Read"
    # Bash error → step back to Read for diagnosis.
    if tool_name == "Bash" and has_error:
        return "Read"
    # Repeated identical tool → try a different angle.
    if has_redundant and tool_name in {"Bash", "Grep", "Read"}:
        return "Glob" if tool_name == "Grep" else "Grep"
    return None


def _next_action_hint_for(
    ctx: TemporalContext | None,
    tags: list[AnomalyTag],
) -> str | None:
    """Heuristic action hint based on the trajectory + anomaly mix.

    Hints are short imperative sentences the agent (or operator)
    can act on. Returns ``None`` when no clear corrective action
    suggests itself.
    """
    bullets: list[str] = []
    has_backtrack = any("backtrack" in t.metric for t in tags)
    has_error = any("error" in t.metric for t in tags)
    has_redundant = any("redundant" in t.metric for t in tags)
    has_velocity = any("token_velocity" in t.metric for t in tags)
    has_cache_drop = any(
        "cache_hit_rate" in t.metric or "cache" in t.metric.lower()
        for t in tags
    )

    if has_backtrack:
        bullets.append(
            "agent appears confused (recent edit-revert) — ask the "
            "user to clarify the intended change before continuing"
        )
    if has_error:
        bullets.append(
            "previous tool errored; explain the failure to the user "
            "before retrying"
        )
    if has_redundant:
        bullets.append(
            "same call repeated within window — try a different tool "
            "or a narrower scope"
        )
    if has_velocity:
        bullets.append(
            "token usage is well above baseline; consider summarising "
            "context or starting a fresh session"
        )
    if has_cache_drop and not has_velocity:
        bullets.append(
            "cache hit rate dropped sharply — the prompt prefix likely "
            "changed; consider running `aegis cache-lint` to confirm"
        )

    if not bullets and ctx is not None and ctx.is_progress_stalled:
        bullets.append(
            "task progress has plateaued; consider asking the user "
            "whether the current approach is still on the right track"
        )

    if not bullets:
        return None
    return "; ".join(bullets)


def compose_advice_heuristic(
    *,
    temporal_ctx: TemporalContext | None = None,
    anomalies: list[AnomalyTag] | None = None,
    base_decision: Decision = "ALLOW",
    base_reason: str = "",
    current_tool: str = "",
) -> ActionAdvice:
    """Build an ActionAdvice from temporal context + anomaly tags.

    This is a deterministic, sub-millisecond *placeholder* until
    the sLLM advisor (PR-ζ-head) is wired. The schema produced
    here is identical to what the sLLM advisor will produce, so
    downstream consumers (audit, firewall enforcement, dashboard)
    don't need changes when the brain is swapped.

    Empty inputs → ``ALLOW`` advice with default reason.
    """
    tags = list(anomalies or [])

    decision, confidence = _decision_from_anomalies(base_decision, tags)

    # Reason: combine base + anomaly summary.
    reason_parts: list[str] = []
    if base_reason:
        reason_parts.append(base_reason)
    n_alert = sum(1 for t in tags if t.severity == "alert")
    n_warning = sum(1 for t in tags if t.severity == "warning")
    if n_alert:
        reason_parts.append(
            f"{n_alert} burn-in alert(s) — this run is "
            "well outside trained-normal range"
        )
    if n_warning:
        reason_parts.append(
            f"{n_warning} burn-in warning(s)"
        )
    if not reason_parts:
        reason_parts.append("no anomalies; pass-through")
    reason = "; ".join(reason_parts)

    # Hint + alternative.
    hint = _next_action_hint_for(temporal_ctx, tags)
    alt = _alternative_tool_for(current_tool, tags)

    # Citation lists.
    cited_metrics = tuple(sorted({t.metric for t in tags}))
    cited_turns: tuple[int, ...] = ()
    if temporal_ctx is not None:
        # Cite turns whose flags drove an anomaly. For now, cite
        # any turn with a backtrack/redundant/error signal.
        cited_turns = tuple(
            s.turn_index_rel
            for s in temporal_ctx.history
            if s.backtrack or s.redundant or s.is_error
        )

    return ActionAdvice(
        decision=decision,
        reason=reason,
        confidence=confidence,
        next_action_hint=hint,
        alternative_tool=alt,
        cited_anomalies=cited_metrics,
        cited_turns_rel=cited_turns,
        advisor_kind="heuristic",
        advisor_hash=_HEURISTIC_HASH,
        produced_at_ns=time.time_ns(),
    )


# ──────────────────────────────────────────────────────────────────────
# Renderer + JSON I/O
# ──────────────────────────────────────────────────────────────────────


def render_advice(advice: ActionAdvice) -> str:
    """Operator-readable rendering for audit / debug."""
    lines = [
        f"ActionAdvice [{advice.advisor_kind} @ "
        f"{advice.advisor_hash[:16]}…]",
        f"  decision:    {advice.decision}",
        f"  confidence:  {advice.confidence:.2f}",
        f"  reason:      {advice.reason}",
    ]
    if advice.next_action_hint:
        lines.append(f"  hint:        {advice.next_action_hint}")
    if advice.alternative_tool:
        lines.append(f"  alt_tool:    {advice.alternative_tool}")
    if advice.cited_anomalies:
        lines.append(
            f"  cited (anomalies): {', '.join(advice.cited_anomalies)}"
        )
    if advice.cited_turns_rel:
        lines.append(
            f"  cited (turns):     "
            f"{', '.join(str(t) for t in advice.cited_turns_rel)}"
        )
    return "\n".join(lines)


def advice_to_dict(advice: ActionAdvice) -> dict[str, Any]:
    """JSON-serialisable form. ``cited_*`` tuples become lists."""
    d = asdict(advice)
    d["cited_anomalies"] = list(advice.cited_anomalies)
    d["cited_turns_rel"] = list(advice.cited_turns_rel)
    return d


def advice_from_dict(d: dict[str, Any]) -> ActionAdvice:
    """Inverse of :func:`advice_to_dict`. Tolerant of missing fields
    so older audit records keep loading."""
    return ActionAdvice(
        decision=d.get("decision", "ALLOW"),
        reason=str(d.get("reason", "")),
        confidence=float(d.get("confidence", 0.0)),
        next_action_hint=d.get("next_action_hint"),
        alternative_tool=d.get("alternative_tool"),
        cited_anomalies=tuple(d.get("cited_anomalies") or ()),
        cited_turns_rel=tuple(d.get("cited_turns_rel") or ()),
        advisor_kind=d.get("advisor_kind", "heuristic"),
        advisor_hash=str(d.get("advisor_hash", "")),
        produced_at_ns=int(d.get("produced_at_ns", 0)),
    )


def advice_to_audit_record(
    advice: ActionAdvice,
    *,
    aid: str,
    tool: str,
) -> dict[str, Any]:
    """Wrap an ActionAdvice into the audit-chain record shape used
    by other Aegis hooks (PR #45 / #46 / #47 conventions). Caller
    appends this via :func:`aegis.audit.local_chain.append`."""
    return {
        "ts_ns": advice.produced_at_ns or time.time_ns(),
        "tool": tool,
        "aid": aid,
        "hook": "ActionAdvice",
        "mode": "local",
        "decision": advice.decision,
        "reason": advice.reason,
        "explain": {"action_advice": advice_to_dict(advice)},
    }


__all__ = [
    "ActionAdvice",
    "AdvisorKind",
    "Decision",
    "advice_from_dict",
    "advice_to_audit_record",
    "advice_to_dict",
    "compose_advice_heuristic",
    "render_advice",
]
