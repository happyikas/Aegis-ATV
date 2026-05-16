"""ContextMemory rotation — size-based, gzip-compressed, with retention.

Why ContextMemory needs its own rotation
----------------------------------------

`~/.aegis/context_memory.jsonl` is the near-storage analytics layer
that backs `aegis doctor`, `aegis advise`, `aegis live`, and `aegis
memory claude-md`. Every PreToolUse hook writes one row, so heavy
sessions accumulate ~3 MB / day on a typical Claude Code workload.
Without rotation:

* the file grows unbounded — disk fill on multi-month users;
* `read_window()` walks every line on every query — O(N) linear
  scan slows as the file grows;
* `aegis memory show` reports a single ever-growing record count
  with no way to bound retention.

Unlike the audit chain (`src/aegis/audit/rotation.py`), ContextMemory
**does not need cross-file hash continuity**. ContextMemory is
analytics-only — losing the oldest records doesn't break a
cryptographic proof. That lets this module be much simpler than the
audit-side equivalent: rotate when over threshold, archive to .gz,
drop the oldest slot beyond retention. No GENESIS fallthrough, no
chain walker, no signature carry-over.

Storage layout
--------------
* Active:   `context_memory.jsonl`            (plain text, append-only)
* Rotated:  `context_memory.jsonl.1.gz` …     `.{K}.gz`  (gzip)

Slot 1 is the most-recent rotation; slot K is the oldest retained.
Compression typically shrinks the JSONL by 5–10× — repeated keys,
boolean/None fields all compress hard.

Configuration
-------------
* `AEGIS_CONTEXT_MEMORY_MAX_BYTES`     — size trigger (default
                                          50 MB; 0 = disable size
                                          rotation).
* `AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS` — keep last K (default 5;
                                          0 = disable rotation
                                          entirely).
* `AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED` — set to `1` to
                                          unconditionally suppress
                                          rotation regardless of
                                          the values above. Useful
                                          for tests + ephemeral
                                          CI runs.

Defensive contract
------------------
This module follows the same "analytics never blocks the verdict
path" rule as `writer.append()`: every rotation step swallows
`OSError` and returns gracefully. A failed rotation leaves the
active file intact and the next append proceeds normally — the
operator notices via `aegis memory show` reporting a stagnant
file size, not via a Claude Code session breaking.
"""

from __future__ import annotations

import contextlib
import gzip
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

DEFAULT_MAX_BYTES = 50 * 1024 * 1024     # 50 MB
DEFAULT_MAX_ROTATIONS = 5                # keep .1.gz .. .5.gz
COMPRESSION_SUFFIX = ".gz"


# ── configuration ──────────────────────────────────────────────────


