"""Unit tests for src/aegis/audit/tiered_archive.py (v3.9)."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.tiered_archive import (
    ArchivePolicy,
    FilesystemArchive,
    S3ArchiveStub,
    TieredArchiveMigrator,
)


def _key() -> bytes:
    return secrets.token_bytes(32)


def _record(i: int) -> dict:
    return {
        "verdict": "ALLOW",
        "payload": {"header": {"tenant_id": "t", "aid": f"a-{i}"}, "i": i},
    }


# ── FilesystemArchive backend ─────────────────────────────────────────


def test_filesystem_archive_creates_cold_dir(tmp_path: Path) -> None:
    cold = tmp_path / "cold"
    FilesystemArchive(cold_dir=cold)
    assert cold.is_dir()


def test_filesystem_archive_copies_segment(tmp_path: Path) -> None:
    src = tmp_path / "live.0001.jsonl"
    src.write_text("hello\n")
    backend = FilesystemArchive(cold_dir=tmp_path / "cold")
    identifier = backend.archive(src)
    assert Path(identifier).exists()
    assert Path(identifier).read_text() == "hello\n"


def test_filesystem_archive_list(tmp_path: Path) -> None:
    backend = FilesystemArchive(cold_dir=tmp_path / "cold")
    (tmp_path / "live.0001.jsonl").write_text("a")
    (tmp_path / "live.0002.jsonl").write_text("b")
    backend.archive(tmp_path / "live.0001.jsonl")
    backend.archive(tmp_path / "live.0002.jsonl")
    listed = backend.list_archived()
    assert len(listed) == 2
    assert all("live" in p for p in listed)


# ── S3ArchiveStub contract ────────────────────────────────────────────


def test_s3_stub_records_keys(tmp_path: Path) -> None:
    """Stub doesn't actually upload — just records the key it would have used."""
    src = tmp_path / "seg.0001.jsonl"
    src.write_text("x")
    stub = S3ArchiveStub(bucket="my-bucket", prefix="aegis/")
    key = stub.archive(src)
    assert key == "s3://my-bucket/aegis/seg.0001.jsonl"
    # list_archived reflects the recorded key
    assert key in stub.list_archived()


# ── Migrator: rotation ────────────────────────────────────────────────


def test_rotate_now_with_data(tmp_path: Path) -> None:
    """Rotate moves the live file aside; next index gets used."""
    live = tmp_path / "audit.jsonl"
    live.write_text("line\n")
    mig = TieredArchiveMigrator(
        live_path=live, backend=FilesystemArchive(tmp_path / "cold"),
    )
    rotated = mig.rotate_now()
    assert rotated is not None
    assert rotated.exists()
    assert rotated.name == "audit.0001.jsonl"
    assert not live.exists()  # journal will recreate it on next append


