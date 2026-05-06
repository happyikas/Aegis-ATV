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
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext
    from aegis.burnin.anomaly import AnomalyTag


# Stable advisor kind tags. Adding a new advisor → append here +
# update consumers (audit verifier, etc.).
AdvisorKind = Literal["heuristic", "sllm-phi3", "sllm-haiku", "learned-head"]

Decision = Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL", "DEFER"]

# v2.5.2 PR-ψ-multi-domain — domain advisor catalog. Each name is an
# external advisor / specialist the agent (or operator) should consult.
# Closed set; new advisors must land here so audit / dashboard rendering
# stays stable.
DomainAdvisor = Literal[
    "cost-optimizer",         # cost-divergence / budget pressure
    "kv-cache-optimizer",     # cache hit collapse / prefix instability
    "security-reviewer",      # destructive paths / privilege escalation
    "context-compactor",      # token velocity / context saturation
    "test-runner",            # error patterns
    "loop-breaker",           # ≥3 same-call repetitions
    "permission-escalator",   # ambiguous high-impact, needs human ACK
    "human-clarifier",        # backtrack / agent appears confused
]

Priority = Literal["high", "medium", "low"]


# Heuristic version — bump on any rule change so audit can pin
# advices to a specific composer revision.
_HEURISTIC_VERSION = "compose_advice_heuristic_v2_multi_domain"
_HEURISTIC_HASH = hashlib.sha3_256(_HEURISTIC_VERSION.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# The schema
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdvisorRecommendation:
    """A single domain-advisor recommendation produced by the
    sLLM / heuristic. Multiple of these may accompany one ActionAdvice
    when several domains (cost, cache, security, …) are simultaneously
    in scope — exactly the user's "Cost 최적화 advisor를 부르고, KV
    Cache 최적화 advisor를 활용을 권하면서 파일 삭제 이상점에 대해
    조치를 취하세요" pattern.

    Fields
    ------
    advisor:
        One of :data:`DomainAdvisor`. Closed catalog so dashboards /
        replayers can rely on the set.
    priority:
        ``high`` / ``medium`` / ``low``. ``high`` means must address
        before continuing; ``low`` is informational.
    action:
        One-sentence imperative — what the advisor should do.
    reasoning:
        Short explanation grounding the recommendation in cited
        signals. Bounded to 256 chars in JSON I/O.
    cited_signals:
        Names of the COST / KV CACHE / SECURITY metrics (or anomaly
        tags) that triggered this recommendation. Audit traceability.
    """

    advisor: DomainAdvisor
    priority: Priority
    action: str
    reasoning: str = ""
    cited_signals: tuple[str, ...] = field(default_factory=tuple)


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

    # v2.5.2 PR-ψ-multi-domain — multi-advisor recommendations. Closed
    # set per :data:`DomainAdvisor`. Empty tuple when no recommendation
    # applies (e.g. clean ALLOW). The decision/reason fields stay
    # verdict-compatible; this field is the *value-added* surface that
    # closes the patent's "sLLM understands the scene → routes to
    # specialists" claim.
    recommended_advisors: tuple[AdvisorRecommendation, ...] = field(
        default_factory=tuple,
    )

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


def _heuristic_recommendations(
    *,
    cost_signals: dict[str, Any] | None,
    cache_signals: dict[str, Any] | None,
    security_signals: dict[str, Any] | None,
    anomalies: list[AnomalyTag],
    temporal_ctx: TemporalContext | None,
    step_traces: dict[str, Any] | None = None,
) -> tuple[AdvisorRecommendation, ...]:
    """Map signal dicts to a tuple of :class:`AdvisorRecommendation`.

    Rules are deterministic and conservative — each one cites the exact
    metric(s) that triggered it. ``high`` priority means the
    recommendation should be addressed before the tool runs;
    ``medium`` / ``low`` are advisory.
    """
    recs: list[AdvisorRecommendation] = []
    cost = cost_signals or {}
    cache = cache_signals or {}
    sec = security_signals or {}

    # Security — destructive path match → high-priority security review.
    if sec.get("destructive_path_match"):
        cited = ["destructive_path_match"]
        if "policy_rule" in sec:
            cited.append(f"policy_rule={sec['policy_rule']}")
        if "blast_radius" in sec:
            cited.append(f"blast_radius={sec['blast_radius']}")
        recs.append(AdvisorRecommendation(
            advisor="security-reviewer",
            priority="high",
            action=(
                "Block until a human reviewer ACKs the destructive "
                "operation."
            ),
            reasoning=(
                f"firewall matched {sec.get('policy_rule', 'a destructive rule')}; "
                f"blast radius {sec.get('blast_radius', 'unknown')}"
            ),
            cited_signals=tuple(cited),
        ))
    elif sec.get("blast_radius") == "high":
        recs.append(AdvisorRecommendation(
            advisor="security-reviewer",
            priority="medium",
            action="Confirm scope of the high-blast-radius operation.",
            reasoning="step320 reports blast_radius=high",
            cited_signals=("blast_radius",),
        ))

    # Cost — divergence ratio > 2× → cost-optimizer high.
    div_ratio = cost.get("hw_vs_sw_divergence_ratio")
    if isinstance(div_ratio, (int, float)) and div_ratio >= 2.0:
        recs.append(AdvisorRecommendation(
            advisor="cost-optimizer",
            priority="high",
            action=(
                "Investigate HW/SW cost divergence before continuing — "
                "actual compute may be far exceeding billed."
            ),
            reasoning=(
                f"hw_vs_sw_divergence_ratio={div_ratio:.2f}× "
                "(M12 escalation threshold is 2.0)"
            ),
            cited_signals=("hw_vs_sw_divergence_ratio",),
        ))

    # Cost — budget pressure (proj > 90% limit OR step335 warn flag).
    used_ratio = cost.get("budget_used_ratio")
    if (
        isinstance(used_ratio, (int, float)) and used_ratio >= 0.9
    ) or cost.get("budget_warn_flag"):
        cited = []
        if isinstance(used_ratio, (int, float)):
            cited.append("budget_used_ratio")
        if cost.get("budget_warn_flag"):
            cited.append("budget_warn_flag")
        priority: Priority = "high" if (
            isinstance(used_ratio, (int, float)) and used_ratio >= 1.0
        ) else "medium"
        recs.append(AdvisorRecommendation(
            advisor="cost-optimizer",
            priority=priority,
            action=(
                "Trim context or end the session before the budget "
                "ceiling is hit."
            ),
            reasoning=(
                f"projected session cost is "
                f"{(used_ratio or 1.0)*100:.0f}% of budget"
                if isinstance(used_ratio, (int, float))
                else "step335 raised a budget warning"
            ),
            cited_signals=tuple(cited) or ("budget_warn_flag",),
        ))

    # KV cache — significant hit-rate drop.
    drop = cache.get("cache_hit_rate_max_drop_pp")
    if isinstance(drop, (int, float)) and drop >= 30:
        priority = "high" if drop >= 50 else "medium"
        recs.append(AdvisorRecommendation(
            advisor="kv-cache-optimizer",
            priority=priority,
            action=(
                "Audit recent prompt-prefix mutations; the cache is "
                "being re-keyed on most turns."
            ),
            reasoning=(
                f"cache_hit_rate_max_drop_pp={drop:.0f}pp"
                + (
                    f"; prefix re-keys={cache['prefix_re_keys_in_window']}"
                    if "prefix_re_keys_in_window" in cache
                    else ""
                )
            ),
            cited_signals=tuple(
                k for k in (
                    "cache_hit_rate_max_drop_pp",
                    "prefix_re_keys_in_window",
                ) if k in cache
            ),
        ))
    elif cache.get("prefix_stability") == "unstable":
        recs.append(AdvisorRecommendation(
            advisor="kv-cache-optimizer",
            priority="low",
            action="Stabilise the prompt prefix to recover cache hits.",
            reasoning=(
                f"{cache.get('prefix_re_keys_in_window', 0)} prefix "
                "re-keys in window"
            ),
            cited_signals=("prefix_stability",),
        ))

    # Trajectory — token-velocity anomaly → context-compactor.
    has_velocity = any(
        "token_velocity" in t.metric for t in anomalies
    )
    if has_velocity:
        recs.append(AdvisorRecommendation(
            advisor="context-compactor",
            priority="medium",
            action=(
                "Summarise the last N turns and start fresh to "
                "control token velocity."
            ),
            reasoning="window_token_velocity_per_turn z-score elevated",
            cited_signals=("window_token_velocity_per_turn",),
        ))

    # Trajectory — repeated-call pattern → loop-breaker. Triggered by
    # ANY of three signals so the recommendation fires even on a fresh
    # session without burn-in baseline:
    #   - burn-in anomaly tag (mature install)
    #   - temporal_ctx.n_redundant >= 3 (audit history present)
    #   - step336 loop-detector trace (immediate firewall flag)
    # The third was added in v2.7.1 after the demo session showed that
    # loop-breaker missed a 3x-repeat case the firewall had already
    # escalated to REQUIRE_APPROVAL.
    has_redundant = any("redundant" in t.metric for t in anomalies)
    step336_loop = False
    if step_traces:
        s336 = str(step_traces.get("aegis.firewall.step336_loop.run", ""))
        # Detector emits "step336: loop (N× seen) — Tool" on a loop,
        # "step336: redundant read-only (N× seen)" on a redundant repeat,
        # and "step336: fresh call" otherwise. Match on "× seen" which
        # appears only in the firing variants.
        if "× seen" in s336 or "redundant" in s336.lower():
            step336_loop = True
    if has_redundant or step336_loop or (
        temporal_ctx is not None and temporal_ctx.n_redundant >= 3
    ):
        cited = ["session_redundancy_ratio", "n_redundant"]
        if step336_loop:
            cited.append("step336_loop_detector")
        recs.append(AdvisorRecommendation(
            advisor="loop-breaker",
            priority="high",
            action=(
                "Switch tools or narrow the scope — the same call has "
                "repeated within the window."
            ),
            reasoning=(
                f"n_redundant={getattr(temporal_ctx, 'n_redundant', 0)}"
                + ("; step336 fired" if step336_loop else "")
            ),
            cited_signals=tuple(cited),
        ))

    # Trajectory — error pattern → test-runner.
    has_error = any("error" in t.metric for t in anomalies)
    if has_error or (
        temporal_ctx is not None and temporal_ctx.n_errors >= 2
    ):
        recs.append(AdvisorRecommendation(
            advisor="test-runner",
            priority="medium",
            action=(
                "Run the relevant tests / smoke check before retrying "
                "the failing call."
            ),
            reasoning=(
                f"n_errors={getattr(temporal_ctx, 'n_errors', 0)} "
                "in window"
            ),
            cited_signals=("session_error_rate", "n_errors"),
        ))

    # Trajectory — backtrack pattern → human-clarifier.
    has_backtrack = any("backtrack" in t.metric for t in anomalies)
    if has_backtrack or (
        temporal_ctx is not None and temporal_ctx.n_backtracks >= 1
    ):
        recs.append(AdvisorRecommendation(
            advisor="human-clarifier",
            priority="medium",
            action=(
                "Ask the user to confirm the intended change — recent "
                "edit was reverted."
            ),
            reasoning=(
                f"n_backtracks={getattr(temporal_ctx, 'n_backtracks', 0)} "
                "in window"
            ),
            cited_signals=("session_backtrack_ratio", "n_backtracks"),
        ))

    # Default escalation — REQUIRE_APPROVAL with no specific domain
    # signal → permission-escalator (so the operator sees something).
    if not recs:
        decision = sec.get("verdict_decision", "ALLOW")
        if decision in ("REQUIRE_APPROVAL", "BLOCK"):
            recs.append(AdvisorRecommendation(
                advisor="permission-escalator",
                priority="medium",
                action=(
                    "Surface the verdict to the human operator before "
                    "proceeding."
                ),
                reasoning=(
                    f"firewall verdict={decision} without a recognised "
                    "domain signal"
                ),
                cited_signals=("verdict_decision",),
            ))

    return tuple(recs)


def compose_advice_heuristic(
    *,
    temporal_ctx: TemporalContext | None = None,
    anomalies: list[AnomalyTag] | None = None,
    base_decision: Decision = "ALLOW",
    base_reason: str = "",
    current_tool: str = "",
    cost_signals: dict[str, Any] | None = None,
    cache_signals: dict[str, Any] | None = None,
    security_signals: dict[str, Any] | None = None,
    step_traces: dict[str, Any] | None = None,
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

    # PR-ψ-multi-domain: route signal dicts through the
    # heuristic mapper for multi-advisor recommendations.
    recommendations = _heuristic_recommendations(
        cost_signals=cost_signals,
        cache_signals=cache_signals,
        security_signals=security_signals,
        anomalies=tags,
        temporal_ctx=temporal_ctx,
        step_traces=step_traces,
    )

    return ActionAdvice(
        decision=decision,
        reason=reason,
        confidence=confidence,
        next_action_hint=hint,
        alternative_tool=alt,
        cited_anomalies=cited_metrics,
        cited_turns_rel=cited_turns,
        recommended_advisors=recommendations,
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
    if advice.recommended_advisors:
        lines.append("  recommended advisors:")
        for r in advice.recommended_advisors:
            lines.append(
                f"    [{r.priority:<6}] {r.advisor:<22} {r.action}"
            )
            if r.reasoning:
                lines.append(f"               · why: {r.reasoning}")
            if r.cited_signals:
                lines.append(
                    f"               · signals: "
                    f"{', '.join(r.cited_signals)}"
                )
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
    """JSON-serialisable form. ``cited_*`` tuples become lists, and
    each :class:`AdvisorRecommendation` is normalised to a plain dict."""
    d = asdict(advice)
    d["cited_anomalies"] = list(advice.cited_anomalies)
    d["cited_turns_rel"] = list(advice.cited_turns_rel)
    d["recommended_advisors"] = [
        {
            "advisor": r.advisor,
            "priority": r.priority,
            "action": r.action,
            "reasoning": r.reasoning,
            "cited_signals": list(r.cited_signals),
        }
        for r in advice.recommended_advisors
    ]
    return d


_ALLOWED_DOMAIN_ADVISORS: frozenset[str] = frozenset({
    "cost-optimizer", "kv-cache-optimizer", "security-reviewer",
    "context-compactor", "test-runner", "loop-breaker",
    "permission-escalator", "human-clarifier",
})
_ALLOWED_PRIORITIES: frozenset[str] = frozenset({"high", "medium", "low"})


def _recommendation_from_dict(d: dict[str, Any]) -> AdvisorRecommendation | None:
    """Defensive parse of a single recommendation dict — returns
    ``None`` when the advisor name or priority is outside the closed
    catalog (so older or malformed records can't poison replay)."""
    advisor = d.get("advisor", "")
    priority = d.get("priority", "")
    if advisor not in _ALLOWED_DOMAIN_ADVISORS:
        return None
    if priority not in _ALLOWED_PRIORITIES:
        return None
    cited = d.get("cited_signals") or []
    return AdvisorRecommendation(
        advisor=cast(DomainAdvisor, advisor),
        priority=cast(Priority, priority),
        action=str(d.get("action", ""))[:512],
        reasoning=str(d.get("reasoning", ""))[:512],
        cited_signals=tuple(
            str(x) for x in cited if isinstance(x, str)
        ),
    )


def advice_from_dict(d: dict[str, Any]) -> ActionAdvice:
    """Inverse of :func:`advice_to_dict`. Tolerant of missing fields
    so older audit records (pre-v2.5.2) keep loading without
    ``recommended_advisors``."""
    raw_recs = d.get("recommended_advisors") or []
    recs: tuple[AdvisorRecommendation, ...] = tuple(
        r
        for r in (
            _recommendation_from_dict(item)
            for item in raw_recs
            if isinstance(item, dict)
        )
        if r is not None
    )
    return ActionAdvice(
        decision=d.get("decision", "ALLOW"),
        reason=str(d.get("reason", "")),
        confidence=float(d.get("confidence", 0.0)),
        next_action_hint=d.get("next_action_hint"),
        alternative_tool=d.get("alternative_tool"),
        cited_anomalies=tuple(d.get("cited_anomalies") or ()),
        cited_turns_rel=tuple(d.get("cited_turns_rel") or ()),
        recommended_advisors=recs,
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
    "AdvisorRecommendation",
    "Decision",
    "DomainAdvisor",
    "Priority",
    "advice_from_dict",
    "advice_to_audit_record",
    "advice_to_dict",
    "compose_advice_heuristic",
    "render_advice",
]
