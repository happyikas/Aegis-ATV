"""Tiered archive for the encrypted ATV journal (v3.9).

WHITEPAPER_PERFORMANCE_KR.md §2 pattern B (tiered durability):

    hot tier (NVMe, 1ms)     warm tier (replica, 100ms)     cold tier (S3, hours)
    ┌─────────────┐          ┌─────────────┐                ┌─────────────┐
    │ append()    │ ─batch─► │ rotated     │ ───archive──► │ object store │
    │ → journal   │          │ files       │                │ (versioned) │
    └─────────────┘          └─────────────┘                └─────────────┘

The encrypted journal already produces append-only JSONL files. v3.9
adds:

* **Rotation** — split the live file into segments (size or time
  based) so older segments can be archived without locking the writer.
* **Archive backend** abstraction — push closed segments off the hot
  tier. The default backend is filesystem (move into ``cold_dir/``);
  the ``S3Archive`` stub shows how to plug a real object store.
* **Local retention policy** — keep the last K archived segments on
  the hot tier for fast replay, delete older ones once the cold copy
  is durable.

What this is NOT
----------------
* It does **not** add a new audit primitive — the on-disk format,
  encryption, commitment, and Merkle chain are unchanged.
* It does **not** require T3 hardware. T3 (M19+) will swap the
  filesystem backend for a CSD-backed durable region.
* It does **not** fork existing tests — the live writer keeps writing
  to ``journal_path``; rotation happens out-of-band.

Patent linkage
--------------
Whitepaper Claim 47 (cross-tenant federation) builds on this
infrastructure: cold-tier segments tagged by ``batch_key`` cohort can
be cross-rented between tenants under the cost-attestation key
(Claim 34).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ArchivePolicy:
    """Tunables for the rotation + archive cadence."""

    # Rotate the live file when its size exceeds this many bytes.
    rotate_bytes: int = 100 * 1024 * 1024  # 100 MB
    # Rotate after this many seconds even if size threshold not hit.
    rotate_seconds: float = 3600.0  # 1 hour
    # Keep the last N rotated segments on the hot tier even after
    # they're archived. Older ones are deleted from hot tier.
    hot_retention_segments: int = 3
    # How often the migrator wakes up to look for rotation triggers.
    poll_seconds: float = 10.0


# ─────────────────────────────────────────────────────────────────────
# Backend protocol
# ─────────────────────────────────────────────────────────────────────


class ArchiveBackend(ABC):
    """Pluggable cold-tier sink. Implementations must be idempotent —
    archiving the same segment twice is a no-op."""

    @abstractmethod
    def archive(self, segment_path: Path) -> str:
        """Move/copy ``segment_path`` to the cold tier. Returns a
        backend-specific identifier (path, S3 key, etc.) so the
        caller can record where the segment landed."""

    @abstractmethod
    def list_archived(self) -> list[str]:
        """Return the identifiers of every successfully archived segment."""


class FilesystemArchive(ArchiveBackend):
    """Default backend: copy the segment to ``cold_dir/`` and return
    the new absolute path. Suitable for single-host T2 deployments,
    NFS-mounted enterprise NAS, and CI tests."""

    def __init__(self, cold_dir: Path) -> None:
        self.cold_dir = cold_dir
        self.cold_dir.mkdir(parents=True, exist_ok=True)

    def archive(self, segment_path: Path) -> str:
        target = self.cold_dir / segment_path.name
        # shutil.copy2 preserves timestamps; we copy first then delete
        # so a crash mid-way leaves the data on the hot tier.
        shutil.copy2(segment_path, target)
        # fsync the directory to make the new file visible across reboots.
        with target.open("rb") as f:
            os.fsync(f.fileno())
        return str(target.resolve())

    def list_archived(self) -> list[str]:
        return sorted(str(p.resolve()) for p in self.cold_dir.iterdir() if p.is_file())


class S3ArchiveStub(ArchiveBackend):
    """Reference contract for an S3 / GCS / Azure Blob backend.

    Production: import boto3 here, accept a bucket + key prefix in
    the constructor, and use put_object with server-side encryption
    + checksum. We don't ship a working implementation in v3.9 to
    keep the dependency surface minimal — the unit tests target the
    filesystem backend.
    """

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self._archived: list[str] = []

    def archive(self, segment_path: Path) -> str:  # pragma: no cover — stub
        # Pseudo-code:
        #     boto3.client("s3").upload_file(
        #         str(segment_path),
        #         self.bucket,
        #         f"{self.prefix}/{segment_path.name}",
        #         ExtraArgs={"ServerSideEncryption": "AES256"},
        #     )
        key = f"s3://{self.bucket}/{self.prefix}/{segment_path.name}"
        self._archived.append(key)
        return key

    def list_archived(self) -> list[str]:  # pragma: no cover — stub
        return list(self._archived)


# ─────────────────────────────────────────────────────────────────────
# Migrator
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _SegmentInfo:
    path: Path
    rotated_ns: int
    archive_id: str | None = None  # populated after successful archive


@dataclass
class MigratorState:
    """Public introspection — useful for tests and operators."""

    live_path: Path
    rotated: list[Path] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    last_rotation_ns: int = 0


class TieredArchiveMigrator:
    """Background coordinator that rotates the live journal file and
    pushes closed segments to a cold backend.

    The migrator does NOT write new records — it only renames + moves
    files that the encrypted journal has already produced. The journal
    keeps writing to ``live_path``; on rotation we rename the current
    file out of the way (e.g. to ``audit_encrypted.0001.jsonl``) and
    the journal opens a new ``live_path`` on the next ``append`` call.
    """

    def __init__(
        self,
        *,
        live_path: Path,
        backend: ArchiveBackend,
        policy: ArchivePolicy | None = None,
    ) -> None:
        self.live_path = live_path
        self.backend = backend
        self.policy = policy or ArchivePolicy()
        self._lock = threading.Lock()
        self._segments: list[_SegmentInfo] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_rotation_ns: int = time.time_ns()

    # ── Rotation ─────────────────────────────────────────────────────

    def _next_segment_index(self) -> int:
        existing = [
            int(p.stem.rsplit(".", 1)[-1])
            for p in self.live_path.parent.glob(f"{self.live_path.stem}.*.jsonl")
            if p.stem.rsplit(".", 1)[-1].isdigit()
        ]
        return (max(existing) + 1) if existing else 1

    def rotate_now(self) -> Path | None:
        """Rotate the current live file out of the way. Returns the
        rotated path (or None if the live file is empty / missing)."""
        with self._lock:
            if not self.live_path.exists() or self.live_path.stat().st_size == 0:
                return None
            idx = self._next_segment_index()
            rotated = self.live_path.with_suffix(f".{idx:04d}.jsonl")
            self.live_path.rename(rotated)
            # The journal will lazily re-open ``live_path`` on the next append.
            self._segments.append(
                _SegmentInfo(path=rotated, rotated_ns=time.time_ns()),
            )
            self._last_rotation_ns = time.time_ns()
            return rotated

    def _should_rotate(self) -> bool:
        if not self.live_path.exists():
            return False
        try:
            size = self.live_path.stat().st_size
        except FileNotFoundError:
            return False
        if size >= self.policy.rotate_bytes:
            return True
        elapsed = (time.time_ns() - self._last_rotation_ns) / 1e9
        return elapsed >= self.policy.rotate_seconds and size > 0

    # ── Archive ──────────────────────────────────────────────────────

    def archive_pending(self) -> list[_SegmentInfo]:
        """Push every un-archived segment to the cold backend.
        Idempotent — already-archived segments are skipped."""
        archived_now: list[_SegmentInfo] = []
        with self._lock:
            pending = [s for s in self._segments if s.archive_id is None]
        for seg in pending:
            try:
                identifier = self.backend.archive(seg.path)
            except Exception:
                # Leave seg.archive_id None; next tick will retry.
                continue
            with self._lock:
                seg.archive_id = identifier
                archived_now.append(seg)
        return archived_now

    def prune_hot_tier(self) -> list[Path]:
        """Delete archived segments from the hot tier once we've kept
        the configured retention. Returns paths that were deleted."""
        deleted: list[Path] = []
        with self._lock:
            archived = [s for s in self._segments if s.archive_id is not None]
            # Sort by rotation time ascending; oldest first
            archived.sort(key=lambda s: s.rotated_ns)
            keep = self.policy.hot_retention_segments
            to_delete = archived[:-keep] if keep > 0 else archived
            for seg in to_delete:
                try:
                    if seg.path.exists():
                        seg.path.unlink()
                        deleted.append(seg.path)
                except OSError:
                    continue
                # Drop fully-evicted segments from our bookkeeping
                self._segments.remove(seg)
        return deleted

    # ── Lifecycle ────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.policy.poll_seconds)
            if self._stop_event.is_set():
                break
            try:
                if self._should_rotate():
                    self.rotate_now()
                self.archive_pending()
                self.prune_hot_tier()
            except Exception:
                # Don't kill the daemon thread on transient errors.
                continue
        # Final pass on shutdown
        with contextlib.suppress(Exception):
            self.archive_pending()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="aegis-tiered-archive", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_sec: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout_sec)
        self._thread = None

    # ── Introspection ────────────────────────────────────────────────

    def state(self) -> MigratorState:
        with self._lock:
            return MigratorState(
                live_path=self.live_path,
                rotated=[s.path for s in self._segments],
                archived=[s.archive_id for s in self._segments if s.archive_id],
                last_rotation_ns=self._last_rotation_ns,
            )


__all__ = [
    "ArchiveBackend",
    "ArchivePolicy",
    "FilesystemArchive",
    "MigratorState",
    "S3ArchiveStub",
    "TieredArchiveMigrator",
]