def test_rotate_now_with_empty_returns_none(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    mig = TieredArchiveMigrator(
        live_path=live, backend=FilesystemArchive(tmp_path / "cold"),
    )
    assert mig.rotate_now() is None
    live.write_text("")
    assert mig.rotate_now() is None


def test_rotate_increments_index(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    mig = TieredArchiveMigrator(
        live_path=live, backend=FilesystemArchive(tmp_path / "cold"),
    )
    for i in range(3):
        live.write_text(f"line-{i}\n")
        mig.rotate_now()
    rotated_files = sorted(tmp_path.glob("audit.*.jsonl"))
    assert [p.name for p in rotated_files] == [
        "audit.0001.jsonl", "audit.0002.jsonl", "audit.0003.jsonl",
    ]


def test_should_rotate_size(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.write_bytes(b"x" * 100)
    mig = TieredArchiveMigrator(
        live_path=live,
        backend=FilesystemArchive(tmp_path / "cold"),
        policy=ArchivePolicy(rotate_bytes=50, rotate_seconds=3600),
    )
    assert mig._should_rotate() is True


def test_should_rotate_time(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.write_text("x")
    mig = TieredArchiveMigrator(
        live_path=live,
        backend=FilesystemArchive(tmp_path / "cold"),
        policy=ArchivePolicy(rotate_bytes=10**9, rotate_seconds=0.05),
    )
    time.sleep(0.10)
    assert mig._should_rotate() is True


# ── Migrator: archive ─────────────────────────────────────────────────


def test_archive_pending_pushes_segment_to_backend(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.write_text("a\n")
    backend = FilesystemArchive(tmp_path / "cold")
    mig = TieredArchiveMigrator(live_path=live, backend=backend)
    mig.rotate_now()
    archived = mig.archive_pending()
    assert len(archived) == 1
    assert archived[0].archive_id is not None
    assert backend.list_archived()


def test_archive_pending_idempotent(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.write_text("a")
    mig = TieredArchiveMigrator(
        live_path=live, backend=FilesystemArchive(tmp_path / "cold"),
    )
    mig.rotate_now()
    first = mig.archive_pending()
    second = mig.archive_pending()  # nothing pending now
    assert len(first) == 1
    assert len(second) == 0


# ── Migrator: hot tier retention ──────────────────────────────────────


def test_prune_hot_tier_keeps_n_most_recent(tmp_path: Path) -> None:
    """After K rotations + archives, only the N most recent stay on hot tier."""
    live = tmp_path / "audit.jsonl"
    backend = FilesystemArchive(tmp_path / "cold")
    mig = TieredArchiveMigrator(
        live_path=live, backend=backend,
        policy=ArchivePolicy(hot_retention_segments=2),
    )
    for i in range(5):
        live.write_text(f"line-{i}\n")
        mig.rotate_now()
        mig.archive_pending()
    deleted = mig.prune_hot_tier()
    # 5 rotated, retention 2 → 3 should be deleted from hot tier.
    assert len(deleted) == 3
    remaining_hot = sorted(tmp_path.glob("audit.*.jsonl"))
    assert len(remaining_hot) == 2
    # All 5 still on cold tier
    assert len(backend.list_archived()) == 5


def test_prune_hot_tier_zero_retention_evicts_all(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    backend = FilesystemArchive(tmp_path / "cold")
    mig = TieredArchiveMigrator(
        live_path=live, backend=backend,
        policy=ArchivePolicy(hot_retention_segments=0),
    )
    live.write_text("a")
    mig.rotate_now()
    mig.archive_pending()
    mig.prune_hot_tier()
    assert list(tmp_path.glob("audit.*.jsonl")) == []
    assert len(backend.list_archived()) == 1


# ── Lifecycle ─────────────────────────────────────────────────────────


def test_start_stop_cleanly(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.write_text("seed\n")
    mig = TieredArchiveMigrator(
        live_path=live, backend=FilesystemArchive(tmp_path / "cold"),
        policy=ArchivePolicy(
            rotate_bytes=1, rotate_seconds=3600, poll_seconds=0.05,
            hot_retention_segments=10,
        ),
    )
    mig.start()
    time.sleep(0.20)
    mig.stop(timeout_sec=2.0)
    state = mig.state()
    # Background loop should have rotated + archived at least one segment.
    assert len(state.rotated) >= 1


# ── Cross-tier replay (encrypted journal) ─────────────────────────────


def test_archived_segment_remains_decryptable(tmp_path: Path) -> None:
    """After rotation + archive, the encrypted journal records in the
    cold-tier file are still decryptable with the same data key."""
    live = tmp_path / "audit.jsonl"
    key = _key()
    journal = EncryptedJournal(live, data_key=key)
    for i in range(3):
        journal.append(_record(i))

    backend = FilesystemArchive(tmp_path / "cold")
    mig = TieredArchiveMigrator(live_path=live, backend=backend)
    rotated = mig.rotate_now()
    assert rotated is not None
    mig.archive_pending()

    # Read back from cold tier
    archived_path = next(iter(backend.cold_dir.iterdir()))
    j2 = EncryptedJournal(archived_path, data_key=key)
    records = [r for r in j2.iter_records() if "_decrypt_error" not in r]
    assert len(records) == 3
    assert {r["payload"]["i"] for r in records} == {0, 1, 2}


def test_state_introspection(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.write_text("a")
    mig = TieredArchiveMigrator(
        live_path=live, backend=FilesystemArchive(tmp_path / "cold"),
    )
    mig.rotate_now()
    state = mig.state()
    assert state.live_path == live
    assert len(state.rotated) == 1
    assert state.last_rotation_ns > 0
