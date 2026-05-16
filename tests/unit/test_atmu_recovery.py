"""v0.5.8 PR ⑪ — ATMU auto WAL replay (orphan recovery).

Covers `find_orphans` + `recover_orphans` + `render_sweep_summary`
against an in-memory IntentLog. Uses synthetic backdated rows
(direct UPDATE of `created_at_ns`) so the age threshold logic is
testable without `time.sleep()`.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aegis.atmu import (
    IntentLog,
    OrphanSweepResult,
    TxState,
    find_orphans,
    recover_orphans,
    render_sweep_summary,
)
from aegis.atmu.recovery import NON_TERMINAL_STATES

# ── helpers ────────────────────────────────────────────────────────


@pytest.fixture
def log(tmp_path: Path):
    """Fresh IntentLog per test, closed automatically on teardown."""
    p = tmp_path / "intent.sqlite"
    log = IntentLog(str(p))
    yield log
    log.close()


def _append_tentative(log: IntentLog, *, aid: str = "aid", tool: str = "Bash"):
    """Convenience: append a TENTATIVE row with sensible defaults."""
    return log.append_tentative(
        aid=aid, tenant_id="t",
        trace_id=f"tr-{aid}", span_id=f"sp-{aid}",
        parent_span_id=None, tool_name=tool, tool_args_hash="h",
        blast_radius=5, atv_commitment="c",
    )


def _backdate(log: IntentLog, record_id: str, hours_ago: float) -> None:
    """Mutate `created_at_ns` so a row appears N hours older.

    Side-channel write — bypasses the state machine on purpose,
    purely to make the age threshold testable without a real
    clock advance.
    """
    now_ns = time.time_ns()
    backdated_ns = now_ns - int(hours_ago * 3600 * 1_000_000_000)
    log.conn.execute(
        "UPDATE intent_log SET created_at_ns=? WHERE record_id=?",
        (backdated_ns, record_id),
    )


# ── NON_TERMINAL_STATES sanity ─────────────────────────────────────


def test_non_terminal_states_excludes_terminals() -> None:
    """The recovery sweep must never target terminal rows."""
    from aegis.atmu.state_machine import TERMINAL_STATES
    for s in NON_TERMINAL_STATES:
        assert s not in TERMINAL_STATES


def test_non_terminal_states_includes_tentative_and_prepared() -> None:
    assert TxState.TENTATIVE in NON_TERMINAL_STATES
    assert TxState.PREPARED in NON_TERMINAL_STATES


# ── find_orphans — pure (no mutation) ──────────────────────────────


def test_find_orphans_on_empty_log_is_empty(log: IntentLog) -> None:
    eligible, young = find_orphans(log)
    assert eligible == ()
    assert young == ()


def test_find_orphans_splits_by_age(log: IntentLog) -> None:
    old = _append_tentative(log, aid="old")
    _backdate(log, old["record_id"], hours_ago=30.0)
    young = _append_tentative(log, aid="young")
    _backdate(log, young["record_id"], hours_ago=1.0)

    eligible, too_young = find_orphans(log, max_age_hours=24.0)
    assert len(eligible) == 1
    assert eligible[0].aid == "old"
    assert eligible[0].age_seconds > 24 * 3600
    assert len(too_young) == 1
    assert too_young[0].aid == "young"


def test_find_orphans_zero_threshold_is_all_eligible(log: IntentLog) -> None:
    """`max_age_hours=0` makes every non-terminal row eligible —
    useful for test fixtures and operator overrides."""
    _append_tentative(log, aid="a")
    _append_tentative(log, aid="b")
    eligible, too_young = find_orphans(log, max_age_hours=0.0)
    assert len(eligible) == 2
    assert too_young == ()


def test_find_orphans_ignores_terminal_states(log: IntentLog) -> None:
    """Rows already in ABORTED / COMMITTED must not appear in
    the sweep list — only non-terminals."""
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=100.0)
    # Move directly to ABORTED.
    log.transition(
        rec["record_id"], new_state=TxState.ABORTED, reason="test setup",
    )
    eligible, too_young = find_orphans(log, max_age_hours=0.0)
    assert eligible == ()
    assert too_young == ()


def test_find_orphans_pure_no_mutation(log: IntentLog) -> None:
    """`find_orphans` must NOT change any row state."""
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=99.0)
    find_orphans(log, max_age_hours=24.0)
    # Row is still TENTATIVE.
    row = log.get(rec["record_id"])
    assert row is not None
    assert row["current_state"] == TxState.TENTATIVE.value


# ── recover_orphans — mutation ─────────────────────────────────────


def test_recover_orphans_dry_run_does_not_mutate(log: IntentLog) -> None:
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=30.0)
    result = recover_orphans(log, dry_run=True, max_age_hours=24.0)
    assert result.dry_run is True
    assert result.n_swept == 1   # "would sweep"
    # But the underlying row is unchanged.
    row = log.get(rec["record_id"])
    assert row is not None
    assert row["current_state"] == TxState.TENTATIVE.value


def test_recover_orphans_transitions_to_aborted(log: IntentLog) -> None:
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=30.0)
    result = recover_orphans(log, max_age_hours=24.0)
    assert result.n_swept == 1
    assert result.n_failed == 0
    row = log.get(rec["record_id"])
    assert row is not None
    assert row["current_state"] == TxState.ABORTED.value
    # State history records the auto-recovery reason.
    history = row["state_history"]
    aborted_entry = next(
        (e for e in history if e["state"] == TxState.ABORTED.value), None,
    )
    assert aborted_entry is not None
    assert "orphan" in aborted_entry["reason"].lower()


def test_recover_orphans_idempotent(log: IntentLog) -> None:
    """Running the sweep twice produces no spurious work the second
    time — the first sweep moved everything eligible to a terminal
    state, the second finds nothing to do."""
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=30.0)
    first = recover_orphans(log, max_age_hours=24.0)
    second = recover_orphans(log, max_age_hours=24.0)
    assert first.n_swept == 1
    assert second.n_swept == 0


def test_recover_orphans_skips_young_rows(log: IntentLog) -> None:
    """Rows younger than the threshold must NOT be touched."""
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=1.0)
    result = recover_orphans(log, max_age_hours=24.0)
    assert result.n_swept == 0
    assert result.n_skipped_young == 1
    # Row is still TENTATIVE.
    row = log.get(rec["record_id"])
    assert row is not None
    assert row["current_state"] == TxState.TENTATIVE.value


def test_recover_orphans_handles_prepared_state(log: IntentLog) -> None:
    """PREPARED rows (phase-2 not yet reached) are also orphans."""
    rec = _append_tentative(log)
    log.transition(
        rec["record_id"], new_state=TxState.PREPARED, reason="ok",
    )
    _backdate(log, rec["record_id"], hours_ago=30.0)
    result = recover_orphans(log, max_age_hours=24.0)
    assert result.n_swept == 1
    row = log.get(rec["record_id"])
    assert row is not None
    assert row["current_state"] == TxState.ABORTED.value


def test_recover_orphans_uses_custom_reason(log: IntentLog) -> None:
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=30.0)
    recover_orphans(
        log, max_age_hours=24.0,
        reason="custom recovery — see runbook §3",
    )
    row = log.get(rec["record_id"])
    assert row is not None
    aborted = next(
        e for e in row["state_history"]
        if e["state"] == TxState.ABORTED.value
    )
    assert "runbook" in aborted["reason"]


def test_recover_orphans_per_row_failure_isolation(
    log: IntentLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a single transition raises, the rest of the sweep still
    runs. The failing row lands in result.failed."""
    rec_a = _append_tentative(log, aid="a")
    rec_b = _append_tentative(log, aid="b")
    _backdate(log, rec_a["record_id"], hours_ago=30.0)
    _backdate(log, rec_b["record_id"], hours_ago=30.0)

    original_transition = log.transition
    call_count = {"n": 0}

    def flaky(record_id, *, new_state, reason):
        call_count["n"] += 1
        # Fail the FIRST call, succeed the second.
        if call_count["n"] == 1:
            raise RuntimeError("simulated sqlite failure")
        return original_transition(
            record_id, new_state=new_state, reason=reason,
        )

    monkeypatch.setattr(log, "transition", flaky)

    result = recover_orphans(log, max_age_hours=24.0)
    assert result.n_swept == 1
    assert result.n_failed == 1
    assert "simulated" in result.failed[0][1].lower()


