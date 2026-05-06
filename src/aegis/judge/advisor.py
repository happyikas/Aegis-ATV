"""Advisor backends — sLLM-driven ActionAdvice composition (PR-ζ-head, Phase C).

PR-ζ-head finishes the CCTV → narrative → sLLM → next-action pipeline that
PR-θ (TemporalContext), PR-ε (BurnIn anomalies), PR-ι (TrajectoryCatalog),
PR-η (IntentClassifier), and PR-κ (ActionTable) feed into.

Architecture
------------
:func:`aegis.judge.action_advice.compose_advice_heuristic` is a deterministic
fallback. This module adds a sibling, :func:`compose_advice_sllm`, that
consumes the same inputs but routes them through an Anthropic Haiku call,
producing the same :class:`ActionAdvice` shape.

Provider selection (``AEGIS_ADVISOR_PROVIDER``):

* ``dummy`` (default) — :class:`DummyAdvisor` delegates to the heuristic
  composer. Zero-cost, deterministic, no API key required.
* ``haiku`` — :class:`HaikuAdvisor`, Anthropic Haiku 4.5 backed. Falls back
  to :class:`DummyAdvisor` automatically when ``ANTHROPIC_API_KEY`` is
  missing or the API response is unparseable.

Audit pinning
-------------
* ``advisor_kind`` is set per implementation (``heuristic`` / ``sllm-haiku``).
* ``advisor_hash`` is SHA3-256 over (revision || model || system-prompt).
  The audit-chain replayer can reject advices produced by a different
  revision than the one currently shipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any, Protocol, cast

from aegis.judge.action_advice import (
    ActionAdvice,
    AdvisorKind,
    AdvisorRecommendation,
    Decision,
    DomainAdvisor,
    Priority,
    compose_advice_heuristic,
)
from aegis.judge.advisor_signals import (
    render_cache_signals,
    render_cost_signals,
    render_security_signals,
)

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext
    from aegis.burnin.action_embeddings import ActionTable
    from aegis.burnin.anomaly import AnomalyTag, BurnInBaseline
    from aegis.burnin.intent_classifier import IntentClassifier
    from aegis.burnin.trajectory_catalog import TrajectoryCatalog


# Bump on any prompt / model change so audit can pin advices to a
# specific revision. v2 = PR-ψ-multi-domain (cost/cache/security
# narrative sections + recommended_advisors output).
_ADVISOR_PROMPT_VERSION = "advisor_v2_multi_domain"
_ADVISOR_MODEL = "claude-haiku-4-5-20251001"


ADVISOR_SYSTEM_PROMPT = """\
You are a multi-domain advisor router for AI agent tool calls. You read
a CCTV-style narrative of the agent's recent activity and route the
situation to the right domain specialists.

Narrative sections you may see (any may be absent):

  TEMPORAL TRAJECTORY      — last N tool calls with outcome / cache / token info
  TRAJECTORY METRICS       — aggregate token / cache / velocity numbers
  ANOMALIES vs BURN-IN     — z-score deviations from the trained baseline
  NEAREST BURN-IN CLUSTERS — semantically nearest k-means trajectory archetypes
  TASK INTENT PREDICTION   — softmax over {debug, explore, edit, test, refactor,
                              review, create, general}
  COST METRICS             — cumulative/projected $, hw-vs-sw divergence ratio
  KV CACHE METRICS         — hit rate, prefix stability, re-key count
  SECURITY SIGNALS         — destructive path matches, blast radius, rule hits
  CANDIDATE ALTERNATIVES   — tools semantically similar to the proposed call
  PROPOSED CALL            — tool the firewall is about to gate
  BASE VERDICT             — what the deterministic firewall already decided

Closed catalog of domain advisors you may recommend:

  cost-optimizer        — cost divergence / budget pressure
  kv-cache-optimizer    — cache hit collapse / prefix instability
  security-reviewer     — destructive paths / privilege escalation
  context-compactor     — token velocity / context saturation
  test-runner           — error patterns
  loop-breaker          — same call repeated >=3 times
  permission-escalator  — high-impact ambiguous op needing human ACK
  human-clarifier       — backtrack / agent appears confused

