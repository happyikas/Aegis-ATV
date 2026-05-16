"""Tests for v0.5.14 — explicit-deny CLI + doctor postmortem.

Closes the negative-feedback loop the v0.5.12 Bayesian backbone
left open. The deny path:

1. ``append_denial`` writes one JSONL line to a configurable path.
2. ``load_denial_trace_ids`` reads the file defensively (missing /
   malformed / empty → empty set, never raises).
3. ``classify_record`` returns ``EXPLICIT_DENY`` for any record
   whose trace_id is in the denial set, overriding both clean
   readings AND the in-record step_traces stamp.
4. ``learn_with_diagnostics`` propagates the denials into the
   posterior so denied patterns get ``β += 10`` per match.

The doctor path:

5. ``autonomy_stats`` counts bypass + explore + outlier records
   from ContextMemory.
6. ``render_doctor_report`` includes the autonomy section between
   security and next-actions, with a markdown outlier table.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aegis.autonomy.denials import (
    DenialRecord,
    append_denial,
    load_denial_trace_ids,
    load_denials,
)
from aegis.autonomy.learner import learn_with_diagnostics
from aegis.autonomy.reward import RewardEvent, classify_record
from aegis.context_memory.record import ContextMemoryRecord
from aegis.context_memory.report import autonomy_stats, render_doctor_report

# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


def _mk_record(
    *,
    decision: str = "REQUIRE_APPROVAL",
    reason: str = "same Bash call repeated 3 times this session",
    tool: str = "Bash",
    aid: str = "agent-A",
    trace_id: str = "trace-001",
    ts_ns: int | None = None,
    step_traces: dict[str, str] | None = None,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns if ts_ns is not None else time.time_ns(),
        trace_id=trace_id,
        invocation_id="inv-001",
        aid=aid,
        tenant_id="t",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=1.0,
        cost_usd=0.0,
        tokens_in=0,
        tokens_out=0,
        step_traces=step_traces or {},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="sidecar",
    )


# ──────────────────────────────────────────────────────────────────
# Denials file — append + load
# ──────────────────────────────────────────────────────────────────


class TestDenialsFile:
    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deny.jsonl"
        rec = append_denial("trace-abc", note="bad call", path=target)
        assert target.exists()
        assert rec.trace_id == "trace-abc"
        assert rec.note == "bad call"

    def test_append_is_jsonl(self, tmp_path: Path) -> None:
        target = tmp_path / "deny.jsonl"
        append_denial("trace-1", path=target)
        append_denial("trace-2", note="oops", path=target)
        lines = target.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        for ln in lines:
            payload = json.loads(ln)
            assert "trace_id" in payload
            assert "ts_ns" in payload

    def test_append_rejects_empty_trace_id(self, tmp_path: Path) -> None:
        target = tmp_path / "deny.jsonl"
        with pytest.raises(ValueError):
            append_denial("", path=target)

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "absent.jsonl"
        assert load_denials(path=target) == []
        assert load_denial_trace_ids(path=target) == frozenset()

    def test_load_skips_malformed_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "deny.jsonl"
        target.write_text(
            '{"trace_id": "good", "ts_ns": 1}\n'
            "not json at all\n"
            '{"trace_id": "", "ts_ns": 2}\n'
            '{"trace_id": "also-good", "ts_ns": 3}\n',
            encoding="utf-8",
        )
        ids = load_denial_trace_ids(path=target)
        assert ids == frozenset({"good", "also-good"})

    def test_load_returns_dataclass_records(self, tmp_path: Path) -> None:
        target = tmp_path / "deny.jsonl"
        append_denial("trace-1", note="reason", path=target)
        records = load_denials(path=target)
        assert len(records) == 1
        assert isinstance(records[0], DenialRecord)
        assert records[0].trace_id == "trace-1"
        assert records[0].note == "reason"


# ──────────────────────────────────────────────────────────────────
# classify_record honours denied set
# ──────────────────────────────────────────────────────────────────


class TestClassifyHonoursDenials:
    def test_denied_trace_returns_explicit_deny(self) -> None:
        rec = _mk_record(trace_id="trace-bad")
        ev = classify_record(
            rec,
            denied_trace_ids=frozenset({"trace-bad"}),
        )
        assert ev is RewardEvent.EXPLICIT_DENY

    def test_not_denied_returns_clean(self) -> None:
        rec = _mk_record(trace_id="trace-ok")
        ev = classify_record(
            rec,
            denied_trace_ids=frozenset({"trace-bad"}),
        )
        assert ev is RewardEvent.CLEAN

    def test_denied_beats_block_followup(self) -> None:
        """If a record is denied AND followed by a BLOCK, we want
        EXPLICIT_DENY (the stronger signal, weight 10) not
        BLOCK_FOLLOWUP (weight 3)."""
        rec = _mk_record(trace_id="trace-bad")
        follow = _mk_record(decision="BLOCK", trace_id="trace-block")
        ev = classify_record(
            rec,
            block_within=[follow],
            denied_trace_ids=frozenset({"trace-bad"}),
        )
        assert ev is RewardEvent.EXPLICIT_DENY


# ──────────────────────────────────────────────────────────────────
# learn_with_diagnostics propagates denials into the posterior
# ──────────────────────────────────────────────────────────────────


class TestLearnerHonoursDenials:
    def test_explicit_deny_drops_pattern_trust(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Build a pattern that WOULD qualify on cleans alone; add
        a denial for one record; verify the posterior LCB drops
        below the bypass threshold."""
        denials_file = tmp_path / "deny.jsonl"
        monkeypatch.setenv("AEGIS_AUTONOMY_DENIALS", str(denials_file))

        # 8 cleans of the loop:Bash pattern.
        recs = []
        base_ts = time.time_ns() - 1_000_000_000
        for i in range(8):
            recs.append(_mk_record(
                trace_id=f"loop-{i:04d}",
                aid=f"agent-{i % 3}",
                ts_ns=base_ts + i,
            ))

        # Baseline: no denials → pattern admitted with high LCB.
        result_clean = learn_with_diagnostics(recs, min_samples=5)
        assert ("Bash", "loop:Bash") in result_clean.trust_table
        baseline_lcb = result_clean.trust_table[("Bash", "loop:Bash")].trust_score

        # Now deny one record and re-learn (passing the set
        # directly, equivalent to what the on-disk loader does).
        denied = frozenset({"loop-0000"})
        result_denied = learn_with_diagnostics(
            recs, min_samples=5, denied_trace_ids=denied,
        )
        # One deny costs ten cleans → posterior drops sharply.
        if ("Bash", "loop:Bash") in result_denied.trust_table:
            new_lcb = result_denied.trust_table[("Bash", "loop:Bash")].trust_score
            assert new_lcb < baseline_lcb - 0.05, (
                f"single deny barely moved LCB: {baseline_lcb:.3f} → "
                f"{new_lcb:.3f}"
            )
        # else: dropped entirely, which is also a valid (stronger) outcome.

    def test_denials_loaded_from_env_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The learner pulls denials from the env-configured path
        when no explicit set is passed."""
        denials_file = tmp_path / "env-deny.jsonl"
        append_denial("loop-0000", path=denials_file)
        monkeypatch.setenv("AEGIS_AUTONOMY_DENIALS", str(denials_file))

        base_ts = time.time_ns() - 1_000_000_000
        recs = [
            _mk_record(
                trace_id=f"loop-{i:04d}",
                aid=f"agent-{i % 3}",
                ts_ns=base_ts + i,
            )
            for i in range(8)
        ]

        # No denied_trace_ids kwarg → learner reads the env file.
        result = learn_with_diagnostics(recs, min_samples=5)
        if ("Bash", "loop:Bash") in result.trust_table:
            p = result.trust_table[("Bash", "loop:Bash")]
            assert p.n_explicit_deny >= 1, (
                f"deny count not propagated: n_explicit_deny={p.n_explicit_deny}"
            )


# ──────────────────────────────────────────────────────────────────
# autonomy_stats + doctor section
# ──────────────────────────────────────────────────────────────────


class TestDoctorAutonomySection:
    def test_stats_empty_window(self) -> None:
        s = autonomy_stats([])
        assert s.n_bypass == 0
        assert s.n_explore == 0
        assert s.outliers == ()
        assert s.n_records == 0

    def test_stats_counts_bypass_and_explore(self) -> None:
        recs = [
            _mk_record(
                trace_id="t1",
                step_traces={"aegis.autonomy.step331.run": "auto"},
            ),
            _mk_record(
                trace_id="t2",
                step_traces={"aegis.autonomy.step331.run": "auto"},
            ),
            _mk_record(
                trace_id="t3",
                step_traces={"aegis.autonomy.step331.explore": "explore"},
            ),
            _mk_record(trace_id="t4"),  # no autonomy stamp
        ]
        s = autonomy_stats(recs)
        assert s.n_bypass == 2
        assert s.n_explore == 1
        assert s.n_records == 4

    def test_stats_detects_outlier(self) -> None:
        anchor = time.time_ns()
        bypass_rec = _mk_record(
            decision="ALLOW",
            reason="auto-approved by autonomy bypass",
            trace_id="bypass-1",
            aid="agent-X",
            ts_ns=anchor,
            step_traces={
                "aegis.autonomy.step331.run":
                    "step331: auto-approved signature=loop:Bash",
            },
        )
        block_rec = _mk_record(
            decision="BLOCK",
            reason="rule:dangerous_pattern: rm" + " -rf " + "/",
            trace_id="block-1",
            aid="agent-X",
            ts_ns=anchor + 1000,
        )
        s = autonomy_stats([bypass_rec, block_rec])
        assert len(s.outliers) == 1

    def test_doctor_report_includes_autonomy_section(self) -> None:
        recs = [
            _mk_record(
                trace_id="t1",
                step_traces={"aegis.autonomy.step331.run": "auto"},
            ),
        ]
        md = render_doctor_report(recs)
        assert "## 🤖 Autonomy" in md
        assert "자동 승인" in md

    def test_doctor_report_silent_when_no_autonomy(self) -> None:
        """When the window contains no autonomy events, the section
        still appears but stays a one-liner — keeps the report
        concise for operators who haven't opted in."""
        recs = [_mk_record(trace_id="t1")]
        md = render_doctor_report(recs)
        assert "## 🤖 Autonomy" in md
        assert "autonomy disabled or no bypass events" in md

    def test_doctor_report_renders_outlier_table(self) -> None:
        anchor = time.time_ns()
        recs = [
            _mk_record(
                decision="ALLOW",
                reason="auto-approved",
                trace_id="bypass-very-long-id-1234",
                aid="agent-Y",
                ts_ns=anchor,
                step_traces={
                    "aegis.autonomy.step331.run":
                        "step331: auto-approved signature=loop:Bash other=x",
                },
            ),
            _mk_record(
                decision="BLOCK",
                reason="rule:cloud_destructive",
                trace_id="block-1",
                aid="agent-Y",
                ts_ns=anchor + 1000,
            ),
        ]
        md = render_doctor_report(recs)
        assert "Outliers" in md
        assert "| trace_id |" in md or "trace_id" in md  # outlier table header
        assert "rule:cloud_destructive" in md
        assert "aegis autonomy deny" in md