def rotation_disabled() -> bool:
    """`True` if rotation is unconditionally suppressed via env."""
    raw = os.environ.get(
        "AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def max_bytes() -> int:
    """Threshold above which the active store is rotated. 0 = off."""
    raw = os.environ.get("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def max_rotations() -> int:
    """Number of rotated archives kept. 0 = no rotation at all."""
    raw = os.environ.get(
        "AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "",
    ).strip()
    if not raw:
        return DEFAULT_MAX_ROTATIONS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_ROTATIONS


# ── path helpers ───────────────────────────────────────────────────


def compressed_rotation_path(base: Path, n: int) -> Path:
    """Path of the N-th rotated archive: `<base>.<n>.gz`."""
    return base.with_name(f"{base.name}.{n}{COMPRESSION_SUFFIX}")


def slot_path(base: Path, n: int) -> Path | None:
    """Return the existing file at slot `n`, or `None`. Compressed
    slot only — ContextMemory rotations are always gzipped (unlike
    the audit chain, which carries legacy plain-text rotations from
    pre-PR-#26 history)."""
    gz = compressed_rotation_path(base, n)
    return gz if gz.exists() else None


def list_rotation_chain(base: Path) -> list[Path]:
    """All ContextMemory files in chronological (write) order.

    For `base = ~/.aegis/context_memory.jsonl` with rotations 1, 2, 3
    present:

        [...jsonl.3.gz, ...jsonl.2.gz, ...jsonl.1.gz, ...jsonl]

    Order matches append order: oldest first, active last.
    """
    out: list[Path] = []
    for n in range(max_rotations(), 0, -1):
        p = slot_path(base, n)
        if p is not None:
            out.append(p)
    if base.exists():
        out.append(base)
    return out


def open_rotation_text(path: Path) -> Iterator[str]:
    """Yield UTF-8 decoded lines from a rotation file (plain or .gz).

    Errors silently produce no output — readers downstream treat the
    file as empty and move on, matching writer.append()'s defensive
    "analytics writes never block" contract.
    """
    if not path.exists():
        return
    try:
        if path.suffix == COMPRESSION_SUFFIX:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                yield from fh
        else:
            with path.open(encoding="utf-8") as fh:
                yield from fh
    except OSError:
        return


# ── trigger + rotation engine ──────────────────────────────────────


def should_rotate(base: Path) -> bool:
    """`True` iff `base` is over the size threshold AND rotation is
    enabled. Pure function — no side effects."""
    if rotation_disabled() or max_rotations() <= 0:
        return False
    try:
        if not base.is_file():
            return False
        size = base.stat().st_size
    except OSError:
        return False
    cap = max_bytes()
    return cap > 0 and size >= cap


def _shift_slot(base: Path, src_n: int, dst_n: int) -> bool:
    """Move `<base>.<src_n>.gz` → `<base>.<dst_n>.gz`. Returns True
    on success. Silently no-op when the source doesn't exist."""
    src = slot_path(base, src_n)
    if src is None:
        return False
    dst = compressed_rotation_path(base, dst_n)
    try:
        # If something is at the destination (shouldn't happen in
        # normal flow, but defensive), drop it first.
        if dst.exists() and dst != src:
            with contextlib.suppress(OSError):
                dst.unlink()
        src.replace(dst)
        return True
    except OSError:
        return False


def _drop_slot(base: Path, n: int) -> bool:
    """Delete the file at slot `n`. Returns True if something was
    actually removed."""
    p = slot_path(base, n)
    if p is None:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def _gzip_to_slot_1(base: Path) -> bool:
    """Compress the active file to `<base>.1.gz` and delete the
    plain original. Returns True on success.

    On failure (disk full, permission error), the plain active file
    is left in place — caller falls back to "no rotation this time"
    and the next append proceeds normally.
    """
    if not base.is_file():
        return False
    target = compressed_rotation_path(base, 1)
    try:
        with base.open("rb") as src_fh, gzip.open(target, "wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)
    except OSError:
        # Clean up partial output, leave plain alone.
        with contextlib.suppress(OSError):
            target.unlink()
        return False
    # Compression succeeded — drop the plain file.
    try:
        base.unlink()
    except OSError:
        # Edge case: gz exists, plain still there. Next append will
        # land in a fresh `<base>` and slot 1 is the just-rotated
        # archive; the leftover plain at the original path won't be
        # touched. Operators see this via `aegis memory show` and
        # can `rm` it manually.
        return False
    return True


def rotate(base: Path) -> int | None:
    """Perform rotation. Returns the new slot-1 index (always 1) on
    success, or `None` when rotation was skipped or failed.

    Atomic-ish sequence:

    1. Drop slot K (oldest, beyond retention).
    2. For i in [K-1 .. 1]: shift slot i → i+1.
    3. Gzip the active file into slot 1 and delete the plain
       original.

    Concurrent writes during step 3 are tolerated (writer.append()
    opens the file in append mode, so a new append after the gzip
    starts simply lands at the end of the file — the gzip captures
    a prefix and the post-gzip writes go into a fresh empty file
    once we delete the original; the lost lines are bounded by how
    long the gzip takes, typically <1s for 50 MB).
    """
    if rotation_disabled():
        return None
    k = max_rotations()
    if k <= 0:
        return None
    if not base.exists():
        return None

    # Step 1: drop oldest beyond retention.
    _drop_slot(base, k)

    # Step 2: shift everything up one slot. Walk from highest down
    # so we don't overwrite ourselves.
    for i in range(k - 1, 0, -1):
        _shift_slot(base, i, i + 1)

    # Step 3: compress active → slot 1.
    if not _gzip_to_slot_1(base):
        return None

    return 1


def rotate_if_needed(base: Path) -> int | None:
    """`should_rotate` + `rotate` combined. Returns the rotation
    index if a rotation happened, else `None`. Safe to call from the
    writer's hot path: when nothing needs to be done, this is a
    `stat()` call and a return."""
    if not should_rotate(base):
        return None
    return rotate(base)


__all__ = [
    "COMPRESSION_SUFFIX",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ROTATIONS",
    "compressed_rotation_path",
    "list_rotation_chain",
    "max_bytes",
    "max_rotations",
    "open_rotation_text",
    "rotate",
    "rotate_if_needed",
    "rotation_disabled",
    "should_rotate",
    "slot_path",
]