Respond with ONLY a JSON object, no prose, exactly this shape:
{
  "decision": "ALLOW|BLOCK|REQUIRE_APPROVAL|DEFER",
  "reason": "string <=200 chars; cite the signals that drove non-ALLOW",
  "confidence": 0.0-1.0,
  "next_action_hint": "string or null - short imperative",
  "alternative_tool": "string or null - prefer one of CANDIDATE ALTERNATIVES",
  "cited_anomalies": ["metric_name", ...],
  "cited_turns_rel": [-1, -2, ...],
  "recommended_advisors": [
    {
      "advisor": "<one of the 8 above>",
      "priority": "high|medium|low",
      "action": "one-sentence imperative",
      "reasoning": "short why, citing signals",
      "cited_signals": ["metric_name", ...]
    },
    ...
  ]
}

Rules:
* Recommend ONE advisor per domain at most. Multiple domains may fire
  simultaneously (cost + cache + security all at once is the canonical
  pattern this exists for).
* Every recommendation MUST cite at least one signal name from the
  COST / KV CACHE / SECURITY / ANOMALIES / TRAJECTORY sections.
* If no domain signals apply, recommended_advisors may be empty.
* Decide BLOCK only on clear malice or destructive scope.
* Decide REQUIRE_APPROVAL when alert-level anomalies cluster or the
  trajectory shows confused / repeated / errored patterns.
