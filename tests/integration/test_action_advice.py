"""Tests for ``aegis.judge.action_advice`` — sLLM output schema (PR-ζ-schema)."""

from __future__ import annotations

import json

import pytest

from aegis.atv.temporal import ATVSnapshot, TemporalContext
from aegis.burnin.anomaly import AnomalyTag
from aegis.judge.action_advice import (
    ActionAdvice,
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