# ── render_sweep_summary ───────────────────────────────────────────


def test_render_summary_dry_run_says_would_sweep(log: IntentLog) -> None:
    rec = _append_tentative(log)
    _backdate(log, rec["record_id"], hours_ago=30.0)
    result = recover_orphans(log, dry_run=True, max_age_hours=24.0)
    out = render_sweep_summary(result)
    assert "would sweep" in out


def test_render_summary_truncates_long_lists(log: IntentLog) -> None:
    """With > 20 swept rows, the renderer truncates and shows an
    "… N more" line."""
    # 25 backdated TENTATIVE rows.
    for i in range(25):
        r = _append_tentative(log, aid=f"a{i}")
        _backdate(log, r["record_id"], hours_ago=30.0)
    result = recover_orphans(log, max_age_hours=24.0)
    out = render_sweep_summary(result)
    assert result.n_swept == 25
    assert "5 more" in out


def test_render_summary_empty_sweep_still_renders(log: IntentLog) -> None:
    result = recover_orphans(log, max_age_hours=24.0)
    out = render_sweep_summary(result)
    assert "swept: 0" in out or "0 row" in out


# ── OrphanSweepResult shape ────────────────────────────────────────


def test_sweep_result_n_total_eligible_sums_correctly(log: IntentLog) -> None:
    old = _append_tentative(log, aid="old")
    _backdate(log, old["record_id"], hours_ago=30.0)
    young = _append_tentative(log, aid="young")
    _backdate(log, young["record_id"], hours_ago=1.0)
    result = recover_orphans(log, max_age_hours=24.0)
    assert isinstance(result, OrphanSweepResult)
    assert result.n_total_eligible == result.n_swept + result.n_skipped_young + result.n_failed
