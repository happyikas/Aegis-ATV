"""Tests for ``aegis.judge.action_advice`` — sLLM output schema (PR-ζ-schema)."""

from __future__ import annotations

import json

import pytest

from aegis.atv.temporal import ATVSnapshot, TemporalContext
from aegis.burnin.anomaly import AnomalyTag
from aegis.judge.action_advice import (
    ActionAdvice,
    ActionStep,
    AdvisorRecommendation,
    advice_from_dict,
    advice_to_audit_record,
    advice_to_dict,
    compose_advice_heuristic,
    render_advice,
)

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _mk_tag(
    metric: str, severity: str = "warning", z: float = 2.5,
) -> AnomalyTag:
    return AnomalyTag(
        metric=metric,
        severity=severity,
        observed=10.0,
        baseline_mean=1.0,
        baseline_std=1.0,
        z_score=z,
        description=f"{metric} = 10 ({z:.1f}σ above baseline)",
    )


def _mk_temporal(
    *,
    n_history: int = 5,
    n_backtracks: int = 0,
    n_redundant: int = 0,
    n_errors: int = 0,
    flagged_turns: list[int] | None = None,
) -> TemporalContext:
    """Build a TemporalContext with optional per-turn signals.

    ``flagged_turns`` lists turn_index_rel values that should carry
    a backtrack / redundant / is_error flag. Distributed round-robin.
    """
    flagged = set(flagged_turns or [])
    snaps: list[ATVSnapshot] = []
    for i in range(n_history):
        rel = i - (n_history - 1)
        snap = ATVSnapshot(
            turn_index_rel=rel, ts_ns=0,
            tool_name="Read", args_excerpt="",
            decision="ALLOW", outcome="success",
            backtrack=(rel in flagged),
            redundant=False,
            is_error=False,
        )
        snaps.append(snap)
    return TemporalContext(
        history=tuple(snaps),
        window_size=n_history,
        cumulative_token_trajectory=tuple(0 for _ in range(n_history)),
        cache_hit_rate_trajectory=tuple(0.0 for _ in range(n_history)),
        n_backtracks=n_backtracks, n_redundant=n_redundant,
        n_errors=n_errors, n_failures=0,
        cache_hit_rate_max_drop_pp=0.0,
        token_velocity_per_turn=0.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Read",),
    )


# ──────────────────────────────────────────────────────────────────────
# Dataclass invariants
# ──────────────────────────────────────────────────────────────────────


class TestSchema:
    def test_default_construction(self) -> None:
        a = ActionAdvice(decision="ALLOW", reason="ok", confidence=0.9)
        assert a.decision == "ALLOW"
        assert a.confidence == 0.9
        assert a.next_action_hint is None
        assert a.alternative_tool is None
        assert a.cited_anomalies == ()
        assert a.cited_turns_rel == ()
        assert a.advisor_kind == "heuristic"

    def test_confidence_clamped_to_unit_interval(self) -> None:
        # Frozen dataclass with __post_init__ clamping.
        too_high = ActionAdvice(
            decision="ALLOW", reason="x", confidence=1.7,
        )
        too_low = ActionAdvice(
            decision="ALLOW", reason="x", confidence=-0.4,
        )
        assert too_high.confidence == 1.0
        assert too_low.confidence == 0.0

    def test_frozen_dataclass(self) -> None:
        a = ActionAdvice(decision="ALLOW", reason="x", confidence=0.5)
        with pytest.raises((AttributeError, TypeError)):
            a.decision = "BLOCK"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────
# Heuristic composer
# ──────────────────────────────────────────────────────────────────────


