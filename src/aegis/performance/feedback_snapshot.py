"""Persistent snapshot for the v3.2 perf-feedback EWMA store (v3.8).

The PerfFeedbackStore is per-(tenant, aid) EWMA state that the v3.1
KV cache advisor (and v3.4 / v3.7 advisors) backfill from. Without
persistence, a restart resets every EWMA → first ~5 turns after
restart see low-confidence advice and runtime falls back to native
heuristics. That's safe (advisory-only) but loses the ATV-driven
gain during warm-up.

Design (per docs/WHITEPAPER_PERFORMANCE_KR.md §4.2 + §5)
--------------------------------------------------------
* **Format**: SQLite with a single ``perf_feedback`` table — one row
  per (tenant_id, aid) key.
* **Trigger**: snapshot every ``min(N seconds, M updates since last
  snapshot)``. Default: ``N=30``, ``M=100``.
* **Background thread**: simple daemon thread that polls the in-memory
  store and writes when triggered. Lock-free reads of the store
  snapshot — the store's own lock guards the critical section.
* **Startup load**: eager — reads all rows on boot and seeds the
  store with prior EWMA state.
* **Crash safety**: SQLite WAL mode, durable writes per snapshot.
  RPO ≤ 30 sec by default. Acceptable because advice is advisory-only;
  losing 30 sec of EWMA observations costs ~5 turns of warm-up.
* **Production tuning**: bump M to 1000+ and N to 60s for high-
  throughput. T3 hardware (M19+) will swap this whole module for
  TEE-NVRAM-backed updates per ATV.

Patent linkage
--------------
Claim 42 — closed-loop perf attestation. The persisted snapshot
becomes the durable ground truth that survives restarts. T3
hardware can sign each row with the cost-attestation key (Claim 34).
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from aegis.performance.feedback import (
    PerfFeedbackStore,
    get_default_store,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS perf_feedback (
    tenant_id                  TEXT NOT NULL,
    aid                        TEXT NOT NULL,
    cache_hit_rate             REAL NOT NULL,
    context_utilization_ratio  REAL NOT NULL,
    tokens_per_second          REAL NOT NULL,
    runtime_latency_ms         REAL NOT NULL,
    memory_peak_bytes          REAL NOT NULL,
    sample_count               INTEGER NOT NULL,
    last_updated_ns            INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, aid)
);
"""

_DEFAULT_INTERVAL_SEC = 30.0
_DEFAULT_UPDATES_PER_SNAPSHOT = 100


@dataclass(frozen=True)
class SnapshotterConfig:
    """Tunables for the snapshotter."""

    interval_sec: float = _DEFAULT_INTERVAL_SEC
    updates_per_snapshot: int = _DEFAULT_UPDATES_PER_SNAPSHOT


class PerfFeedbackSnapshotter:
    """Periodic durability for ``PerfFeedbackStore``.

    Usage::

        store = get_default_store()
        snap = PerfFeedbackSnapshotter(
            store=store,
            db_path="./data/perf_feedback.sqlite",
        )
        snap.load_into_store()           # boot — restore prior EWMA
        snap.start()                     # background thread
        ... process traffic ...
        snap.stop()

    The class is **stop-able**: a clean shutdown emits one final
    snapshot before terminating the thread, so graceful restarts
    have RPO=0.
    """

    def __init__(
        self,
        *,
        store: PerfFeedbackStore | None = None,
        db_path: str | Path,
        config: SnapshotterConfig | None = None,
    ) -> None:
        self._store = store if store is not None else get_default_store()
        self._db_path = str(db_path)
        self._cfg = config or SnapshotterConfig()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_snapshot_ns: int = 0
        self._last_total_samples: int = 0
        # Make sure the parent directory exists; SQLite won't create it.
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    # ── Persistence ──────────────────────────────────────────────────

    def snapshot_now(self) -> int:
        """Write the entire in-memory store to SQLite and return the
        number of rows written. Synchronous, fsync-durable on commit.
        """
        from aegis.performance.feedback import _PerfState as _State

        rows: list[tuple[str, str, _State]] = []
        # Snapshot the store's keys + values under its lock so the
        # update path doesn't race us.
        with self._store._lock:
            for (tenant_id, aid), st in self._store._states.items():
                rows.append((tenant_id, aid, st))

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                for tenant_id, aid, st in rows:
                    cur.execute(
                        """INSERT OR REPLACE INTO perf_feedback (
                            tenant_id, aid,
                            cache_hit_rate, context_utilization_ratio,
                            tokens_per_second, runtime_latency_ms,
                            memory_peak_bytes,
                            sample_count, last_updated_ns
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            tenant_id, aid,
                            st.cache_hit_rate, st.context_utilization_ratio,
                            st.tokens_per_second, st.runtime_latency_ms,
                            st.memory_peak_bytes,
                            st.sample_count, st.last_updated_ns,
                        ),
                    )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            self._last_snapshot_ns = time.time_ns()
            self._last_total_samples = sum(s.sample_count for _, _, s in rows)
        return len(rows)

    def load_into_store(self) -> int:
        """Restore prior snapshot rows into the store. Call once at boot.
        Returns the number of rows loaded."""
        from aegis.performance.feedback import _PerfState as _State

        rows = self._conn.execute(
            "SELECT tenant_id, aid, cache_hit_rate, context_utilization_ratio,"
            " tokens_per_second, runtime_latency_ms, memory_peak_bytes,"
            " sample_count, last_updated_ns FROM perf_feedback"
        ).fetchall()
        with self._store._lock:
            for r in rows:
                key = (r[0], r[1])
                st = _State(
                    cache_hit_rate=r[2],
                    context_utilization_ratio=r[3],
                    tokens_per_second=r[4],
                    runtime_latency_ms=r[5],
                    memory_peak_bytes=r[6],
                    sample_count=int(r[7]),
                    last_updated_ns=int(r[8]),
                )
                self._store._states[key] = st
            self._last_total_samples = sum(
                s.sample_count for s in self._store._states.values()
            )
        return len(rows)

    # ── Background loop ──────────────────────────────────────────────

    def _should_snapshot(self) -> bool:
        """Either ``interval_sec`` elapsed OR ``updates_per_snapshot``
        observations accumulated since the last snapshot."""
        now = time.time_ns()
        elapsed_sec = (now - self._last_snapshot_ns) / 1e9
        if self._last_snapshot_ns == 0:
            return False  # nothing to snapshot yet
        if elapsed_sec >= self._cfg.interval_sec:
            return True
        # Count current samples
        with self._store._lock:
            current_total = sum(
                s.sample_count for s in self._store._states.values()
            )
        new_samples = current_total - self._last_total_samples
        return new_samples >= self._cfg.updates_per_snapshot

    def _loop(self) -> None:
        # Take an immediate snapshot so subsequent triggers can compare
        self.snapshot_now()
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=1.0)  # poll at 1Hz
            if self._stop_event.is_set():
                break
            if self._should_snapshot():
                # Don't crash the daemon thread on transient SQLite errors;
                # next tick will retry.
                with contextlib.suppress(Exception):
                    self.snapshot_now()
        # Final flush on graceful stop
        with contextlib.suppress(Exception):
            self.snapshot_now()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="perf-feedback-snapshotter",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_sec: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout_sec)
        self._thread = None

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._conn.close()

    # ── Introspection (test helpers) ─────────────────────────────────

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def last_snapshot_ns(self) -> int:
        return self._last_snapshot_ns

    def row_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM perf_feedback").fetchone()[0]
        )


__all__ = ["PerfFeedbackSnapshotter", "SnapshotterConfig"]
