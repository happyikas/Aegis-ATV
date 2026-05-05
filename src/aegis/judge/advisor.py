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
from typing import TYPE_CHECKING, Protocol, cast

from aegis.judge.action_advice import (
    ActionAdvice,
    AdvisorKind,
    Decision,
    compose_advice_heuristic,
)

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext
    from aegis.burnin.action_embeddings import ActionTable
    from aegis.burnin.anomaly import AnomalyTag, BurnInBaseline
    from aegis.burnin.intent_classifier import IntentClassifier
    from aegis.burnin.trajectory_catalog import TrajectoryCatalog


# Bump on any prompt / model change so audit can pin advices to a
# specific revision.
_ADVISOR_PROMPT_VERSION = "advisor_v1"
_ADVISOR_MODEL = "claude-haiku-4-5-20251001"


ADVISOR_SYSTEM_PROMPT = """\
You are a deterministic security advisor for AI agent tool calls.
You are given a CCTV-style narrative of the agent's recent activity built
from these layers:

  TEMPORAL TRAJECTORY      — last N tool calls with outcome / cache / token info
  ANOMALIES vs BURN-IN     — z-score deviations from the agent's trained baseline
  NEAREST BURN-IN CLUSTERS — semantically nearest k-means trajectory archetypes
  TASK INTENT PREDICTION   — softmax over {debug, explore, edit, test, refactor,
                              review, create, general}
  CANDIDATE ALTERNATIVES   — tools semantically similar to the proposed call

Plus:
  PROPOSED CALL  — the tool the firewall is about to gate
  BASE VERDICT   — what the deterministic firewall already decided

Respond with ONLY a JSON object, no prose, with this exact shape:
{
  "decision": "ALLOW|BLOCK|REQUIRE_APPROVAL|DEFER",
  "reason": "string, <=200 chars; reference cited anomalies/turns when non-ALLOW",
  "confidence": 0.0-1.0,
  "next_action_hint": "string or null - short imperative for what to do next",
  "alternative_tool": "string or null - prefer one of CANDIDATE ALTERNATIVES",
  "cited_anomalies": ["metric_name", ...],
  "cited_turns_rel": [-1, -2, ...]
}

Decide BLOCK only on clear malice or destructive scope.
Decide REQUIRE_APPROVAL when alert-level anomalies cluster, or the trajectory
shows confused / repeated / errored patterns.
Decide DEFER when the agent should clarify with the user before continuing.
Otherwise ALLOW. Be conservative: when in doubt, REQUIRE_APPROVAL.
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
    ) -> ActionAdvice: ...


# ──────────────────────────────────────────────────────────────────────
# DummyAdvisor — deterministic, zero-cost
# ──────────────────────────────────────────────────────────────────────


class DummyAdvisor:
    """Delegates to :func:`compose_advice_heuristic`. Used as the default
    and as the parse-failure fallback for :class:`HaikuAdvisor`."""

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
    ) -> ActionAdvice:
        return compose_advice_heuristic(
            temporal_ctx=temporal_ctx,
            anomalies=anomalies,
            base_decision=base_decision,
            base_reason=base_reason,
            current_tool=current_tool,
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
) -> str:
    """Assemble the 4-layer narrative + alternative-tool section + proposed
    call for the user message. Best-effort: if any layer's renderer fails,
    that section is skipped rather than aborting the call."""
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
        )

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=600,
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
    advisor: Advisor | None = None,
) -> ActionAdvice:
    """Build an :class:`ActionAdvice` via the configured advisor backend.

    Drop-in superset of :func:`compose_advice_heuristic` — accepts the
    extra ``baseline`` / ``catalog`` / ``intent_classifier`` /
    ``action_table`` context that an sLLM can leverage but the heuristic
    ignores. Pass ``advisor=`` to inject a specific advisor instance
    (useful for tests)."""
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