class TestComposeHeuristic:
    def test_no_anomalies_passes_through(self) -> None:
        a = compose_advice_heuristic(
            base_decision="ALLOW", base_reason="firewall pass",
        )
        assert a.decision == "ALLOW"
        assert a.confidence == 0.90
        assert "no anomalies" in a.reason or "firewall pass" in a.reason

    def test_single_alert_escalates_to_require_approval(self) -> None:
        tags = [_mk_tag("window_cache_hit_rate_max_drop_pp", "alert", z=5.5)]
        a = compose_advice_heuristic(anomalies=tags)
        assert a.decision == "REQUIRE_APPROVAL"
        assert a.confidence == 0.85

    def test_two_warnings_escalate_to_require_approval(self) -> None:
        tags = [
            _mk_tag("a", "warning", 2.5),
            _mk_tag("b", "warning", 2.2),
        ]
        a = compose_advice_heuristic(anomalies=tags, base_decision="ALLOW")
        assert a.decision == "REQUIRE_APPROVAL"
        assert a.confidence == 0.75

    def test_single_warning_keeps_base_with_lower_confidence(
        self,
    ) -> None:
        tags = [_mk_tag("x", "warning", 2.0)]
        a = compose_advice_heuristic(
            anomalies=tags, base_decision="ALLOW",
        )
        assert a.decision == "ALLOW"
        assert a.confidence == 0.70

    def test_info_only_keeps_high_confidence(self) -> None:
        tags = [_mk_tag("x", "info", 1.2)]
        a = compose_advice_heuristic(
            anomalies=tags, base_decision="ALLOW",
        )
        assert a.decision == "ALLOW"
        assert a.confidence == 0.90

    def test_alternative_tool_for_edit_with_backtrack(self) -> None:
        tags = [_mk_tag("window_n_backtracks", "warning", 3.0)]
        a = compose_advice_heuristic(
            anomalies=tags, current_tool="Edit",
        )
        assert a.alternative_tool == "Read"

    def test_alternative_tool_for_bash_with_error(self) -> None:
        tags = [_mk_tag("window_n_errors", "warning", 2.0)]
        a = compose_advice_heuristic(
            anomalies=tags, current_tool="Bash",
        )
        assert a.alternative_tool == "Read"

    def test_alternative_tool_for_redundant_grep(self) -> None:
        tags = [_mk_tag("window_n_redundant", "warning", 2.0)]
        a = compose_advice_heuristic(
            anomalies=tags, current_tool="Grep",
        )
        assert a.alternative_tool == "Glob"

    def test_alternative_tool_none_for_unknown_tool(self) -> None:
        tags = [_mk_tag("window_n_backtracks", "warning", 3.0)]
        a = compose_advice_heuristic(
            anomalies=tags, current_tool="(unknown)",
        )
        assert a.alternative_tool is None

    def test_hint_for_backtrack(self) -> None:
        tags = [_mk_tag("window_n_backtracks", "alert", 3.0)]
        a = compose_advice_heuristic(anomalies=tags)
        assert a.next_action_hint is not None
        assert "confused" in a.next_action_hint or \
               "edit-revert" in a.next_action_hint

    def test_hint_for_error(self) -> None:
        tags = [_mk_tag("window_n_errors", "warning", 2.0)]
        a = compose_advice_heuristic(anomalies=tags)
        assert a.next_action_hint is not None
        assert "errored" in a.next_action_hint

    def test_hint_for_redundant(self) -> None:
        tags = [_mk_tag("window_n_redundant", "warning", 2.0)]
        a = compose_advice_heuristic(anomalies=tags)
        assert a.next_action_hint is not None
        assert (
            "repeated" in a.next_action_hint
            or "different tool" in a.next_action_hint
        )

    def test_hint_for_token_velocity(self) -> None:
        tags = [_mk_tag("window_token_velocity_per_turn", "alert", 4.0)]
        a = compose_advice_heuristic(anomalies=tags)
        assert a.next_action_hint is not None
        assert (
            "token usage" in a.next_action_hint
            or "summarising" in a.next_action_hint
        )

    def test_no_anomalies_no_hint(self) -> None:
        a = compose_advice_heuristic(anomalies=[])
        assert a.next_action_hint is None

    def test_cited_anomalies_dedup_and_sorted(self) -> None:
        tags = [
            _mk_tag("metric_b", "warning", 2.5),
            _mk_tag("metric_a", "alert", 4.0),
            _mk_tag("metric_b", "warning", 2.5),  # dup
        ]
        a = compose_advice_heuristic(anomalies=tags)
        assert a.cited_anomalies == ("metric_a", "metric_b")

    def test_cited_turns_only_those_with_signals(self) -> None:
        ctx = _mk_temporal(
            n_history=5, n_backtracks=1, flagged_turns=[-2],
        )
        tags = [_mk_tag("window_n_backtracks", "warning", 3.0)]
        a = compose_advice_heuristic(temporal_ctx=ctx, anomalies=tags)
        # Only the flagged turn (-2) is cited.
        assert a.cited_turns_rel == (-2,)

    def test_advisor_kind_hash_set(self) -> None:
        a = compose_advice_heuristic()
        assert a.advisor_kind == "heuristic"
        assert len(a.advisor_hash) == 64
        assert all(c in "0123456789abcdef" for c in a.advisor_hash)

    def test_produced_at_set(self) -> None:
        a = compose_advice_heuristic()
        assert a.produced_at_ns > 0


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_render_includes_decision(self) -> None:
        a = ActionAdvice(decision="BLOCK", reason="x", confidence=0.5)
        text = render_advice(a)
        assert "decision:    BLOCK" in text
        assert "ActionAdvice" in text

    def test_render_omits_unset_fields(self) -> None:
        a = ActionAdvice(decision="ALLOW", reason="x", confidence=0.9)
        text = render_advice(a)
        assert "alt_tool" not in text
        assert "hint:" not in text

    def test_render_includes_citations(self) -> None:
        a = ActionAdvice(
            decision="REQUIRE_APPROVAL", reason="x", confidence=0.7,
            cited_anomalies=("m1", "m2"),
            cited_turns_rel=(-2, -1),
        )
        text = render_advice(a)
        assert "m1" in text and "m2" in text
        assert "-2" in text


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