* Decide DEFER when the agent should clarify with the user.
* Otherwise ALLOW. Be conservative: when in doubt, REQUIRE_APPROVAL.
"""


_ADVISOR_HASH_INPUT = (
    _ADVISOR_PROMPT_VERSION + "\n" + _ADVISOR_MODEL + "\n" + ADVISOR_SYSTEM_PROMPT
)
ADVISOR_PROMPT_HASH = hashlib.sha3_256(
    _ADVISOR_HASH_INPUT.encode("utf-8")
).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Advisor protocol
# ──────────────────────────────────────────────────────────────────────


class Advisor(Protocol):
    """Common interface. Kwargs mirror :func:`compose_advice_heuristic`
    plus the extra context layers that an sLLM can leverage."""

    def advise(
        self,
        *,
        temporal_ctx: TemporalContext | None = None,
        anomalies: list[AnomalyTag] | None = None,
        baseline: BurnInBaseline | None = None,
        catalog: TrajectoryCatalog | None = None,
        intent_classifier: IntentClassifier | None = None,
        action_table: ActionTable | None = None,
        base_decision: Decision = "ALLOW",
        base_reason: str = "",
        current_tool: str = "",
        cost_signals: dict[str, Any] | None = None,
        cache_signals: dict[str, Any] | None = None,
        security_signals: dict[str, Any] | None = None,
        step_traces: dict[str, Any] | None = None,
    ) -> ActionAdvice: ...


# ──────────────────────────────────────────────────────────────────────
# DummyAdvisor — deterministic, zero-cost
# ──────────────────────────────────────────────────────────────────────


class DummyAdvisor:
    """Delegates to :func:`compose_advice_heuristic`. Used as the default
    and as the parse-failure fallback for :class:`HaikuAdvisor`. Passes
    cost / cache / security signal dicts through so the heuristic
    composer can map them to multi-domain recommendations."""

    def advise(
        self,
        *,
        temporal_ctx: TemporalContext | None = None,
        anomalies: list[AnomalyTag] | None = None,
        baseline: BurnInBaseline | None = None,
        catalog: TrajectoryCatalog | None = None,
        intent_classifier: IntentClassifier | None = None,
        action_table: ActionTable | None = None,
        base_decision: Decision = "ALLOW",
        base_reason: str = "",
        current_tool: str = "",
        cost_signals: dict[str, Any] | None = None,
        cache_signals: dict[str, Any] | None = None,
        security_signals: dict[str, Any] | None = None,
        step_traces: dict[str, Any] | None = None,
    ) -> ActionAdvice:
        return compose_advice_heuristic(
            temporal_ctx=temporal_ctx,
            anomalies=anomalies,
            base_decision=base_decision,
            base_reason=base_reason,
            current_tool=current_tool,
            cost_signals=cost_signals,
            cache_signals=cache_signals,
            security_signals=security_signals,
            step_traces=step_traces,
        )


# ──────────────────────────────────────────────────────────────────────
# HaikuAdvisor — Anthropic Haiku 4.5 backed
# ──────────────────────────────────────────────────────────────────────


def _build_user_message(
    *,
    temporal_ctx: TemporalContext | None,
    anomalies: list[AnomalyTag] | None,
    baseline: BurnInBaseline | None,
    catalog: TrajectoryCatalog | None,
    intent_classifier: IntentClassifier | None,
    action_table: ActionTable | None,
    base_decision: Decision,
    base_reason: str,
    current_tool: str,
    cost_signals: dict[str, Any] | None = None,
    cache_signals: dict[str, Any] | None = None,
    security_signals: dict[str, Any] | None = None,
) -> str:
    """Assemble the multi-layer narrative for the sLLM user message.
    Best-effort: a failure in any single layer's renderer skips that
    section rather than aborting the call."""
    sections: list[str] = []

    if temporal_ctx is not None:
        try:
            from aegis.atv.temporal import serialize_temporal

            narrative = serialize_temporal(
                temporal_ctx,
                baseline=baseline,
                catalog=catalog,
                intent_classifier=intent_classifier,
            )
            sections.append(narrative)
        except Exception:  # noqa: BLE001
            sections.append("(temporal context render failed)")
    else:
        sections.append("(no temporal context available)")

    # PR-ψ-multi-domain: cost / cache / security sections. Each renderer
    # returns "" on empty dict so the section is omitted cleanly.
    for renderer, payload in (
        (render_cost_signals, cost_signals or {}),
        (render_cache_signals, cache_signals or {}),
        (render_security_signals, security_signals or {}),
    ):
        try:
            text = renderer(payload)
            if text:
                sections.append(text)
        except Exception:  # noqa: BLE001
            pass

    if action_table is not None and current_tool:
        try:
            from aegis.burnin.action_embeddings import nearest_actions

            top = nearest_actions(current_tool, k=3, table=action_table)
            if top:
                lines = ["CANDIDATE ALTERNATIVES (semantic similarity):"]
                for name, sim in top:
                    lines.append(f"  {name:<14}  cos={sim:.2f}")
                sections.append("\n".join(lines))
        except Exception:  # noqa: BLE001
            pass

    sections.append(
        "PROPOSED CALL\n"
        f"  tool: {current_tool or '(unknown)'}\n"
        "BASE VERDICT (from firewall)\n"
        f"  decision: {base_decision}\n"
        f"  reason:   {base_reason or '(none)'}"
    )

    return "\n\n".join(sections)


_ALLOWED_DOMAIN_ADVISORS: frozenset[str] = frozenset({
    "cost-optimizer", "kv-cache-optimizer", "security-reviewer",
    "context-compactor", "test-runner", "loop-breaker",
    "permission-escalator", "human-clarifier",
})
_ALLOWED_PRIORITIES: frozenset[str] = frozenset({"high", "medium", "low"})


def _parse_recommended_advisors(
    raw: object,
) -> tuple[AdvisorRecommendation, ...]:
    """Best-effort parse of the model's ``recommended_advisors`` list.
    Items with an unknown advisor name or priority are silently dropped
    so a hallucinated advisor type can't poison the audit chain."""
    if not isinstance(raw, list):
        return ()
    out: list[AdvisorRecommendation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        advisor = item.get("advisor", "")
        priority = item.get("priority", "")
        if advisor not in _ALLOWED_DOMAIN_ADVISORS:
            continue
        if priority not in _ALLOWED_PRIORITIES:
            continue
        cited = item.get("cited_signals") or []
        out.append(AdvisorRecommendation(
            advisor=cast(DomainAdvisor, advisor),
            priority=cast(Priority, priority),
            action=str(item.get("action", ""))[:512],
            reasoning=str(item.get("reasoning", ""))[:512],
            cited_signals=tuple(
                str(x) for x in cited if isinstance(x, str)
            ),
        ))
    return tuple(out)


def _parse_advice_json(
    text: str,
    *,
    advisor_kind: AdvisorKind,
    advisor_hash: str,
) -> ActionAdvice | None:
    """Extract a valid :class:`ActionAdvice` from a Haiku response.
    Returns ``None`` when the response is unusable."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    decision_raw = data.get("decision", "")
    if decision_raw not in ("ALLOW", "BLOCK", "REQUIRE_APPROVAL", "DEFER"):
        return None

    cited_a = data.get("cited_anomalies") or []
    cited_t = data.get("cited_turns_rel") or []
    recs = _parse_recommended_advisors(
        data.get("recommended_advisors"),
    )

    hint = data.get("next_action_hint")
    alt = data.get("alternative_tool")
    return ActionAdvice(
        decision=cast(Decision, decision_raw),
        reason=str(data.get("reason", ""))[:512],
        confidence=float(data.get("confidence", 0.5)),
        next_action_hint=(str(hint) if hint else None),
        alternative_tool=(str(alt) if alt else None),
        cited_anomalies=tuple(
            str(x) for x in cited_a if isinstance(x, str)
        ),
        cited_turns_rel=tuple(
            int(x) for x in cited_t if isinstance(x, (int, float))
        ),
        recommended_advisors=recs,
        advisor_kind=advisor_kind,
        advisor_hash=advisor_hash,
        produced_at_ns=time.time_ns(),
    )


def _stamp_advisor_kind(
    base: ActionAdvice,
    *,
    advisor_kind: AdvisorKind,
    advisor_hash: str,
) -> ActionAdvice:
    """Re-stamp an advice's audit fields. Used so that fallback paths
    still attribute the call to the model that produced (or was supposed
    to produce) the advice."""
    return ActionAdvice(
        decision=base.decision,
        reason=base.reason,
        confidence=base.confidence,
        next_action_hint=base.next_action_hint,
        alternative_tool=base.alternative_tool,
        cited_anomalies=base.cited_anomalies,
        cited_turns_rel=base.cited_turns_rel,
        recommended_advisors=base.recommended_advisors,
        advisor_kind=advisor_kind,
        advisor_hash=advisor_hash,
        produced_at_ns=base.produced_at_ns,
    )


class HaikuAdvisor:
    """Anthropic Haiku 4.5 backed advisor. Falls back to the heuristic
    when the API call fails or returns garbled output, but always stamps
    ``advisor_kind="sllm-haiku"`` if the call was actually attempted."""

    advisor_kind: AdvisorKind = "sllm-haiku"

    def __init__(self) -> None:
        from anthropic import Anthropic

        from aegis.config import settings

        self.client = Anthropic()
        self.model = _ADVISOR_MODEL
        self.temperature = settings.aegis_judge_temperature
        self._fallback = DummyAdvisor()

    def advise(
        self,
        *,
        temporal_ctx: TemporalContext | None = None,
        anomalies: list[AnomalyTag] | None = None,
        baseline: BurnInBaseline | None = None,
        catalog: TrajectoryCatalog | None = None,
        intent_classifier: IntentClassifier | None = None,
        action_table: ActionTable | None = None,
        base_decision: Decision = "ALLOW",
        base_reason: str = "",
        current_tool: str = "",
        cost_signals: dict[str, Any] | None = None,
        cache_signals: dict[str, Any] | None = None,
        security_signals: dict[str, Any] | None = None,
        step_traces: dict[str, Any] | None = None,
    ) -> ActionAdvice:
        user_msg = _build_user_message(
            temporal_ctx=temporal_ctx,
            anomalies=anomalies,
            baseline=baseline,
            catalog=catalog,
            intent_classifier=intent_classifier,
            action_table=action_table,
            base_decision=base_decision,
            base_reason=base_reason,
            current_tool=current_tool,
            cost_signals=cost_signals,
            cache_signals=cache_signals,
            security_signals=security_signals,
        )

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=900,  # bumped for recommended_advisors list
                temperature=self.temperature,
                system=ADVISOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            block = resp.content[0]
            text = cast(str, getattr(block, "text", "")).strip()
        except Exception:  # noqa: BLE001
            # API unreachable / auth error / rate-limited — return the
            # plain heuristic advice (advisor_kind="heuristic" so audit
            # records the call DID NOT reach the model).
            return self._fallback.advise(
                temporal_ctx=temporal_ctx,
                anomalies=anomalies,
                base_decision=base_decision,
                base_reason=base_reason,
                current_tool=current_tool,
                cost_signals=cost_signals,
                cache_signals=cache_signals,
                security_signals=security_signals,
                step_traces=step_traces,
            )

        parsed = _parse_advice_json(
            text,
            advisor_kind=self.advisor_kind,
            advisor_hash=ADVISOR_PROMPT_HASH,
        )
        if parsed is not None:
            return parsed

        # Haiku responded but produced unusable output. Conservative
        # fallback: heuristic with REQUIRE_APPROVAL as the seed, stamped
        # as sllm-haiku so audit knows the call DID happen.
        fb = self._fallback.advise(
            temporal_ctx=temporal_ctx,
            anomalies=anomalies,
            base_decision="REQUIRE_APPROVAL",
            base_reason=f"sllm-haiku unparseable response: {text[:64]!r}",
            current_tool=current_tool,
            cost_signals=cost_signals,
            cache_signals=cache_signals,
            security_signals=security_signals,
        )
        return _stamp_advisor_kind(
            fb,
            advisor_kind=self.advisor_kind,
            advisor_hash=ADVISOR_PROMPT_HASH,
        )


# ──────────────────────────────────────────────────────────────────────
# Factory + public API
# ──────────────────────────────────────────────────────────────────────


def get_advisor() -> Advisor:
    """Return the advisor selected by ``AEGIS_ADVISOR_PROVIDER``.

    Falls back to :class:`DummyAdvisor` when the requested provider's
    requirements (API key for ``haiku``) aren't met. Mirrors the policy
    of :func:`aegis.judge.get_judge`.
    """
    provider = (
        os.environ.get("AEGIS_ADVISOR_PROVIDER", "dummy").strip().lower()
    )
    if provider == "haiku":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return DummyAdvisor()
        return HaikuAdvisor()
    # Unknown / empty provider → dummy. Don't raise: a typo in production
    # env shouldn't break the firewall path.
    return DummyAdvisor()


def compose_advice_sllm(
    *,
    temporal_ctx: TemporalContext | None = None,
    anomalies: list[AnomalyTag] | None = None,
    baseline: BurnInBaseline | None = None,
    catalog: TrajectoryCatalog | None = None,
    intent_classifier: IntentClassifier | None = None,
    action_table: ActionTable | None = None,
    base_decision: Decision = "ALLOW",
    base_reason: str = "",
    current_tool: str = "",
    cost_signals: dict[str, Any] | None = None,
    cache_signals: dict[str, Any] | None = None,
    security_signals: dict[str, Any] | None = None,
    step_traces: dict[str, Any] | None = None,
    advisor: Advisor | None = None,
) -> ActionAdvice:
    """Build an :class:`ActionAdvice` via the configured advisor backend.

    Drop-in superset of :func:`compose_advice_heuristic` — accepts the
    extra ``baseline`` / ``catalog`` / ``intent_classifier`` /
    ``action_table`` context that an sLLM can leverage, plus the v2.5.2
    ``cost_signals`` / ``cache_signals`` / ``security_signals`` dicts
    that drive multi-domain advisor recommendations. v2.7.1 adds
    ``step_traces`` so the heuristic loop-breaker can fire on a fresh
    session that has the firewall's step336 trace but no burn-in
    redundancy baseline yet. Pass ``advisor=`` to inject a specific
    advisor instance (useful for tests)."""
    chosen: Advisor = advisor if advisor is not None else get_advisor()
    return chosen.advise(
        temporal_ctx=temporal_ctx,
        anomalies=anomalies,
        baseline=baseline,
        catalog=catalog,
        intent_classifier=intent_classifier,
        action_table=action_table,
        base_decision=base_decision,
        base_reason=base_reason,
        current_tool=current_tool,
        cost_signals=cost_signals,
        cache_signals=cache_signals,
        security_signals=security_signals,
        step_traces=step_traces,
    )


__all__ = [
    "ADVISOR_PROMPT_HASH",
    "ADVISOR_SYSTEM_PROMPT",
    "Advisor",
    "DummyAdvisor",
    "HaikuAdvisor",
    "compose_advice_sllm",
    "get_advisor",
]
