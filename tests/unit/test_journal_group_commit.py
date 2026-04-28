"""Unit tests for src/aegis/audit/group_commit.py (v3.8)."""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

import pytest

from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.group_commit import GroupCommitEncryptedJournal, make_journal


def _key() -> bytes:
    return secrets.token_bytes(32)


def _record(i: int) -> dict:
    return {
        "verdict": "ALLOW",
        "payload": {"header": {"tenant_id": "t", "aid": f"a-{i}"}, "i": i},
    }


# ── Basic semantics ──────────────────────────────────────────────────


def test_append_round_trip(tmp_path: Path) -> None:
    """append → iter_records — encrypts + persists + decrypts identically."""
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    gc = GroupCommitEncryptedJournal(underlying, batch_size=10, interval_ms=2.0)
    try:
        for i in range(5):
            wrapper = gc.append(_record(i))
            assert wrapper["aid"] == f"a-{i}"
        records = [r for r in gc.iter_records() if "_decrypt_error" not in r]
        assert len(records) == 5
        assert {r["payload"]["i"] for r in records} == set(range(5))
    finally:
        gc.close()


def test_append_returns_after_durable(tmp_path: Path) -> None:
    """When ``append`` returns, the line is on disk (no in-flight buffer)."""
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    gc = GroupCommitEncryptedJournal(underlying, batch_size=1, interval_ms=0.5)
    try:
        gc.append(_record(0))
        # File must contain the line right now (no race window)
        content = (tmp_path / "j.jsonl").read_text(encoding="utf-8")
        assert content.count("\n") == 1
    finally:
        gc.close()


def test_make_journal_factory_sync(tmp_path: Path) -> None:
    j = make_journal(tmp_path / "j.jsonl", data_key=_key(), group_commit=False)
    assert isinstance(j, EncryptedJournal)


def test_make_journal_factory_group_commit(tmp_path: Path) -> None:
    j = make_journal(
        tmp_path / "j.jsonl", data_key=_key(),
        group_commit=True, batch_size=8, interval_ms=2.0,
    )
    assert isinstance(j, GroupCommitEncryptedJournal)
    j.close()


def test_invalid_batch_size_rejected(tmp_path: Path) -> None:
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    with pytest.raises(ValueError, match="batch_size"):
        GroupCommitEncryptedJournal(underlying, batch_size=0, interval_ms=1.0)


def test_invalid_interval_rejected(tmp_path: Path) -> None:
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    with pytest.raises(ValueError, match="interval_ms"):
        GroupCommitEncryptedJournal(underlying, batch_size=10, interval_ms=0.0)


# ── Concurrency / batching ───────────────────────────────────────────


def test_concurrent_appends_all_durable(tmp_path: Path) -> None:
    """N threads append concurrently — all records make it to disk."""
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    gc = GroupCommitEncryptedJournal(
        underlying, batch_size=20, interval_ms=2.0,
    )
    try:
        n = 50
        threads = []
        for i in range(n):
            t = threading.Thread(target=lambda i=i: gc.append(_record(i)))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()
        records = [r for r in gc.iter_records() if "_decrypt_error" not in r]
        assert len(records) == n
        assert {r["payload"]["i"] for r in records} == set(range(n))
    finally:
        gc.close()


def test_throughput_amortises_fsync(tmp_path: Path) -> None:
    """Group commit should NOT be slower than sync-per-call for batched
    workloads. Smoke test: 100 sequential appends through GC complete
    within a generous bound."""
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    gc = GroupCommitEncryptedJournal(
        underlying, batch_size=50, interval_ms=2.0,
    )
    try:
        t0 = time.perf_counter()
        for i in range(100):
            gc.append(_record(i))
        dt = time.perf_counter() - t0
        # 100 appends through GC. Even with 2ms interval each, well under 1s.
        assert dt < 5.0
        assert sum(1 for _ in gc.iter_records()) == 100
    finally:
        gc.close()


# ── Cross-compatibility with sync API ─────────────────────────────────


def test_group_committed_records_decrypt_with_plain_journal(tmp_path: Path) -> None:
    """A second process opening the file with the plain sync EncryptedJournal
    can iterate records group-committed earlier — proves on-disk format
    is bit-identical."""
    path = tmp_path / "j.jsonl"
    key = _key()

    underlying = EncryptedJournal(path, data_key=key)
    gc = GroupCommitEncryptedJournal(underlying, batch_size=5, interval_ms=2.0)
    try:
        for i in range(5):
            gc.append(_record(i))
    finally:
        gc.close()

    # Reopen with plain journal
    j2 = EncryptedJournal(path, data_key=key)
    records = [r for r in j2.iter_records() if "_decrypt_error" not in r]
    assert len(records) == 5


def test_close_drains_pending_queue(tmp_path: Path) -> None:
    """close() flushes any items still in the queue before terminating."""
    underlying = EncryptedJournal(tmp_path / "j.jsonl", data_key=_key())
    gc = GroupCommitEncryptedJournal(
        underlying, batch_size=1000, interval_ms=10_000.0,
    )
    # Set a huge interval so the flusher never wakes naturally;
    # we rely on close() to drain.
    finished = threading.Event()

    def writer() -> None:
        for i in range(3):
            gc.append(_record(i))
        finished.set()

    th = threading.Thread(target=writer)
    th.start()
    # Close immediately so drain path runs
    time.sleep(0.05)
    gc.close(timeout_sec=3.0)
    finished.wait(timeout=3.0)
    assert finished.is_set()
    th.join(timeout=2.0)

    # Re-open and verify all 3 records persisted
    j2 = EncryptedJournal(underlying.path, data_key=secrets.token_bytes(32))
    # ^ wrong key on purpose just to read wrappers
    assert len(j2.list_wrappers()) == 3