class TestJSONRoundTrip:
    def test_to_dict_serialisable(self) -> None:
        a = compose_advice_heuristic(
            anomalies=[_mk_tag("x", "alert", 3.5)],
        )
        d = advice_to_dict(a)
        # Lists, not tuples, in the dict form.
        assert isinstance(d["cited_anomalies"], list)
        assert isinstance(d["cited_turns_rel"], list)
        # JSON-serialisable.
        json.dumps(d)

    def test_round_trip(self) -> None:
        a = ActionAdvice(
            decision="REQUIRE_APPROVAL",
            reason="alert: backtrack pattern",
            confidence=0.85,
            next_action_hint="ask user to clarify",
            alternative_tool="Read",
            cited_anomalies=("window_n_backtracks",),
            cited_turns_rel=(-1,),
            advisor_kind="heuristic",
            advisor_hash="abc",
            produced_at_ns=12345,
        )
        loaded = advice_from_dict(advice_to_dict(a))
        assert loaded == a

    def test_from_dict_tolerates_missing_fields(self) -> None:
        # Older audit record missing some fields → loader fills.
        d = {"decision": "ALLOW", "reason": "x", "confidence": 0.5}
        a = advice_from_dict(d)
        assert a.decision == "ALLOW"
        assert a.cited_anomalies == ()
        assert a.advisor_kind == "heuristic"


# ──────────────────────────────────────────────────────────────────────
# Audit record shape
# ──────────────────────────────────────────────────────────────────────


class TestAuditRecord:
    def test_record_shape_matches_aegis_convention(self) -> None:
        a = compose_advice_heuristic(
            anomalies=[_mk_tag("x", "warning", 2.5)],
        )
        rec = advice_to_audit_record(a, aid="sess-1", tool="Bash")
        # Same shape as PR #45 / #46 / #47 records:
        # ts_ns, tool, aid, hook, mode, decision, reason, explain.
        for field_name in (
            "ts_ns", "tool", "aid", "hook", "mode",
            "decision", "reason", "explain",
        ):
            assert field_name in rec
        assert rec["hook"] == "ActionAdvice"
        assert rec["aid"] == "sess-1"
        assert rec["tool"] == "Bash"
        # explain.action_advice is the round-trippable dict.
        ad = rec["explain"]["action_advice"]
        assert ad["decision"] == a.decision

    def test_record_json_serialisable(self) -> None:
        a = compose_advice_heuristic(
            anomalies=[_mk_tag("x", "alert", 4.0)],
        )
        rec = advice_to_audit_record(a, aid="sess-1", tool="Bash")
        json.dumps(rec)


