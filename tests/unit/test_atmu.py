"""Unit tests for the ATMU (Agent Telemetry Management Unit) package
— patent §5A + APPENDIX B (M10)."""

from __future__ import annotations

import threading
import time

import pytest

from aegis.atmu import (
    HIGH_BLAST_THRESHOLD,
    IntentLog,
    InvalidTransition,
    TxState,
    can_transition,
    ensure_transition,
    make_checkpoint,
    plan_for,
)
from aegis.schema import ATVHeader, ATVInput


def _inp(**over: object) -> ATVInput:
    base: dict[str, object] = dict(
        header=ATVHeader(
            trace_id="t", span_id="s", tenant_id="demo-tenant",
            aid="a", timestamp_ns=time.time_ns(),
        ),
        plan_text="plan",
        tool_name="read_file",
        tool_args_json='{"path":"./data/x.txt"}',
    )
    base.update(over)
    return ATVInput(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────
class TestStateMachine:
    @pytest.mark.parametrize(
        "frm, to, expected",
        [
            (TxState.TENTATIVE, TxState.PREPARED, True),
            (TxState.TENTATIVE, TxState.ABORTED, True),
            (TxState.TENTATIVE, TxState.QUARANTINED, True),
            (TxState.TENTATIVE, TxState.COMMITTED, False),    # must go through prepared
            (TxState.TENTATIVE, TxState.ROLLED_BACK, False),
            (TxState.PREPARED, TxState.COMMITTED, True),
            (TxState.PREPARED, TxState.ABORTED, True),
            (TxState.COMMITTED, TxState.ROLLED_BACK, True),
            (TxState.COMMITTED, TxState.COMPENSATED, True),
            (TxState.ABORTED, TxState.PREPARED, False),       # terminal
            (TxState.ABORTED, TxState.COMMITTED, False),
            (TxState.ROLLED_BACK, TxState.COMMITTED, False),  # terminal
            (TxState.QUARANTINED, TxState.COMMITTED, False),  # admin-release only
        ],
    )
    def test_legal_transitions(self, frm: TxState, to: TxState, expected: bool) -> None:
        assert can_transition(frm, to) is expected

    def test_ensure_transition_raises_on_illegal(self) -> None:
        with pytest.raises(InvalidTransition):
            ensure_transition(TxState.ABORTED, TxState.COMMITTED)


# ─────────────────────────────────────────────────────────────────────
# Intent log
# ─────────────────────────────────────────────────────────────────────
class TestIntentLog:
    def _log(self) -> IntentLog:
        return IntentLog(":memory:")

    def _append(self, log: IntentLog, **over: object) -> dict[str, object]:
        defaults: dict[str, object] = dict(
            aid="a", tenant_id="demo-tenant", trace_id="t", span_id="s",
            parent_span_id=None, tool_name="read_file",
            tool_args_hash="0" * 64, blast_radius=1, atv_commitment="atv-x",
        )
        defaults.update(over)
        return log.append_tentative(**defaults)  # type: ignore[arg-type]

    def test_append_starts_at_tentative(self) -> None:
        log = self._log()
        rec = self._append(log)
        assert rec["current_state"] == "tentative"
        assert rec["state_history"][0]["state"] == "tentative"
        assert rec["seq"] == 1

    def test_seq_monotonic(self) -> None:
        log = self._log()
        a = self._append(log)
        b = self._append(log)
        assert b["seq"] == int(a["seq"]) + 1

    def test_blast_class_low_medium_high_critical(self) -> None:
        log = self._log()
        assert self._append(log, blast_radius=1)["blast_class"] == "low"
        assert self._append(log, blast_radius=5)["blast_class"] == "medium"
        assert self._append(log, blast_radius=8)["blast_class"] == "high"
        assert self._append(log, blast_radius=10)["blast_class"] == "critical"

    def test_legal_transition_persists(self) -> None:
        log = self._log()
        rec = self._append(log)
        rec2 = log.transition(rec["record_id"], new_state=TxState.PREPARED, reason="ok")
        assert rec2["current_state"] == "prepared"
        assert len(rec2["state_history"]) == 2
        rec3 = log.transition(rec["record_id"], new_state=TxState.COMMITTED, reason="signed")
        assert rec3["current_state"] == "committed"
        assert len(rec3["state_history"]) == 3

    def test_illegal_transition_rejected_and_state_unchanged(self) -> None:
        log = self._log()
        rec = self._append(log)
        with pytest.raises(InvalidTransition):
            log.transition(rec["record_id"], new_state=TxState.COMMITTED, reason="skip prepared")
        assert log.get(rec["record_id"])["current_state"] == "tentative"  # unchanged

    def test_unknown_record_id_raises(self) -> None:
        log = self._log()
        with pytest.raises(KeyError):
            log.transition("not-a-real-id", new_state=TxState.PREPARED, reason="x")

    def test_tool_outcome_attaches(self) -> None:
        log = self._log()
        rec = self._append(log)
        log.transition(rec["record_id"], new_state=TxState.PREPARED, reason="ok")
        log.transition(rec["record_id"], new_state=TxState.COMMITTED, reason="signed")
        out = log.append_tool_outcome(
            rec["record_id"], status="success", result_hash="abc",
            side_effect_receipt="txn-42",
        )
        assert out["tool_outcome"]["status"] == "success"
        assert out["tool_outcome"]["result_hash"] == "abc"
        assert out["tool_outcome"]["side_effect_receipt"] == "txn-42"

    def test_invalid_outcome_status_rejected(self) -> None:
        log = self._log()
        rec = self._append(log)
        with pytest.raises(ValueError):
            log.append_tool_outcome(rec["record_id"], status="garbage", result_hash="x")

    def test_compensation_plan_persists(self) -> None:
        log = self._log()
        rec = self._append(log, tool_name="transfer_funds", blast_radius=10)
        log.set_compensation_plan(rec["record_id"], {"strategy": "counter_transfer"})
        got = log.get(rec["record_id"])
        assert got["compensation_plan"] == {"strategy": "counter_transfer"}

    def test_list_by_aid_returns_seq_order(self) -> None:
        log = self._log()
        ids = [self._append(log, span_id=f"s-{i}")["record_id"] for i in range(5)]
        out = log.list_by_aid("a")
        assert [r["record_id"] for r in out] == ids

    def test_count_state_buckets(self) -> None:
        log = self._log()
        for _ in range(3):
            self._append(log)
        a = self._append(log)
        log.transition(a["record_id"], new_state=TxState.PREPARED, reason="ok")
        assert log.count_state(TxState.TENTATIVE) == 3
        assert log.count_state(TxState.PREPARED) == 1
        assert log.count_state(TxState.COMMITTED) == 0

    def test_concurrent_appends_keep_seq_unique(self) -> None:
        """20 threads × 5 appends each must yield 100 monotonic, unique seqs."""
        log = self._log()
        n_threads, per_thread = 20, 5

        def worker() -> None:
            for _ in range(per_thread):
                self._append(log, span_id=f"s-{time.time_ns()}")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = log.list_by_aid("a")
        seqs = [int(r["seq"]) for r in rows]
        assert len(rows) == n_threads * per_thread
        assert len(set(seqs)) == len(seqs)
        assert seqs == sorted(seqs)


# ─────────────────────────────────────────────────────────────────────
# Checkpoint
# ─────────────────────────────────────────────────────────────────────
class TestCheckpoint:
    def test_low_blast_returns_none(self) -> None:
        assert make_checkpoint(_inp(), blast_radius=1) is None

    def test_high_blast_returns_manifest(self) -> None:
        cp = make_checkpoint(_inp(tool_name="transfer_funds"), blast_radius=HIGH_BLAST_THRESHOLD)
        assert cp is not None
        assert "checkpoint_id" in cp
        assert len(cp["manifest_hash"]) == 64

    def test_deterministic_for_same_input(self) -> None:
        # checkpoint_id is uuid (changes), manifest_hash is content-derived.
        a = make_checkpoint(_inp(), blast_radius=10)
        b = make_checkpoint(_inp(), blast_radius=10)
        assert a is not None and b is not None
        assert a["manifest_hash"] == b["manifest_hash"]


# ─────────────────────────────────────────────────────────────────────
# Compensation registry
# ─────────────────────────────────────────────────────────────────────
class TestCompensation:
    def test_known_irreversible_tools_have_plans(self) -> None:
        for tool in ("transfer_funds", "send_email", "execute_shell", "delete_file"):
            assert plan_for(tool) is not None

    def test_idempotent_tools_no_plan(self) -> None:
        assert plan_for("read_file") is None
        assert plan_for("list_directory") is None
