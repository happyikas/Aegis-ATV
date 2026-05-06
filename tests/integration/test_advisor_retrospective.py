"""Tests for ``aegis.judge.retrospective`` — predicted-vs-actual
advisor comparison emitted by the PostToolUse hook
(PR-ψ-retrospective, v2.7).

Distinct from ``test_retrospective.py`` (Stop-hook session
retrospective from PR #46) — this module covers the per-tool
PreToolUse-vs-PostToolUse advisor accuracy comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.judge.retrospective import (
    RetrospectiveAdvice,
    evaluate_retrospective,
    find_pretool_record,
    render_retrospective,
    retrospective_from_dict,
    retrospective_to_dict,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _write_pretool(
    audit: Path,
    *,
    invocation_id: str,
    tool: str,
    decision: str,
    advisors: list[tuple[str, str]] | None = None,
) -> None:
    """Append a synthetic PreToolUse record. ``advisors`` is a list of
    ``(advisor, priority)`` pairs."""
    advisors = advisors or []
    rec = {
        "ts_ns": 1,
        "tool": tool,
        "aid": "sess-test",
        "invocation_id": invocation_id,
        "decision": decision,
        "trace_id": "t" * 32,
        "mode": "local",
        "explain": {
            "action_advice": {
                "decision": decision,
                "reason": "synthetic",
                "confidence": 0.8,
                "recommended_advisors": [
                    {
                        "advisor": a, "priority": p,
                        "action": "x", "reasoning": "y",
                        "cited_signals": [],
                    }
                    for a, p in advisors
                ],
                "advisor_kind": "heuristic",
                "advisor_hash": "h" * 64,
                "produced_at_ns": 1,
            }
        },
    }
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _write_pretool_no_advice(
    audit: Path, *, invocation_id: str, tool: str
) -> None:
    """Append a PreToolUse record with NO ``action_advice`` (gate
    skipped or advisor disabled)."""
    rec = {
        "ts_ns": 1,
        "tool": tool,
        "aid": "sess-test",
        "invocation_id": invocation_id,
        "decision": "ALLOW",
        "trace_id": "t" * 32,
        "mode": "local",
        "explain": {},
    }
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


# ──────────────────────────────────────────────────────────────────────
# find_pretool_record
# ──────────────────────────────────────────────────────────────────────


class TestFindPretool:
    def test_missing_audit_returns_none(self, tmp_path: Path) -> None:
        result = find_pretool_record(
            tmp_path / "no.jsonl", invocation_id="x",
        )
        assert result is None

    def test_empty_invocation_id_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text("{}\n")
        assert find_pretool_record(path, invocation_id="") is None

    def test_finds_matching_pretool_record(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_pretool(path, invocation_id="abc-123", tool="Bash",
                       decision="ALLOW")
        rec = find_pretool_record(path, invocation_id="abc-123")
        assert rec is not None
        assert rec["tool"] == "Bash"
        assert rec["decision"] == "ALLOW"

    def test_skips_posttool_records_with_same_id(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_pretool(path, invocation_id="abc", tool="Bash",
                       decision="REQUIRE_APPROVAL")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts_ns": 2, "tool": "Bash", "aid": "sess-test",
                "invocation_id": "abc", "hook": "PostToolUse",
                "status": "success", "mode": "local",
            }) + "\n")
        rec = find_pretool_record(path, invocation_id="abc")
        assert rec is not None
        assert rec.get("hook") != "PostToolUse"
        assert rec["decision"] == "REQUIRE_APPROVAL"

    def test_corrupt_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text("not json\n\n")
        _write_pretool(path, invocation_id="zzz", tool="Read",
                       decision="ALLOW")
        rec = find_pretool_record(path, invocation_id="zzz")
        assert rec is not None


# ──────────────────────────────────────────────────────────────────────
# evaluate_retrospective — core matrix
# ──────────────────────────────────────────────────────────────────────


class TestEvaluateRetrospective:
    def test_predicted_allow_actual_success_is_accurate(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(audit, invocation_id="i1", tool="Read",
                       decision="ALLOW")
        r = evaluate_retrospective(
            invocation_id="i1", tool_name="Read",
            actual_status="success", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "accurate"
        assert "ALLOW" in r.notes

    def test_predicted_allow_actual_failure_is_missed_signal(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(audit, invocation_id="i2", tool="Bash",
                       decision="ALLOW")
        r = evaluate_retrospective(
            invocation_id="i2", tool_name="Bash",
            actual_status="failure", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "missed_signal"
        assert "failure" in r.notes

    def test_predicted_allow_actual_timeout_is_missed_signal(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(audit, invocation_id="i3", tool="Bash",
                       decision="ALLOW")
        r = evaluate_retrospective(
            invocation_id="i3", tool_name="Bash",
            actual_status="timeout", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "missed_signal"

    def test_predicted_require_approval_high_priority_actual_success_is_false_alarm(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(
            audit, invocation_id="i4", tool="Bash",
            decision="REQUIRE_APPROVAL",
            advisors=[("cost-optimizer", "high"),
                      ("kv-cache-optimizer", "high")],
        )
        r = evaluate_retrospective(
            invocation_id="i4", tool_name="Bash",
            actual_status="success", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "false_alarm"

    def test_predicted_require_approval_low_priority_actual_success_is_accurate(
        self, tmp_path: Path
    ) -> None:
        """Low/medium advisory that turns out unnecessary is acceptable
        — only HIGH-priority over-fires count as false alarms."""
        audit = tmp_path / "audit.jsonl"
        _write_pretool(
            audit, invocation_id="i5", tool="Bash",
            decision="REQUIRE_APPROVAL",
            advisors=[("loop-breaker", "medium")],
        )
        r = evaluate_retrospective(
            invocation_id="i5", tool_name="Bash",
            actual_status="success", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "accurate"

    def test_predicted_require_approval_actual_failure_is_accurate(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(
            audit, invocation_id="i6", tool="Bash",
            decision="REQUIRE_APPROVAL",
            advisors=[("cost-optimizer", "high")],
        )
        r = evaluate_retrospective(
            invocation_id="i6", tool_name="Bash",
            actual_status="failure", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "accurate"

    def test_predicted_block_always_accurate(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(audit, invocation_id="i7", tool="Bash",
                       decision="BLOCK")
        r = evaluate_retrospective(
            invocation_id="i7", tool_name="Bash",
            actual_status="failure", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "accurate"

    def test_no_advice_is_not_applicable(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool_no_advice(
            audit, invocation_id="i8", tool="Read",
        )
        r = evaluate_retrospective(
            invocation_id="i8", tool_name="Read",
            actual_status="success", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "not_applicable"

    def test_no_pretool_record_yields_not_applicable(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        r = evaluate_retrospective(
            invocation_id="orphan", tool_name="Bash",
            actual_status="success", audit_path=audit,
        )
        assert r is not None
        assert r.accuracy == "not_applicable"
        assert r.predicted_decision == "<no advice>"

    def test_advisors_pulled_through_into_result(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_pretool(
            audit, invocation_id="i9", tool="Bash",
            decision="REQUIRE_APPROVAL",
            advisors=[
                ("cost-optimizer", "high"),
                ("security-reviewer", "high"),
            ],
        )
        r = evaluate_retrospective(
            invocation_id="i9", tool_name="Bash",
            actual_status="success", audit_path=audit,
        )
        assert r is not None
        assert r.predicted_advisors == (
            "cost-optimizer", "security-reviewer",
        )
        assert r.predicted_priorities == ("high", "high")


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


class TestJsonRoundTrip:
    def test_round_trip(self) -> None:
        original = RetrospectiveAdvice(
            invocation_id="x",
            tool_name="Bash",
            predicted_decision="ALLOW",
            predicted_advisors=("cost-optimizer",),
            predicted_priorities=("high",),
            actual_status="success",
            accuracy="accurate",
            notes="ok",
            produced_at_ns=42,
        )
        d = retrospective_to_dict(original)
        json.dumps(d)
        restored = retrospective_from_dict(d)
        assert restored == original

    def test_legacy_dict_without_fields_loads(self) -> None:
        restored = retrospective_from_dict(
            {"invocation_id": "y", "tool_name": "Read",
             "predicted_decision": "ALLOW", "actual_status": "success"}
        )
        assert restored.predicted_advisors == ()
        assert restored.accuracy == "not_applicable"


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_includes_accuracy_and_advisors(self) -> None:
        r = RetrospectiveAdvice(
            invocation_id="abc",
            tool_name="Bash",
            predicted_decision="REQUIRE_APPROVAL",
            predicted_advisors=("cost-optimizer", "kv-cache-optimizer"),
            predicted_priorities=("high", "high"),
            actual_status="success",
            accuracy="false_alarm",
            notes="predicted REQUIRE_APPROVAL with HIGH; tool succeeded",
        )
        out = render_retrospective(r)
        assert "false_alarm" in out
        assert "cost-optimizer" in out
        assert "kv-cache-optimizer" in out


@pytest.mark.parametrize(
    "predicted,actual,expected",
    [
        ("ALLOW", "success", "accurate"),
        ("ALLOW", "failure", "missed_signal"),
        ("ALLOW", "timeout", "missed_signal"),
        ("ALLOW", "partial", "accurate"),
        ("BLOCK", "success", "accurate"),
        ("BLOCK", "failure", "accurate"),
        ("DEFER", "failure", "accurate"),
    ],
)
def test_accuracy_matrix(
    tmp_path: Path, predicted: str, actual: str, expected: str
) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_pretool(audit, invocation_id="m", tool="Bash",
                   decision=predicted)
    r = evaluate_retrospective(
        invocation_id="m", tool_name="Bash",
        actual_status=actual,  # type: ignore[arg-type]
        audit_path=audit,
    )
    assert r is not None
    assert r.accuracy == expected