# ──────────────────────────────────────────────────────────────────────
# End-to-end: temporal + anomalies → advice
# ──────────────────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_typical_stuck_pattern_yields_advice(self) -> None:
        ctx = _mk_temporal(
            n_history=5,
            n_backtracks=1,
            n_errors=1,
            flagged_turns=[-2],
        )
        tags = [
            _mk_tag("window_n_backtracks", "alert", 3.0),
            _mk_tag("window_n_errors", "warning", 2.0),
        ]
        advice = compose_advice_heuristic(
            temporal_ctx=ctx, anomalies=tags,
            base_decision="ALLOW", base_reason="firewall pass",
            current_tool="Edit",
        )
        assert advice.decision == "REQUIRE_APPROVAL"
        assert advice.alternative_tool == "Read"
        assert advice.next_action_hint is not None
        assert "backtrack" in " ".join(advice.cited_anomalies).lower() \
            or "n_backtracks" in advice.cited_anomalies
        assert advice.cited_turns_rel == (-2,)


# ──────────────────────────────────────────────────────────────────────
# v2.5.2 PR-ψ-multi-domain — recommended_advisors
# ──────────────────────────────────────────────────────────────────────


class TestRecommendationSchema:
    def test_constructs_with_required_fields(self) -> None:
        r = AdvisorRecommendation(
            advisor="cost-optimizer",
            priority="high",
            action="trim context",
        )
        assert r.advisor == "cost-optimizer"
        assert r.priority == "high"
        assert r.cited_signals == ()

    def test_frozen(self) -> None:
        r = AdvisorRecommendation(
            advisor="cost-optimizer", priority="high", action="x",
        )
        with pytest.raises((AttributeError, TypeError)):
            r.priority = "low"  # type: ignore[misc]


