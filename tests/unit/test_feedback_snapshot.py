"""Unit tests for src/aegis/performance/feedback_snapshot.py (v3.8)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aegis.performance import (
    PerfFeedbackSnapshotter,
    PerfFeedbackStore,
    SnapshotterConfig,
)

# ── Snapshot round-trip ───────────────────────────────────────────────


def test_snapshot_persists_and_loads(tmp_path: Path) -> None:
    """Update store → snapshot → recreate store → load → identical state."""
    store = PerfFeedbackStore()
    store.update(tenant_id="t1", aid="a1", cache_hit_rate=0.85)
    store.update(tenant_id="t1", aid="a1", tokens_per_second=200.0)
    store.update(tenant_id="t2", aid="a2", cache_hit_rate=0.50)

    db_path = tmp_path / "perf.sqlite"
    snap = PerfFeedbackSnapshotter(store=store, db_path=db_path)
    n = snap.snapshot_now()
    assert n == 2  # two distinct (tenant, aid) keys
    snap.close()

    # Fresh store + load
    store2 = PerfFeedbackStore()
    snap2 = PerfFeedbackSnapshotter(store=store2, db_path=db_path)
    loaded = snap2.load_into_store()
    assert loaded == 2

    fb_a = store2.get(tenant_id="t1", aid="a1")
    fb_b = store2.get(tenant_id="t2", aid="a2")
    assert fb_a.cache_hit_rate > 0
    assert fb_a.tokens_per_second > 0
    assert fb_a.sample_count == 2  # two updates
    assert fb_b.cache_hit_rate > 0
    assert fb_b.sample_count == 1
    snap2.close()


def test_snapshot_empty_store_is_noop(tmp_path: Path) -> None:
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(store=store, db_path=tmp_path / "p.sqlite")
    n = snap.snapshot_now()
    assert n == 0
    assert snap.row_count() == 0
    snap.close()


def test_snapshot_overwrites_existing_row(tmp_path: Path) -> None:
    """Subsequent snapshots reflect the latest EWMA, not an append-only log."""
    store = PerfFeedbackStore()
    db = tmp_path / "p.sqlite"
    snap = PerfFeedbackSnapshotter(store=store, db_path=db)

    store.update(tenant_id="t", aid="a", cache_hit_rate=0.10)
    snap.snapshot_now()
    assert snap.row_count() == 1

    store.update(tenant_id="t", aid="a", cache_hit_rate=0.90)
    snap.snapshot_now()
    assert snap.row_count() == 1  # still one row, updated in place

    snap.close()


# ── Trigger logic ─────────────────────────────────────────────────────


def test_should_snapshot_on_interval(tmp_path: Path) -> None:
    """``_should_snapshot`` becomes True once interval_sec has elapsed."""
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(
        store=store, db_path=tmp_path / "p.sqlite",
        config=SnapshotterConfig(interval_sec=0.05, updates_per_snapshot=10_000),
    )
    # Force last_snapshot_ns to "now" to start counting
    snap.snapshot_now()
    assert snap._should_snapshot() is False
    time.sleep(0.10)
    assert snap._should_snapshot() is True
    snap.close()


def test_should_snapshot_on_updates_threshold(tmp_path: Path) -> None:
    """``_should_snapshot`` becomes True after N updates regardless of time."""
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(
        store=store, db_path=tmp_path / "p.sqlite",
        config=SnapshotterConfig(interval_sec=3600.0, updates_per_snapshot=3),
    )
    snap.snapshot_now()
    for _ in range(2):
        store.update(tenant_id="t", aid="a", cache_hit_rate=0.5)
    assert snap._should_snapshot() is False
    store.update(tenant_id="t", aid="a", cache_hit_rate=0.5)  # 3rd → trigger
    assert snap._should_snapshot() is True
    snap.close()


# ── Background thread lifecycle ───────────────────────────────────────


def test_start_stop_cleanly(tmp_path: Path) -> None:
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(
        store=store, db_path=tmp_path / "p.sqlite",
        config=SnapshotterConfig(interval_sec=0.05, updates_per_snapshot=1),
    )
    snap.start()
    store.update(tenant_id="t", aid="a", cache_hit_rate=0.5)
    time.sleep(0.20)  # let background thread snapshot at least once
    snap.stop(timeout_sec=2.0)
    assert snap.row_count() >= 1


def test_double_start_is_noop(tmp_path: Path) -> None:
    """Calling start() twice doesn't spawn a second thread."""
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(
        store=store, db_path=tmp_path / "p.sqlite",
        config=SnapshotterConfig(interval_sec=10.0, updates_per_snapshot=10_000),
    )
    snap.start()
    first_thread = snap._thread
    snap.start()  # idempotent
    assert snap._thread is first_thread
    snap.stop(timeout_sec=2.0)


def test_stop_without_start_is_safe(tmp_path: Path) -> None:
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(store=store, db_path=tmp_path / "p.sqlite")
    snap.stop()  # should not raise


# ── End-to-end durability simulation ──────────────────────────────────


def test_simulated_restart_recovers_ewma(tmp_path: Path) -> None:
    """Heavy update → snapshot → 'restart' (close + new instance) →
    EWMA continues from prior state, not from zero."""
    db = tmp_path / "p.sqlite"

    # Lifetime 1: accumulate signal
    store = PerfFeedbackStore(alpha=0.30)
    snap = PerfFeedbackSnapshotter(store=store, db_path=db)
    for _ in range(20):
        store.update(tenant_id="prod", aid="agent-A", cache_hit_rate=0.90)
    snap.snapshot_now()
    snap.close()
    pre_value = store.get(tenant_id="prod", aid="agent-A").cache_hit_rate
    assert pre_value == pytest.approx(0.90, rel=1e-2)

    # Lifetime 2: cold start, load, one more update
    store2 = PerfFeedbackStore(alpha=0.30)
    snap2 = PerfFeedbackSnapshotter(store=store2, db_path=db)
    snap2.load_into_store()
    post_load = store2.get(tenant_id="prod", aid="agent-A").cache_hit_rate
    assert post_load == pytest.approx(pre_value, rel=1e-3)

    # Updating after load should *continue* the EWMA, not restart it
    store2.update(tenant_id="prod", aid="agent-A", cache_hit_rate=0.20)
    post_update = store2.get(tenant_id="prod", aid="agent-A").cache_hit_rate
    expected = 0.30 * 0.20 + 0.70 * post_load
    assert post_update == pytest.approx(expected, rel=1e-3)
    snap2.close()


def test_load_with_no_existing_db_creates_empty_store(tmp_path: Path) -> None:
    store = PerfFeedbackStore()
    snap = PerfFeedbackSnapshotter(store=store, db_path=tmp_path / "fresh.sqlite")
    n = snap.load_into_store()
    assert n == 0
    snap.close()


def test_perf_state_round_trip_preserves_sample_count(tmp_path: Path) -> None:
    store = PerfFeedbackStore()
    db = tmp_path / "p.sqlite"
    snap = PerfFeedbackSnapshotter(store=store, db_path=db)

    for i in range(7):
        store.update(tenant_id="t", aid="a", cache_hit_rate=0.5 + i * 0.05)
    snap.snapshot_now()
    snap.close()

    store2 = PerfFeedbackStore()
    snap2 = PerfFeedbackSnapshotter(store=store2, db_path=db)
    snap2.load_into_store()
    fb = store2.get(tenant_id="t", aid="a")
    assert fb.sample_count == 7
    assert fb.last_updated_ns > 0
    snap2.close()
