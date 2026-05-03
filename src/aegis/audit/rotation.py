"""Audit-log rotation — size-based with cross-file SHA3 chain continuity.

PR #26 enriched each PreToolUse audit record with an ``explain`` block
(~500–800 bytes per record). On a heavy Claude Code day (1k tool calls)
that's ~1 MB/day in ``~/.aegis/audit.jsonl``. Without rotation:

* the file grows unbounded — disk fill on multi-month users;
* ``aegis verify-audit`` walks the full chain on every invocation —
  O(N) cost grows linearly with file size;
* ``tail`` / log-grep tooling slows down on multi-100-MB files.

This module rotates ``audit.jsonl`` to ``audit.jsonl.1`` once it
exceeds a threshold (default 50 MB), keeping the most-recent K
rotations (default 10). Older rotations are deleted opportunistically
on rotation.

Chain continuity across rotation
--------------------------------
The pre-rotation Solo Free contract (``aegis verify-audit`` proves
the JSONL hasn't been mutated post-write) MUST survive rotation.
Approach:

1. **No new GENESIS_HASH on rotation.** When ``audit.jsonl`` is
   renamed to ``audit.jsonl.1``, the *next* append into the new
   ``audit.jsonl`` carries ``prev_hash = last_hash(audit.jsonl.1)``
   so the chain is unbroken across the file boundary.
2. **`_last_hash` falls through to the most-recent rotation.** When
   the current file is empty/missing (just-rotated state),
   ``_last_hash`` reads from ``audit.jsonl.1`` instead of returning
   GENESIS_HASH.
3. **`verify_chain` walks the rotation set in chronological order.**
   ``audit.jsonl.K`` (oldest) → ... → ``audit.jsonl.1`` → ``audit.jsonl``.
   A break in any file is reported with a global record index.

This means a fresh Solo Free user's audit log is one continuous chain
from their first tool call, even after years of operation and many
rotations.

Configuration
-------------
* ``AEGIS_AUDIT_MAX_BYTES``      — rotation trigger (default 50 MB)
* ``AEGIS_AUDIT_MAX_ROTATIONS``  — keep last K (default 10)

Set either to ``0`` to disable rotation entirely (back to v0
behaviour: unbounded file).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

DEFAULT_MAX_BYTES = 50 * 1024 * 1024     # 50 MB
DEFAULT_MAX_ROTATIONS = 10               # keep .1 .. .10
ROTATION_SUFFIX = ".jsonl"               # the base file's suffix


def max_bytes() -> int:
    """Threshold above which the active log is rotated. 0 = disabled."""
    raw = os.environ.get("AEGIS_AUDIT_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def max_rotations() -> int:
    """Number of rotated files to keep. 0 = disable rotation."""
    raw = os.environ.get("AEGIS_AUDIT_MAX_ROTATIONS", "").strip()
    if not raw:
        return DEFAULT_MAX_ROTATIONS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_ROTATIONS


def rotation_path(base: Path, n: int) -> Path:
    """Path of the N-th rotation. ``n=1`` is most-recent rotation."""
    return base.with_name(f"{base.name}.{n}")


def list_rotation_chain(base: Path) -> list[Path]:
    """All log files in chronological (write) order — oldest first.

    For ``base = ~/.aegis/audit.jsonl`` and rotations 1, 2, 3 present:

        [audit.jsonl.3, audit.jsonl.2, audit.jsonl.1, audit.jsonl]

    The order matches the order in which records were appended, so a
    consumer that walks ``[f for f in chain]`` reading line-by-line
    sees the global audit timeline reconstructed.

    Files that don't exist are skipped — verifier-friendly.
    """
    out: list[Path] = []
    # Rotated files: walk from highest number (oldest) down to 1
    for n in range(max_rotations(), 0, -1):
        p = rotation_path(base, n)
        if p.exists():
            out.append(p)
    # Active file last (newest)
    if base.exists():
        out.append(base)
    return out


def should_rotate(base: Path) -> bool:
    """True iff ``base`` exists and exceeds the configured threshold."""
    cap = max_bytes()
    if cap <= 0 or max_rotations() <= 0:
        return False
    try:
        return base.is_file() and base.stat().st_size >= cap
    except OSError:
        return False


def rotate(base: Path) -> int:
    """Perform rotation atomically. Returns new max rotation index used.

    Steps:

    1. Drop ``audit.jsonl.K`` if it exists (oldest beyond retention).
    2. For ``i`` in ``K-1 .. 1``:
       rename ``audit.jsonl.i`` → ``audit.jsonl.{i+1}``
    3. Rename ``audit.jsonl`` → ``audit.jsonl.1``.

    After rotation, the active path doesn't exist — the next
    :func:`local_chain.append` call recreates it. The rotation routine
    NEVER raises on partial failure — ``OSError`` is swallowed and the
    function returns the highest successfully-rotated index. Callers
    in the hot path (the local hook) treat rotation as best-effort.

    Returns 0 if rotation was disabled or skipped (file missing /
    threshold not exceeded). Returns the new top index (1..K) on
    successful rotation.
    """
    keep = max_rotations()
    if keep <= 0:
        return 0
    if not base.is_file():
        return 0

    # 1. Drop the oldest rotation if at capacity.
    oldest = rotation_path(base, keep)
    if oldest.exists():
        with contextlib.suppress(OSError):
            oldest.unlink()

    # 2. Shift each rotation up one slot. Walk backwards so we never
    #    overwrite a file we still need.
    for i in range(keep - 1, 0, -1):
        src = rotation_path(base, i)
        dst = rotation_path(base, i + 1)
        if src.exists():
            with contextlib.suppress(OSError):
                src.replace(dst)

    # 3. Move the active file into the .1 slot.
    try:
        base.replace(rotation_path(base, 1))
    except OSError:
        return 0
    return 1


def maybe_rotate(base: Path) -> int:
    """Combined check + rotate. Returns the new top index, 0 if no-op."""
    if not should_rotate(base):
        return 0
    return rotate(base)


def total_size(base: Path) -> int:
    """Sum of bytes across the active file + all rotations."""
    return sum(p.stat().st_size for p in list_rotation_chain(base) if p.is_file())


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ROTATIONS",
    "list_rotation_chain",
    "max_bytes",
    "max_rotations",
    "maybe_rotate",
    "rotate",
    "rotation_path",
    "should_rotate",
    "total_size",
]