class TestHeuristicMultiDomainMapping:
    def test_destructive_path_match_emits_security_high(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="BLOCK", base_reason="rule:git_destructive",
            current_tool="Bash",
            security_signals={
                "verdict_decision": "BLOCK",
                "destructive_path_match": True,
                "policy_rule": "rule:git_destructive",
                "blast_radius": "high",
            },
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "security-reviewer" in names
        sec = next(r for r in advice.recommended_advisors
                   if r.advisor == "security-reviewer")
        assert sec.priority == "high"
        assert "destructive_path_match" in sec.cited_signals

    def test_cost_divergence_above_threshold_emits_cost_high(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="REQUIRE_APPROVAL",
            current_tool="Bash",
            cost_signals={"hw_vs_sw_divergence_ratio": 3.15},
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "cost-optimizer" in names
        co = next(r for r in advice.recommended_advisors
                  if r.advisor == "cost-optimizer")
        assert co.priority == "high"

    def test_cache_drop_above_30pp_emits_cache_high(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="ALLOW",
            cache_signals={
                "cache_hit_rate_max_drop_pp": 51.0,
                "prefix_re_keys_in_window": 4,
            },
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "kv-cache-optimizer" in names

    def test_three_domains_simultaneously(self) -> None:
        """The user's canonical example: cost +30%, KV cache 저하,
        백업 파일 삭제 → 3 advisors at once."""
        advice = compose_advice_heuristic(
            base_decision="REQUIRE_APPROVAL", current_tool="Bash",
            cost_signals={"hw_vs_sw_divergence_ratio": 3.0},
            cache_signals={"cache_hit_rate_max_drop_pp": 51.0},
            security_signals={
                "verdict_decision": "REQUIRE_APPROVAL",
                "destructive_path_match": True,
                "policy_rule": "rule:backup_path_destructive",
                "blast_radius": "high",
            },
        )
        names = {r.advisor for r in advice.recommended_advisors}
        assert {"cost-optimizer", "kv-cache-optimizer", "security-reviewer"} <= names

    def test_no_signals_no_recommendations(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="ALLOW", current_tool="Read",
        )
        assert advice.recommended_advisors == ()

    def test_loop_breaker_fires_from_step336_trace(self) -> None:
        """v2.7.1 — when burn-in baseline is unavailable but the
        firewall's step336 detector flagged a loop, the heuristic
        should still emit a `loop-breaker` recommendation."""
        advice = compose_advice_heuristic(
            base_decision="REQUIRE_APPROVAL",
            current_tool="Bash",
            step_traces={
                "aegis.firewall.step336_loop.run":
                    "step336: loop (3× seen) — Bash",
            },
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "loop-breaker" in names
        lb = next(r for r in advice.recommended_advisors
                  if r.advisor == "loop-breaker")
        assert lb.priority == "high"
        assert "step336_loop_detector" in lb.cited_signals

    def test_loop_breaker_fires_from_step336_redundant_trace(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="ALLOW",
            current_tool="Read",
            step_traces={
                "aegis.firewall.step336_loop.run":
                    "step336: redundant read-only (2× seen)",
            },
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "loop-breaker" in names

    def test_loop_breaker_does_not_fire_on_fresh_call(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="ALLOW",
            current_tool="Read",
            step_traces={
                "aegis.firewall.step336_loop.run": "step336: fresh call",
            },
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert "loop-breaker" not in names

    def test_default_escalation_when_block_without_domain_signal(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="BLOCK", base_reason="(no domain match)",
            security_signals={"verdict_decision": "BLOCK"},
        )
        names = [r.advisor for r in advice.recommended_advisors]
        assert names == ["permission-escalator"]


class TestRecommendationJsonRoundtrip:
    def test_round_trip_preserves_recommendations(self) -> None:
        advice = compose_advice_heuristic(
            base_decision="REQUIRE_APPROVAL",
            cost_signals={"hw_vs_sw_divergence_ratio": 3.0},
            security_signals={
                "verdict_decision": "REQUIRE_APPROVAL",
                "destructive_path_match": True,
                "policy_rule": "rule:git_destructive",
                "blast_radius": "high",
            },
        )
        d = advice_to_dict(advice)
        json.dumps(d)  # serialisable
        restored = advice_from_dict(d)
        assert len(restored.recommended_advisors) == len(advice.recommended_advisors)
        for orig, back in zip(
            advice.recommended_advisors,
            restored.recommended_advisors,
            strict=True,
        ):
            assert orig.advisor == back.advisor
            assert orig.priority == back.priority
            assert orig.action == back.action
            assert orig.cited_signals == back.cited_signals

    def test_unknown_advisor_dropped_on_load(self) -> None:
        # Older / hallucinated advisor name — must not survive parse.
        d = {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "recommended_advisors": [
                {"advisor": "cost-optimizer", "priority": "high",
                 "action": "x"},
                {"advisor": "made-up-advisor", "priority": "high",
                 "action": "y"},
                {"advisor": "kv-cache-optimizer", "priority": "bogus",
                 "action": "z"},
            ],
        }
        restored = advice_from_dict(d)
        names = [r.advisor for r in restored.recommended_advisors]
        assert names == ["cost-optimizer"]

    def test_legacy_advice_without_recommendations_loads(self) -> None:
        # Pre-v2.5.2 audit records have no ``recommended_advisors`` key.
        d = {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "advisor_kind": "heuristic",
        }
        restored = advice_from_dict(d)
        assert restored.recommended_advisors == ()


# ──────────────────────────────────────────────────────────────────────
# v2.8 PR-α — ActionStep schema
# ──────────────────────────────────────────────────────────────────────


class TestActionStepSchema:
    def test_constructs_with_required_fields(self) -> None:
        s = ActionStep(
            verb="prune-turns",
            parameters={
                "turn_indices_rel": [-3, -2, -1],
                "saved_tokens_estimate": 4500,
            },
        )
        assert s.verb == "prune-turns"
        assert s.parameters["turn_indices_rel"] == [-3, -2, -1]
        assert s.cited_signals == ()
        assert s.confidence == 0.5  # default

    def test_frozen(self) -> None:
        s = ActionStep(verb="end-session", parameters={})
        with pytest.raises((AttributeError, TypeError)):
            s.verb = "swap-tool"  # type: ignore[misc]

    def test_confidence_clamped_to_unit_interval(self) -> None:
        too_high = ActionStep(
            verb="end-session", parameters={}, confidence=1.7,
        )
        too_low = ActionStep(
            verb="end-session", parameters={}, confidence=-0.4,
        )
        assert too_high.confidence == 1.0
        assert too_low.confidence == 0.0


class TestActionStepRoundTrip:
    def test_round_trip_preserves_step(self) -> None:
        original = AdvisorRecommendation(
            advisor="cost-optimizer",
            priority="high",
            action="trim",
            action_steps=(
                ActionStep(
                    verb="prune-turns",
                    parameters={"turn_indices_rel": [-3, -2, -1],
                                "saved_tokens_estimate": 4500},
                    expected_impact="ratio 1.50 → 1.18",
                    confidence=0.85,
                    cited_signals=("budget_used_ratio",),
                ),
                ActionStep(
                    verb="swap-model",
                    parameters={"from_model": "claude-opus-4-7",
                                "to_model": "claude-haiku-4-5"},
                    expected_impact="3x cheaper",
                    confidence=0.6,
                ),
            ),
        )
        advice = ActionAdvice(
            decision="REQUIRE_APPROVAL", reason="x", confidence=0.8,
            recommended_advisors=(original,),
        )
        d = advice_to_dict(advice)
        json.dumps(d)  # serializable

        restored = advice_from_dict(d)
        assert len(restored.recommended_advisors) == 1
        rec = restored.recommended_advisors[0]
        assert len(rec.action_steps) == 2
        assert rec.action_steps[0].verb == "prune-turns"
        assert rec.action_steps[0].parameters["turn_indices_rel"] == [-3, -2, -1]
        assert rec.action_steps[0].confidence == 0.85
        assert rec.action_steps[1].verb == "swap-model"


class TestActionStepDefensiveParse:
    def test_unknown_verb_dropped(self) -> None:
        d = {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "high",
                "action": "x",
                "action_steps": [
                    {"verb": "do-magic-fix", "parameters": {}},
                    {"verb": "end-session", "parameters": {}},
                ],
            }],
        }
        restored = advice_from_dict(d)
        steps = restored.recommended_advisors[0].action_steps
        verbs = [s.verb for s in steps]
        # "do-magic-fix" silently dropped; "end-session" survives
        assert verbs == ["end-session"]

    def test_missing_required_param_drops_step(self) -> None:
        d = {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "high",
                "action": "x",
                "action_steps": [
                    # swap-model requires from_model + to_model
                    {"verb": "swap-model",
                     "parameters": {"from_model": "opus"}},
                    # complete one
                    {"verb": "swap-model",
                     "parameters": {"from_model": "opus",
                                    "to_model": "haiku"}},
                ],
            }],
        }
        restored = advice_from_dict(d)
        steps = restored.recommended_advisors[0].action_steps
        assert len(steps) == 1
        assert steps[0].parameters["to_model"] == "haiku"

    def test_non_dict_parameters_drops_step(self) -> None:
        d = {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "high",
                "action": "x",
                "action_steps": [
                    {"verb": "end-session", "parameters": "not a dict"},
                    {"verb": "end-session", "parameters": {}},
                ],
            }],
        }
        restored = advice_from_dict(d)
        assert len(restored.recommended_advisors[0].action_steps) == 1

    def test_legacy_recommendation_without_steps_loads(self) -> None:
        # Pre-v2.8 audit records have no ``action_steps`` key.
        d = {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "high",
                "action": "trim",
            }],
        }
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_all_eleven_verbs_accepted(self) -> None:
        """Sanity: every verb in the closed catalog round-trips."""
        verbs_with_params = [
            ("prune-turns", {"turn_indices_rel": [-1]}),
            ("summarize-window", {"turn_range": [-5, -1]}),
            ("swap-model", {"from_model": "a", "to_model": "b"}),
            ("end-session", {}),
            ("swap-tool", {"from_tool": "Read", "to_tool": "Grep"}),
            ("narrow-scope", {"original_args": "*", "suggested_args": "src/"}),
            ("clarify-intent", {"clarifying_question": "X or Y?"}),
            ("run-diagnostic", {"diagnostic_command": "pytest"}),
            ("verify-state", {"check": "file exists"}),
            ("notify-operator", {"channel": "#ops", "summary": "alert"}),
            ("require-approval", {"reason": "destructive"}),
        ]
        for verb, params in verbs_with_params:
            d = {
                "decision": "ALLOW", "reason": "x", "confidence": 0.5,
                "recommended_advisors": [{
                    "advisor": "cost-optimizer", "priority": "high",
                    "action": "x",
                    "action_steps": [{"verb": verb, "parameters": params}],
                }],
            }
            restored = advice_from_dict(d)
            steps = restored.recommended_advisors[0].action_steps
            assert len(steps) == 1, f"{verb} dropped — params={params}"
            assert steps[0].verb == verb


