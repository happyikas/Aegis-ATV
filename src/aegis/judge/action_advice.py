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


# v2.8 PR-α — closed catalog of executable verbs for ``ActionStep``.
# Each verb is a structured action the operator (or downstream
# automation) can actually perform. Adding a new verb is a deliberate
# schema change: extend this Literal AND update _VERB_PARAM_KEYS so
# defensive parsing accepts it.
ActionVerb = Literal[
    # Cost / context shaping
    "prune-turns",          # drop specific past turns to recover budget / cache
    "summarize-window",     # collapse a span of turns into a summary
    "swap-model",           # switch to a cheaper model for the remainder
    "end-session",          # graceful session termination
    # Tool / flow shaping
    "swap-tool",            # use a different tool with semantic similarity
    "narrow-scope",         # tighten the args of the current call
    "clarify-intent",       # ask the user a clarifying question
    # Diagnostic / verification
    "run-diagnostic",       # execute a diagnostic command (e.g. pytest)
    "verify-state",         # check an invariant before continuing
    # Human-in-the-loop
    "notify-operator",      # ping a channel; non-blocking
    "require-approval",     # blocking — wait for human ACK
]


# Per-verb required parameter keys. Defensive parser drops any step
# whose ``parameters`` dict is missing required keys, so a sLLM
# hallucinating "swap-model with no model name" can't pollute the
# audit chain. Lists here are required-keys-only; extra keys are
# allowed (forward compat).
_VERB_PARAM_KEYS: dict[str, frozenset[str]] = {
    "prune-turns": frozenset({"turn_indices_rel"}),
    "summarize-window": frozenset({"turn_range"}),
    "swap-model": frozenset({"from_model", "to_model"}),
    "end-session": frozenset(),
    "swap-tool": frozenset({"from_tool", "to_tool"}),
    "narrow-scope": frozenset({"original_args", "suggested_args"}),
    "clarify-intent": frozenset({"clarifying_question"}),
    "run-diagnostic": frozenset({"diagnostic_command"}),
    "verify-state": frozenset({"check"}),
    "notify-operator": frozenset({"channel", "summary"}),
    "require-approval": frozenset({"reason"}),
}

_ALLOWED_VERBS: frozenset[str] = frozenset(_VERB_PARAM_KEYS)


