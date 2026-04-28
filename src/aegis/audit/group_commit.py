"""Group-commit wrapper for the encrypted ATV journal (v3.8).

Production durability pattern from `docs/WHITEPAPER_PERFORMANCE_KR.md` §2:
amortise a single ``fsync()`` across many ATV appends so per-call latency
becomes O(batch latency) instead of O(per-fsync). Throughput rises ~N×.

Semantics
---------
* ``append(record)`` is **synchronous** — the caller blocks until its
  line is durably written (fsync returned). This preserves the existing
  ``EncryptedJournal.append`` contract.
* Internally, multiple concurrent appends are queued; a daemon flusher
  thread pulls a batch (up to ``batch_size``) or waits up to
  ``interval_ms`` and writes them in one open() / write* / fsync /
  close() cycle.
* All appends in the batch see their result after the single fsync
  succeeds. If the fsync raises, every caller sees the same exception
  (atomicity at the batch granularity).

Tunables (see config.py)
------------------------
* ``batch_size``: hard cap on records per fsync. Default 100.
  At 100 K calls/sec, 100/batch → 1 K fsync/sec, comfortable for NVMe.
* ``interval_ms``: max wait before flushing a partial batch.
  Default 1.0 ms. p99 added latency budget.
* On graceful shutdown the queue is drained.

Patent linkage
--------------
The whitepaper §2 patterns A–D (group commit, tiered, replicated WAL,
Raft) are alternative durability models for the same audit chain
defined in claims 26 / 27 (HW/SW double check). v3.8 ships pattern A.
"""

from __future__ import annotations

import collections
import os
import threading
from pathlib import Path
from typing import Any

from aegis.audit.encrypted_journal import EncryptedJournal


class _PendingAppend:
    """A single queued append + its completion event."""

    __slots__ = ("record", "wrapper", "event", "exc")

    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.wrapper: dict[str, Any] | None = None
        self.event = threading.Event()
        self.exc: BaseException | None = None


class GroupCommitEncryptedJournal:
    """Drop-in replacement for ``EncryptedJournal`` with group-commit fsync.

    The constructor takes an *underlying* :class:`EncryptedJournal` so the
    encryption / on-disk format is bit-identical. Only the durability
    cadence changes.
    """

    def __init__(
        self,
        underlying: EncryptedJournal,
        *,
        batch_size: int = 100,
        interval_ms: float = 1.0,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if interval_ms <= 0:
            raise ValueError("interval_ms must be > 0")
        self._j = underlying
        self._batch_size = batch_size
        self._interval_ms = interval_ms
        self._queue: collections.deque[_PendingAppend] = collections.deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._flusher_loop,
            name="aegis-journal-group-commit",
            daemon=True,
        )
        self._thread.start()

    # Convenience pass-throughs
    @property
    def path(self) -> Path:
        return self._j.path

    def decrypt_record(self, wrapper: dict[str, Any]) -> dict[str, Any]:
        return self._j.decrypt_record(wrapper)

    def iter_records(self):  # type: ignore[no-untyped-def]
        return self._j.iter_records()

    def list_wrappers(self) -> list[dict[str, Any]]:
        return self._j.list_wrappers()

    # ── Group commit ─────────────────────────────────────────────────

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append one record. Blocks until the batch including this
        record is fsynced. Returns the wrapper metadata (same as the
        non-batched API)."""
        item = _PendingAppend(record)
        with self._cond:
            self._queue.append(item)
            self._cond.notify()
        # Block waiting for the flusher to fsync our batch.
        item.event.wait()
        if item.exc is not None:
            raise item.exc
        assert item.wrapper is not None
        return item.wrapper

    def _drain_batch(self) -> list[_PendingAppend]:
        """Pull up to ``batch_size`` items, waiting at most
        ``interval_ms`` for the first one to arrive."""
        with self._cond:
            if not self._queue:
                # Wait for the first item, bounded by interval.
                self._cond.wait(timeout=self._interval_ms / 1000.0)
            batch: list[_PendingAppend] = []
            while self._queue and len(batch) < self._batch_size:
                batch.append(self._queue.popleft())
            return batch

    def _flusher_loop(self) -> None:
        while not self._stop.is_set():
            batch = self._drain_batch()
            if not batch:
                continue
            self._write_and_fsync_batch(batch)
        # Drain remaining items on shutdown
        while True:
            with self._cond:
                tail = list(self._queue)
                self._queue.clear()
            if not tail:
                break
            self._write_and_fsync_batch(tail)

    def _write_and_fsync_batch(self, batch: list[_PendingAppend]) -> None:
        # 1. Encrypt every record up front (CPU-bound, no I/O).
        for item in batch:
            try:
                item.wrapper = self._j.encrypt(item.record)
            except BaseException as e:  # noqa: BLE001 — propagate any error
                item.exc = e

        live = [it for it in batch if it.exc is None and it.wrapper is not None]

        # 2. Single open() + write_all + fsync + close.
        try:
            with self._j._lock, self._j.path.open("a", encoding="utf-8") as f:
                for item in live:
                    f.write(EncryptedJournal.serialize(item.wrapper))  # type: ignore[arg-type]
                f.flush()
                os.fsync(f.fileno())
        except BaseException as e:  # noqa: BLE001 — every caller sees this
            for item in live:
                item.exc = e

        # 3. Wake every waiter (success or failure).
        for item in batch:
            item.event.set()

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self, *, timeout_sec: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=timeout_sec)


def make_journal(
    path: Path,
    data_key: bytes,
    *,
    group_commit: bool = False,
    batch_size: int = 100,
    interval_ms: float = 1.0,
) -> EncryptedJournal | GroupCommitEncryptedJournal:
    """Factory: returns a synchronous :class:`EncryptedJournal` by default,
    or a :class:`GroupCommitEncryptedJournal` when ``group_commit=True``."""
    base = EncryptedJournal(path=path, data_key=data_key)
    if not group_commit:
        return base
    return GroupCommitEncryptedJournal(
        base, batch_size=batch_size, interval_ms=interval_ms,
    )


# Add a thread-lock attribute to EncryptedJournal so the wrapper can
# share the same critical section for the file write. This keeps
# concurrent direct .append() and group-commit appends serialised on
# the same lock. (EncryptedJournal already has `_lock`; just expose.)


__all__ = ["GroupCommitEncryptedJournal", "make_journal"]