class TestVerbParamValidation:
    """v2.8 PR-γ — per-verb parameter shape validation drops steps
    with structurally invalid params even if required keys are present."""

    def _step_dict(self, verb: str, params: dict) -> dict:
        return {
            "decision": "ALLOW", "reason": "x", "confidence": 0.5,
            "recommended_advisors": [{
                "advisor": "cost-optimizer", "priority": "high",
                "action": "x",
                "action_steps": [
                    {"verb": verb, "parameters": params},
                ],
            }],
        }

    def test_prune_turns_rejects_positive_index(self) -> None:
        # turn_indices_rel must be all <= 0
        d = self._step_dict("prune-turns", {"turn_indices_rel": [-1, 2, -3]})
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_prune_turns_rejects_empty_list(self) -> None:
        # empty prune is meaningless
        d = self._step_dict("prune-turns", {"turn_indices_rel": []})
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_prune_turns_accepts_valid_indices(self) -> None:
        d = self._step_dict("prune-turns",
                            {"turn_indices_rel": [-3, -2, -1]})
        restored = advice_from_dict(d)
        assert len(restored.recommended_advisors[0].action_steps) == 1

    def test_prune_turns_rejects_negative_savings(self) -> None:
        d = self._step_dict("prune-turns", {
            "turn_indices_rel": [-1],
            "saved_tokens_estimate": -100,
        })
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_summarize_window_rejects_inverted_range(self) -> None:
        # start > end
        d = self._step_dict("summarize-window",
                            {"turn_range": [-1, -5]})
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_summarize_window_rejects_positive_in_range(self) -> None:
        d = self._step_dict("summarize-window",
                            {"turn_range": [-5, 1]})
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_summarize_window_accepts_valid(self) -> None:
        d = self._step_dict("summarize-window",
                            {"turn_range": [-5, -2]})
        restored = advice_from_dict(d)
        assert len(restored.recommended_advisors[0].action_steps) == 1

    def test_swap_model_rejects_empty_model_names(self) -> None:
        d = self._step_dict("swap-model",
                            {"from_model": "", "to_model": "haiku"})
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_swap_model_rejects_negative_ratio(self) -> None:
        d = self._step_dict("swap-model", {
            "from_model": "opus", "to_model": "haiku",
            "ratio_savings": -1.0,
        })
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_clarify_intent_rejects_empty_question(self) -> None:
        d = self._step_dict("clarify-intent",
                            {"clarifying_question": "   "})
        restored = advice_from_dict(d)
        assert restored.recommended_advisors[0].action_steps == ()

    def test_extra_keys_pass_through(self) -> None:
        """Forward compat: validators only check known keys; extra
        keys (e.g. ``saved_dollars_estimate``) pass through unchanged."""
        d = self._step_dict("prune-turns", {
            "turn_indices_rel": [-2, -1],
            "saved_dollars_estimate": 0.42,
            "future_field_we_havent_invented_yet": {"foo": "bar"},
        })
        restored = advice_from_dict(d)
        steps = restored.recommended_advisors[0].action_steps
        assert len(steps) == 1
        assert (
            steps[0].parameters["future_field_we_havent_invented_yet"]
            == {"foo": "bar"}
        )


class TestRenderActionSteps:
    def test_render_includes_steps(self) -> None:
        advice = ActionAdvice(
            decision="REQUIRE_APPROVAL", reason="x", confidence=0.8,
            recommended_advisors=(
                AdvisorRecommendation(
                    advisor="cost-optimizer", priority="high",
                    action="trim",
                    action_steps=(
                        ActionStep(
                            verb="prune-turns",
                            parameters={"turn_indices_rel": [-3, -2, -1]},
                            expected_impact="saves $0.42",
                            confidence=0.85,
                            cited_signals=("budget_used_ratio",),
                        ),
                    ),
                ),
            ),
        )
        out = render_advice(advice)
        assert "steps:" in out
        assert "prune-turns" in out
        assert "saves $0.42" in out
        assert "0.85" in out