# Heuristic version — bump on any rule change so audit can pin
# advices to a specific composer revision. v3 adds action_steps.
_HEURISTIC_VERSION = "compose_advice_heuristic_v3_action_steps"
_HEURISTIC_HASH = hashlib.sha3_256(_HEURISTIC_VERSION.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# The schema
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionStep:
    """One executable step the operator (or downstream automation) can
    perform. Introduced in v2.8 PR-α to give Tier 3 sLLM-driven advice
    a structured, machine-readable surface beyond the free-text
    ``action`` field.

    Fields
    ------
    verb:
        One of :data:`ActionVerb` — closed catalog so the operator UI,
        downstream automation, and audit replay can depend on the set.
    parameters:
        verb-specific dict of concrete values (turn indices, model
        names, file paths, tokens-saved estimates, etc.). Required
        keys are enforced by ``_VERB_PARAM_KEYS``; extra keys allowed.
    expected_impact:
        Quantitative outcome string (e.g. "saves ~$0.42; reduces
        ratio 1.50 → 1.18"). Bounded to 256 chars in JSON I/O.
    confidence:
        Self-reported in [0, 1]. The operator UI may show different
        affordances for low-confidence steps.
    cited_signals:
        Names of the COST / CACHE / SECURITY / TEMPORAL signals that
        directly support this step. Audit traceability + dashboard
        drill-through.

    Why a closed verb catalog
    -------------------------
    sLLM hallucinations like "do-magic-fix" would otherwise pollute
    the audit chain. The defensive parser drops any step whose verb
    isn't in the catalog. This makes Tier 3 advisors usable with raw
    Haiku 4.5 (no fine-tuning) — the schema constrains the freedom.
    """

    verb: ActionVerb
    parameters: dict[str, Any]
    expected_impact: str = ""
    confidence: float = 0.5
    cited_signals: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.confidence < 0.0 or self.confidence > 1.0:
            object.__setattr__(
                self,
                "confidence",
                float(min(1.0, max(0.0, self.confidence))),
            )


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
        One-sentence imperative — what the advisor should do (legacy
        free-text surface; kept for stderr / dashboards).
    reasoning:
        Short explanation grounding the recommendation in cited
        signals. Bounded to 256 chars in JSON I/O.
    cited_signals:
        Names of the COST / KV CACHE / SECURITY metrics (or anomaly
        tags) that triggered this recommendation. Audit traceability.
    action_steps:
        v2.8 PR-α — structured executable steps. Empty tuple when the
        advisor has nothing to suggest beyond ``action`` (e.g. older
        records or heuristic-only paths that haven't yet been extended
        to emit steps). Tier 3 sLLM advice typically populates this
        with 1-3 steps.
    """

    advisor: DomainAdvisor
    priority: Priority
    action: str
    reasoning: str = ""
    cited_signals: tuple[str, ...] = field(default_factory=tuple)
    action_steps: tuple[ActionStep, ...] = field(default_factory=tuple)


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


# v2.8 PR-δ — Layer A action_steps templates per advisor.
# Each builder returns ``tuple[ActionStep, ...]`` populated with values
# from the live signals + temporal_ctx. Dummy advisor (heuristic path)
# emits these so users without ANTHROPIC_API_KEY still get concrete
# advice; Tier 3 sLLM may produce richer steps but the schema is the
# same.


# Simple tool swaps for loop-breaker. Picks the most common alternative
# without depending on the PR-κ embedding table (Layer A keeps imports
# minimal). The advisor.py path can pass action_table for semantic
# similarity-based swaps in v2.9.
_LOOP_BREAKER_TOOL_SWAPS: dict[str, str] = {
    "Read":   "Grep",
    "Bash":   "Glob",
    "Edit":   "Read",
    "Glob":   "Grep",
    "Grep":   "Glob",
    "Write":  "Edit",
}

# Default cheaper-model suggestions per current model. Conservative —
# only a few well-known pairs; sLLM has the broader knowledge.
_CHEAPER_MODEL_MAP: dict[str, tuple[str, float]] = {
    "claude-opus-4-7":          ("claude-haiku-4-5", 3.0),
    "claude-opus-4-7[1m]":      ("claude-haiku-4-5", 3.0),
    "claude-sonnet-4-6":        ("claude-haiku-4-5", 1.5),
    "claude-haiku-4-5":         ("claude-haiku-4-7", 1.5),  # if available
}


def _identify_expensive_turns(
    ctx: TemporalContext | None,
    *,
    top_k: int = 3,
    min_tokens: int = 1000,
) -> list[int]:
    """Pick the ``top_k`` turn_index_rel values with highest combined
    in+out tokens (above ``min_tokens``). Returns [] when temporal_ctx
    is empty / no turn meets the threshold."""
    if ctx is None or not ctx.history:
        return []
    candidates = [
        (s.input_tokens + s.output_tokens, s.turn_index_rel)
        for s in ctx.history
        if (s.input_tokens + s.output_tokens) >= min_tokens
    ]
    candidates.sort(reverse=True)
    return [t for _, t in candidates[:top_k]]


def _identify_cache_break_turns(
    ctx: TemporalContext | None,
    *,
    drop_threshold_pp: float = 0.20,
) -> list[int]:
    """Pick turn_index_rel values where cache_hit_rate dropped by
    ``drop_threshold_pp`` from the previous turn."""
    if ctx is None or len(ctx.history) < 2:
        return []
    out: list[int] = []
    prev = ctx.history[0].cache_hit_rate
    for s in ctx.history[1:]:
        if prev - s.cache_hit_rate >= drop_threshold_pp:
            out.append(s.turn_index_rel)
        prev = s.cache_hit_rate
    return out


def _build_security_reviewer_steps(
    sec: dict[str, Any], destructive_match: bool,
) -> tuple[ActionStep, ...]:
    if destructive_match:
        rule = sec.get("policy_rule", "destructive")
        blast = sec.get("blast_radius", "unknown")
        return (
            ActionStep(
                verb="require-approval",
                parameters={
                    "approver_role": "security-reviewer",
                    "reason": (
                        f"destructive operation matched {rule}; "
                        f"blast_radius={blast}"
                    ),
                    "artifacts": [str(rule), f"blast={blast}"],
                },
                expected_impact=(
                    "blocks tool execution until human ACK"
                ),
                confidence=0.95,
                cited_signals=("destructive_path_match",
                               "policy_rule", "blast_radius"),
            ),
        )
    # Non-match but high blast radius - notify only.
    return (
        ActionStep(
            verb="notify-operator",
            parameters={
                "channel": "#aegis-security",
                "urgency": "medium",
                "summary": (
                    f"high blast-radius op: "
                    f"{sec.get('blast_radius', 'unknown')}"
                ),
            },
            expected_impact="security operator informed",
            confidence=0.7,
            cited_signals=("blast_radius",),
        ),
    )


def _build_cost_optimizer_steps(
    cost: dict[str, Any],
    ctx: TemporalContext | None,
    current_model: str | None,
) -> tuple[ActionStep, ...]:
    steps: list[ActionStep] = []

    # Pick expensive turns for prune-turns.
    expensive = _identify_expensive_turns(ctx, top_k=3, min_tokens=1000)
    used_ratio = cost.get("budget_used_ratio")
    has_budget_pressure = (
        (isinstance(used_ratio, (int, float)) and used_ratio >= 0.9)
        or cost.get("budget_warn_flag")
    )

    if has_budget_pressure and expensive:
        # Estimate savings: sum of token counts × rough $/token
        # (using $0.000003 ≈ Haiku-4.5 input rate as a lower bound).
        saved_tokens = 0
        if ctx is not None:
            for s in ctx.history:
                if s.turn_index_rel in expensive:
                    saved_tokens += s.input_tokens + s.output_tokens
        saved_dollars = saved_tokens * 0.000003
        steps.append(ActionStep(
            verb="prune-turns",
            parameters={
                "turn_indices_rel": expensive,
                "saved_tokens_estimate": int(saved_tokens),
                "saved_dollars_estimate": round(saved_dollars, 4),
            },
            expected_impact=(
                f"~{saved_tokens} tokens trimmed "
                f"(~${saved_dollars:.3f} saved)"
            ),
            confidence=0.7,
            cited_signals=("budget_used_ratio", "cumulative_tokens_after"),
        ))

    # Swap-model suggestion when model is known and a cheaper option exists.
    if has_budget_pressure and current_model:
        cheaper = _CHEAPER_MODEL_MAP.get(current_model)
        if cheaper is not None:
            to_model, ratio = cheaper
            steps.append(ActionStep(
                verb="swap-model",
                parameters={
                    "from_model": current_model,
                    "to_model": to_model,
                    "ratio_savings": ratio,
                },
                expected_impact=(
                    f"~{ratio:.1f}x cheaper for the remaining session"
                ),
                confidence=0.5,
                cited_signals=("budget_used_ratio",),
            ))

    # End-session at extreme over-budget.
    if isinstance(used_ratio, (int, float)) and used_ratio >= 1.5:
        steps.append(ActionStep(
            verb="end-session",
            parameters={
                "reason": (
                    f"budget {used_ratio*100:.0f}% over limit; "
                    "cap exceeded"
                ),
            },
            expected_impact="prevents further runaway cost",
            confidence=0.4,
            cited_signals=("budget_used_ratio",),
        ))

    # Divergence path - notify-operator + verify-state.
    div_ratio = cost.get("hw_vs_sw_divergence_ratio")
    if isinstance(div_ratio, (int, float)) and div_ratio >= 2.0:
        steps.append(ActionStep(
            verb="notify-operator",
            parameters={
                "channel": "#aegis-cost",
                "urgency": "high",
                "summary": (
                    f"HW/SW divergence {div_ratio:.2f}x "
                    "(M12 threshold 2.0)"
                ),
            },
            expected_impact=(
                "cost / security team aware of HW-vs-SW mismatch"
            ),
            confidence=0.85,
            cited_signals=("hw_vs_sw_divergence_ratio",),
        ))

    return tuple(steps)


def _build_kv_cache_optimizer_steps(
    cache: dict[str, Any],
    ctx: TemporalContext | None,
) -> tuple[ActionStep, ...]:
    steps: list[ActionStep] = []
    drop = cache.get("cache_hit_rate_max_drop_pp")

    cache_break_turns = _identify_cache_break_turns(ctx)
    if isinstance(drop, (int, float)) and drop >= 30 and cache_break_turns:
        # Estimate tokens saved by pruning the cache-break turns.
        saved_tokens = 0
        if ctx is not None:
            for s in ctx.history:
                if s.turn_index_rel in cache_break_turns:
                    saved_tokens += s.input_tokens + s.output_tokens
        steps.append(ActionStep(
            verb="prune-turns",
            parameters={
                "turn_indices_rel": cache_break_turns,
                "saved_tokens_estimate": int(saved_tokens),
                "saved_dollars_estimate": round(
                    saved_tokens * 0.000003, 4
                ),
            },
            expected_impact=(
                f"prune {len(cache_break_turns)} cache-breaking turns; "
                "prefix re-stabilises"
            ),
            confidence=0.75,
            cited_signals=("cache_hit_rate_max_drop_pp",
                           "prefix_re_keys_in_window"),
        ))

    if cache.get("prefix_stability") == "unstable" and ctx is not None and ctx.history:
        # Suggest summarising the prefix-unstable span.
        first = ctx.history[0].turn_index_rel
        last = ctx.history[-1].turn_index_rel
        steps.append(ActionStep(
            verb="summarize-window",
            parameters={
                "turn_range": [first, last],
                "retain_categories": ["tool_calls", "decisions"],
            },
            expected_impact="collapse unstable prefix into a summary",
            confidence=0.5,
            cited_signals=("prefix_stability",),
        ))

    return tuple(steps)


def _build_loop_breaker_steps(
    current_tool: str,
    ctx: TemporalContext | None,
) -> tuple[ActionStep, ...]:
    steps: list[ActionStep] = []
    swap_to = _LOOP_BREAKER_TOOL_SWAPS.get(current_tool)
    if swap_to:
        steps.append(ActionStep(
            verb="swap-tool",
            parameters={
                "from_tool": current_tool,
                "to_tool": swap_to,
                "similarity_score": 0.6,  # heuristic placeholder
            },
            expected_impact=(
                f"break the loop by switching to {swap_to}"
            ),
            confidence=0.7,
            cited_signals=("session_redundancy_ratio",),
        ))
    # Narrow-scope as a fallback / additional step.
    if current_tool:
        steps.append(ActionStep(
            verb="narrow-scope",
            parameters={
                "original_args": "(current call args)",
                "suggested_args": "narrower path / pattern",
            },
            expected_impact=(
                "fewer matches per call → fewer redundant repeats"
            ),
            confidence=0.4,
            cited_signals=("session_redundancy_ratio",),
        ))
    return tuple(steps)


def _build_test_runner_steps(
    ctx: TemporalContext | None,
) -> tuple[ActionStep, ...]:
    return (
        ActionStep(
            verb="run-diagnostic",
            parameters={
                "diagnostic_command": "pytest -x --maxfail=1",
                "expected_signal": "all tests pass",
            },
            expected_impact=(
                "isolates whether the recent error is reproducible"
            ),
            confidence=0.5,
            cited_signals=("session_error_rate", "n_errors"),
        ),
    )


def _build_human_clarifier_steps(
    ctx: TemporalContext | None,
) -> tuple[ActionStep, ...]:
    backtrack_count = (
        getattr(ctx, "n_backtracks", 0) if ctx is not None else 0
    )
    return (
        ActionStep(
            verb="clarify-intent",
            parameters={
                "clarifying_question": (
                    f"You reverted {backtrack_count} recent edit(s). "
                    "Which direction did you actually intend?"
                ),
            },
            expected_impact=(
                "user re-states intent → agent stops oscillating"
            ),
            confidence=0.65,
            cited_signals=("session_backtrack_ratio", "n_backtracks"),
        ),
    )


def _build_context_compactor_steps(
    ctx: TemporalContext | None,
) -> tuple[ActionStep, ...]:
    if ctx is None or not ctx.history:
        return ()
    first = ctx.history[0].turn_index_rel
    end = ctx.history[-1].turn_index_rel - 2  # leave last 2 turns
    if first > end:
        return ()
    return (
        ActionStep(
            verb="summarize-window",
            parameters={
                "turn_range": [first, end],
                "retain_categories": ["tool_calls", "decisions",
                                       "user_messages"],
            },
            expected_impact=(
                "compact the early window; recent 2 turns kept verbatim"
            ),
            confidence=0.5,
            cited_signals=("window_token_velocity_per_turn",),
        ),
    )


def _build_permission_escalator_steps(
    decision: str,
) -> tuple[ActionStep, ...]:
    return (
        ActionStep(
            verb="notify-operator",
            parameters={
                "channel": "#aegis-approvals",
                "urgency": "high" if decision == "BLOCK" else "medium",
                "summary": (
                    f"firewall verdict={decision} requires operator "
                    "review"
                ),
            },
            expected_impact="operator notified; awaits manual decision",
            confidence=0.6,
            cited_signals=("verdict_decision",),
        ),
    )


def _heuristic_recommendations(
    *,
    cost_signals: dict[str, Any] | None,
    cache_signals: dict[str, Any] | None,
    security_signals: dict[str, Any] | None,
    anomalies: list[AnomalyTag],
    temporal_ctx: TemporalContext | None,
    step_traces: dict[str, Any] | None = None,
    current_tool: str = "",
    current_model: str | None = None,
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
            action_steps=_build_security_reviewer_steps(
                sec, destructive_match=True,
            ),
        ))
    elif sec.get("blast_radius") == "high":
        recs.append(AdvisorRecommendation(
            advisor="security-reviewer",
            priority="medium",
            action="Confirm scope of the high-blast-radius operation.",
            reasoning="step320 reports blast_radius=high",
            cited_signals=("blast_radius",),
            action_steps=_build_security_reviewer_steps(
                sec, destructive_match=False,
            ),
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
            action_steps=_build_cost_optimizer_steps(
                cost, temporal_ctx, current_model,
            ),
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
        # Skip duplicate cost-optimizer if divergence already added one.
        already_cost = any(
            r.advisor == "cost-optimizer" for r in recs
        )
        if not already_cost:
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
                action_steps=_build_cost_optimizer_steps(
                    cost, temporal_ctx, current_model,
                ),
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
            action_steps=_build_kv_cache_optimizer_steps(
                cache, temporal_ctx,
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
            action_steps=_build_kv_cache_optimizer_steps(
                cache, temporal_ctx,
            ),
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
            action_steps=_build_context_compactor_steps(temporal_ctx),
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
            action_steps=_build_loop_breaker_steps(
                current_tool, temporal_ctx,
            ),
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
            action_steps=_build_test_runner_steps(temporal_ctx),
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
            action_steps=_build_human_clarifier_steps(temporal_ctx),
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
                action_steps=_build_permission_escalator_steps(decision),
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
    current_model: str | None = None,
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
        current_tool=current_tool,
        current_model=current_model,
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


def _truncate_repr(value: Any, max_len: int = 40) -> str:
    """Compact, terminal-safe value representation for rendering
    ``ActionStep.parameters``. Long lists / strings get cut with `…`."""
    if isinstance(value, str):
        return value if len(value) <= max_len else value[:max_len - 1] + "…"
    if isinstance(value, list):
        if len(value) <= 6:
            return "[" + ", ".join(repr(x) for x in value) + "]"
        head = ", ".join(repr(x) for x in value[:5])
        return f"[{head}, …+{len(value) - 5} more]"
    return repr(value)


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
            if r.action_steps:
                lines.append("               · steps:")
                for i, step in enumerate(r.action_steps, 1):
                    lines.append(
                        f"                  {i}. {step.verb} "
                        f"(conf={step.confidence:.2f})"
                    )
                    if step.parameters:
                        # Render parameters compactly; long values
                        # truncated for terminal readability.
                        params_str = ", ".join(
                            f"{k}={_truncate_repr(v)}"
                            for k, v in step.parameters.items()
                        )
                        lines.append(
                            f"                     params: {params_str}"
                        )
                    if step.expected_impact:
                        lines.append(
                            f"                     impact: "
                            f"{step.expected_impact}"
                        )
                    if step.cited_signals:
                        lines.append(
                            f"                     signals: "
                            f"{', '.join(step.cited_signals)}"
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


def _action_step_to_dict(step: ActionStep) -> dict[str, Any]:
    return {
        "verb": step.verb,
        "parameters": dict(step.parameters),
        "expected_impact": step.expected_impact,
        "confidence": step.confidence,
        "cited_signals": list(step.cited_signals),
    }


def advice_to_dict(advice: ActionAdvice) -> dict[str, Any]:
    """JSON-serialisable form. ``cited_*`` tuples become lists, and
    each :class:`AdvisorRecommendation` is normalised to a plain dict.
    v2.8 PR-α — also serialises nested :class:`ActionStep` items."""
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
            "action_steps": [
                _action_step_to_dict(s) for s in r.action_steps
            ],
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


def _validate_turn_indices(value: Any) -> bool:
    """``turn_indices_rel`` must be a list of non-positive ints (0 is
    'current', -1 is one back, etc.). Empty list rejected (a step that
    prunes no turns is meaningless)."""
    if not isinstance(value, list) or not value:
        return False
    for x in value:
        if not isinstance(x, int) or isinstance(x, bool):
            return False
        if x > 0:
            return False  # future turns can't be pruned
    return True


def _validate_turn_range(value: Any) -> bool:
    """``turn_range`` is a 2-element [start, end] of non-positive ints
    where start <= end."""
    if not isinstance(value, list) or len(value) != 2:
        return False
    start, end = value
    if not (isinstance(start, int) and isinstance(end, int)):
        return False
    if isinstance(start, bool) or isinstance(end, bool):
        return False
    if start > 0 or end > 0:
        return False
    return start <= end


def _validate_non_negative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def _validate_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# Per-verb param-shape validators. Each entry maps a parameter key to
# a predicate. A step whose parameters fail ANY validator is dropped.
# Keys not in this map are pass-through (extra keys allowed).
_VERB_PARAM_VALIDATORS: dict[str, dict[str, Any]] = {
    "prune-turns": {
        "turn_indices_rel": _validate_turn_indices,
        "saved_tokens_estimate": _validate_non_negative_number,
        "saved_dollars_estimate": _validate_non_negative_number,
    },
    "summarize-window": {
        "turn_range": _validate_turn_range,
    },
    "swap-model": {
        "from_model": _validate_non_empty_string,
        "to_model": _validate_non_empty_string,
        "ratio_savings": _validate_non_negative_number,
    },
    "swap-tool": {
        "from_tool": _validate_non_empty_string,
        "to_tool": _validate_non_empty_string,
    },
    "narrow-scope": {
        "original_args": _validate_non_empty_string,
        "suggested_args": _validate_non_empty_string,
    },
    "clarify-intent": {
        "clarifying_question": _validate_non_empty_string,
    },
    "run-diagnostic": {
        "diagnostic_command": _validate_non_empty_string,
    },
    "verify-state": {
        "check": _validate_non_empty_string,
    },
    "notify-operator": {
        "channel": _validate_non_empty_string,
        "summary": _validate_non_empty_string,
    },
    "require-approval": {
        "reason": _validate_non_empty_string,
    },
}


def _action_step_from_dict(d: dict[str, Any]) -> ActionStep | None:
    """Defensive parse of one ActionStep. Returns ``None`` when:

    * verb is outside :data:`_ALLOWED_VERBS` (sLLM hallucinated a verb)
    * parameters dict is missing any required key for the verb
    * parameters is not a dict / structurally invalid
    * v2.8 PR-γ — any per-verb parameter validator rejects the value
      (e.g. positive turn index for prune-turns, empty model name for
      swap-model, etc.)

    Bounds string fields and clamps confidence to [0,1] via
    :meth:`ActionStep.__post_init__`.
    """
    verb = d.get("verb", "")
    if verb not in _ALLOWED_VERBS:
        return None
    raw_params = d.get("parameters")
    if not isinstance(raw_params, dict):
        return None
    required = _VERB_PARAM_KEYS.get(verb, frozenset())
    if not required.issubset(raw_params.keys()):
        return None

    # PR-γ: per-key shape validation. A failing key drops the step.
    # Keys that aren't in the validator map are pass-through (forward
    # compat for extra metadata the model decides to add).
    validators = _VERB_PARAM_VALIDATORS.get(verb, {})
    for key, validator in validators.items():
        if key in raw_params and not validator(raw_params[key]):
            return None

    cited = d.get("cited_signals") or []
    return ActionStep(
        verb=cast(ActionVerb, verb),
        parameters=dict(raw_params),  # shallow copy — values pass through
        expected_impact=str(d.get("expected_impact", ""))[:256],
        confidence=float(d.get("confidence", 0.5)),
        cited_signals=tuple(
            str(x) for x in cited if isinstance(x, str)
        ),
    )


def _recommendation_from_dict(d: dict[str, Any]) -> AdvisorRecommendation | None:
    """Defensive parse of a single recommendation dict — returns
    ``None`` when the advisor name or priority is outside the closed
    catalog (so older or malformed records can't poison replay).
    v2.8 PR-α — also parses nested ``action_steps`` with the same
    defensive contract; unknown verbs are silently dropped."""
    advisor = d.get("advisor", "")
    priority = d.get("priority", "")
    if advisor not in _ALLOWED_DOMAIN_ADVISORS:
        return None
    if priority not in _ALLOWED_PRIORITIES:
        return None
    cited = d.get("cited_signals") or []

    raw_steps = d.get("action_steps") or []
    steps: tuple[ActionStep, ...] = ()
    if isinstance(raw_steps, list):
        steps = tuple(
            s for s in (
                _action_step_from_dict(item)
                for item in raw_steps
                if isinstance(item, dict)
            )
            if s is not None
        )

    return AdvisorRecommendation(
        advisor=cast(DomainAdvisor, advisor),
        priority=cast(Priority, priority),
        action=str(d.get("action", ""))[:512],
        reasoning=str(d.get("reasoning", ""))[:512],
        cited_signals=tuple(
            str(x) for x in cited if isinstance(x, str)
        ),
        action_steps=steps,
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
    "ActionStep",
    "ActionVerb",
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
