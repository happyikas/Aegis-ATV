"""Audit-log rotation — size + time-based, gzip-compressed, with
cross-file SHA3 chain continuity.

PR #26 enriched each PreToolUse audit record with an ``explain`` block
(~500–800 bytes per record). On a heavy Claude Code day (1k tool calls)
that's ~1 MB/day in ``~/.aegis/audit.jsonl``. Without rotation:

* the file grows unbounded — disk fill on multi-month users;
* ``aegis verify-audit`` walks the full chain on every invocation —
  O(N) cost grows linearly with file size;
* ``tail`` / log-grep tooling slows down on multi-100-MB files.

This module rotates ``audit.jsonl`` to ``audit.jsonl.1.gz`` once it
exceeds a threshold (default 50 MB) or once a configured time-based
trigger fires (e.g. once per day). The most-recent K rotations are
kept (default 10); older rotations are deleted opportunistically on
rotation.

Storage layout
--------------
* Active:    ``audit.jsonl``           (plain text, append-only)
* Rotated:   ``audit.jsonl.1.gz`` …    ``audit.jsonl.K.gz``  (gzip)

Slot 1 is the most-recent rotation; slot K is the oldest retained.
Compression typically shrinks JSONL audit records by 5–10× (the records
are highly compressible — repeated keys, ATV-2080-v1 vector subfields
mostly zeros). 50 MB plain → ~6 MB compressed per slot.

Backwards compat: legacy uncompressed ``audit.jsonl.N`` files (from
rotations performed before this PR) are still readable. They get
shifted up the slot chain on each new rotation; once they age out of
the retention window they're deleted. New rotations always produce
``.gz``. No explicit migration step required.

Chain continuity across rotation
--------------------------------
The Solo Free contract (``aegis verify-audit`` proves the JSONL hasn't
been mutated post-write) MUST survive rotation:

1. **No new GENESIS_HASH on rotation.** When ``audit.jsonl`` is
   compressed to ``audit.jsonl.1.gz``, the *next* append into the new
   ``audit.jsonl`` carries ``prev_hash = last_hash(audit.jsonl.1.gz)``
   so the chain is unbroken across the file boundary.
2. **`_last_hash` falls through to the most-recent rotation.** When
   the current file is empty/missing (just-rotated state),
   ``_last_hash`` reads from ``audit.jsonl.1.gz`` (or ``.1`` legacy)
   instead of returning GENESIS_HASH.
3. **`verify_chain` walks the rotation set in chronological order.**
   A break in any file is reported with a global record index.

This means a Solo Free user's audit log is one continuous chain from
their first tool call, even after years of operation, many rotations,
and the schema bump to compressed rotations.

Configuration
-------------
* ``AEGIS_AUDIT_MAX_BYTES``      — size trigger (default 50 MB; 0 = off)
* ``AEGIS_AUDIT_MAX_ROTATIONS``  — keep last K (default 10; 0 = no rotation)
* ``AEGIS_AUDIT_ROTATE_DAILY``   — also rotate when the active file's
                                   first record is from a previous
                                   calendar day (UTC). Default off
                                   (set to ``"1"`` / ``"true"`` /
                                   ``"yes"`` to enable).
"""

from __future__ import annotations

import contextlib
import gzip
import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MAX_BYTES = 50 * 1024 * 1024     # 50 MB
DEFAULT_MAX_ROTATIONS = 10               # keep .1 .. .10
ROTATION_SUFFIX = ".jsonl"               # the base file's suffix
COMPRESSION_SUFFIX = ".gz"


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


def rotate_daily() -> bool:
    """``True`` if the time-based daily rotation trigger is on.

    Off by default (size-based rotation alone is enough for most
    operators). Enable with ``AEGIS_AUDIT_ROTATE_DAILY=1`` so each new
    UTC day starts a fresh rotation slot — useful when an operator
    wants per-day audit files for ingestion into an external SIEM.
    """
    raw = os.environ.get("AEGIS_AUDIT_ROTATE_DAILY", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def rotation_path(base: Path, n: int) -> Path:
    """Path of the N-th rotation, **legacy uncompressed** form.

    Kept stable for backwards compatibility — pre-compression rotations
    used this name. New rotations produce :func:`compressed_rotation_path`
    instead, but readers still check this path so legacy files keep
    working until they age out of retention.
    """
    return base.with_name(f"{base.name}.{n}")


def compressed_rotation_path(base: Path, n: int) -> Path:
    """Path of the N-th rotation in the compressed form."""
    return base.with_name(f"{base.name}.{n}{COMPRESSION_SUFFIX}")


def slot_path(base: Path, n: int) -> Path | None:
    """Return the path actually present at slot ``n``, or ``None``.

    Prefers the compressed form when both exist (defensive — the post-
    PR contract is that slot N is compressed; if both exist, ``.gz``
    is the canonical one and the plain file is leftover from a
    crashed rotation). Returns ``None`` when neither file exists.
    """
    gz = compressed_rotation_path(base, n)
    if gz.exists():
        return gz
    plain = rotation_path(base, n)
    if plain.exists():
        return plain
    return None


def is_compressed(path: Path) -> bool:
    """``True`` if ``path`` is a gzip-compressed rotation file."""
    return path.suffix == COMPRESSION_SUFFIX


def open_rotation_text(path: Path) -> Iterator[str]:
    """Yield decoded UTF-8 lines from a rotation file (plain or gzip).

    Caller iterates; the underlying file/decoder closes when the
    iterator is exhausted or garbage-collected. Use as::

        for line in open_rotation_text(slot_path(base, 1) or active):
            rec = json.loads(line)
            ...

    Errors silently produce no output — readers in the verify-audit
    walker treat that as "empty file" and move on, which preserves
    the best-effort contract on disk failures.
    """
    if not path.exists():
        return
    try:
        if is_compressed(path):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                yield from fh
        else:
            with path.open(encoding="utf-8") as fh:
                yield from fh
    except OSError:
        return


def list_rotation_chain(base: Path) -> list[Path]:
    """All log files in chronological (write) order — oldest first.

    For ``base = ~/.aegis/audit.jsonl`` and rotations 1, 2, 3 present:

        [audit.jsonl.3.gz, audit.jsonl.2.gz, audit.jsonl.1.gz, audit.jsonl]

    The order matches the order in which records were appended, so a
    consumer that walks ``[f for f in chain]`` reading line-by-line
    sees the global audit timeline reconstructed. Each entry is the
    path that's actually present (compressed or legacy plain).

    Files that don't exist are skipped — verifier-friendly.
    """
    out: list[Path] = []
    # Rotated files: walk from highest number (oldest) down to 1.
    for n in range(max_rotations(), 0, -1):
        p = slot_path(base, n)
        if p is not None:
            out.append(p)
    # Active file last (newest, plain text)
    if base.exists():
        out.append(base)
    return out


def _first_record_ts_ns(path: Path) -> int | None:
    """Cheapest possible peek at a rotation file's first ``ts_ns``.

    Used by :func:`should_rotate` to decide whether the active file
    crosses a UTC-day boundary. Reads at most one parseable line.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    import json

    for raw in open_rotation_text(path):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return None
        ts = rec.get("ts_ns")
        return int(ts) if isinstance(ts, (int, float, str)) and str(ts).strip().lstrip("-").isdigit() else None
    return None


def should_rotate(base: Path) -> bool:
    """True iff ``base`` exists and crosses any configured trigger.

    Two triggers:

    * **size**: file size ≥ ``AEGIS_AUDIT_MAX_BYTES`` (default 50 MB).
      Always active unless the env var is set to ``0``.
    * **daily** (opt-in via ``AEGIS_AUDIT_ROTATE_DAILY=1``): the active
      file's first record is from a previous UTC day. Only checked
      when the file has at least one record (just-created files don't
      qualify).
    """
    if max_rotations() <= 0:
        return False
    try:
        if not base.is_file():
            return False
        size = base.stat().st_size
    except OSError:
        return False

    cap = max_bytes()
    if cap > 0 and size >= cap:
        return True

    if rotate_daily() and size > 0:
        first_ts_ns = _first_record_ts_ns(base)
        if first_ts_ns is not None:
            first_day = datetime.fromtimestamp(
                first_ts_ns / 1_000_000_000, tz=UTC,
            ).date()
            today = datetime.now(UTC).date()
            if first_day < today:
                return True

    return False


def _shift_one_slot(base: Path, src_n: int, dst_n: int) -> bool:
    """Move whichever of ``.{src_n}`` or ``.{src_n}.gz`` exists to the
    matching ``.{dst_n}`` slot. Compressed → compressed, plain → plain
    (no transcoding mid-shift). Returns True if a file was moved.
    """
    src = slot_path(base, src_n)
    if src is None:
        return False
    if is_compressed(src):
        dst = compressed_rotation_path(base, dst_n)
    else:
        dst = rotation_path(base, dst_n)
    try:
        # If something happens to be at the destination already (e.g.
        # legacy plain file when we have a compressed source), drop
        # it first — the source is canonical for that age.
        existing_at_dst = slot_path(base, dst_n)
        if existing_at_dst is not None and existing_at_dst != src:
            with contextlib.suppress(OSError):
                existing_at_dst.unlink()
        src.replace(dst)
        return True
    except OSError:
        return False


def _drop_slot(base: Path, n: int) -> bool:
    """Delete whichever file is at slot ``n`` (.gz or plain). Returns
    True if something was removed."""
    p = slot_path(base, n)
    if p is None:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def _gzip_in_place(plain: Path) -> Path | None:
    """Compress ``plain`` to ``plain.gz`` and delete the original.

    Returns the new compressed path on success, ``None`` on any
    failure (caller should leave the plain file alone in that case).
    """
    if not plain.is_file():
        return None
    target = plain.with_name(plain.name + COMPRESSION_SUFFIX)
    try:
        with plain.open("rb") as src_fh, gzip.open(target, "wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)
    except OSError:
        # Compression failed — clean up partial output, keep plain.
        with contextlib.suppress(OSError):
            target.unlink()
        return None
    with contextlib.suppress(OSError):
        plain.unlink()
    return target


def rotate(base: Path) -> int:
    """Perform rotation atomically. Returns new max rotation index used.

    Updated steps (from this PR onwards rotated slots are gzipped):

    1. Drop ``audit.jsonl.K`` AND ``audit.jsonl.K.gz`` if either
       exists (oldest beyond retention).
    2. For ``i`` in ``K-1 .. 1``: shift slot i → i+1, preserving the
       file's existing format (legacy plain stays plain on the way
       up; new .gz stays .gz).
    3. Rename ``audit.jsonl`` → ``audit.jsonl.1`` (still plain at
       this point), then gzip it in place to ``audit.jsonl.1.gz``.

    After rotation, the active path doesn't exist — the next
    :func:`local_chain.append` call recreates it. The rotation routine
    NEVER raises on partial failure — ``OSError`` is swallowed and the
    function returns 1 if rotation succeeded (the active was at least
    moved into slot 1, even if the gzip step failed and slot 1 stays
    plain).

    Returns 0 if rotation was disabled or skipped (file missing /
    retention=0). Returns 1 on successful rotation (the active file
    is now at slot 1, and slots 2..K may have shifted).
    """
    keep = max_rotations()
    if keep <= 0:
        return 0
    if not base.is_file():
        return 0

    # 1. Drop the oldest rotation if at capacity.
    _drop_slot(base, keep)

    # 2. Shift each rotation up one slot. Walk backwards so we never
    #    overwrite a file we still need.
    for i in range(keep - 1, 0, -1):
        _shift_one_slot(base, i, i + 1)

    # 3. Move the active file into the .1 slot (still uncompressed),
    #    then compress in place to .1.gz. If the gzip step fails for
    #    any reason (disk full, weird FS), the plain .1 file remains
    #    valid and readable — our slot_path / open_rotation_text
    #    helpers handle either form.
    plain_slot1 = rotation_path(base, 1)
    try:
        base.replace(plain_slot1)
    except OSError:
        return 0
    _gzip_in_place(plain_slot1)
    return 1


def maybe_rotate(base: Path) -> int:
    """Combined check + rotate. Returns the new top index, 0 if no-op."""
    if not should_rotate(base):
        return 0
    return rotate(base)


def prune(base: Path, *, keep: int) -> list[Path]:
    """Drop rotations beyond ``keep``. Returns list of paths removed.

    Operator-facing surface for ``aegis audit prune --keep N``. Unlike
    :func:`rotate`, this does NOT shift slots — it just deletes the
    files at slots ``keep+1 .. max_rotations()``. Useful when an
    operator wants to free space immediately (e.g. before disk runs
    out) without waiting for the next size-triggered rotation.

    ``keep=0`` removes every rotation; the active file is never
    touched. Negative values are clamped to 0.
    """
    keep = max(0, keep)
    removed: list[Path] = []
    # Walk a generous window — beyond max_rotations() in case the env
    # was tightened mid-life and there are stranded slots above K.
    horizon = max(max_rotations(), keep) + 50
    for n in range(keep + 1, horizon + 1):
        p = slot_path(base, n)
        if p is None:
            continue
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            pass
    return removed


def total_size(base: Path) -> int:
    """Sum of bytes across the active file + all rotations."""
    return sum(p.stat().st_size for p in list_rotation_chain(base) if p.is_file())


def status(base: Path) -> dict[str, object]:
    """Operator-friendly summary for ``aegis audit status``.

    Returns a dict with current usage + thresholds + per-slot details.
    Dict shape is stable for ``--json`` consumers (jq, fleet-monitor):

        {
          "active_path": "<absolute path>",
          "active_bytes": <int>,
          "active_exists": <bool>,
          "threshold_bytes": <int>,
          "max_rotations": <int>,
          "rotate_daily": <bool>,
          "total_bytes": <int>,
          "rotation_slots": [
            {"n": 1, "path": "...", "compressed": <bool>, "bytes": <int>},
            ...
          ]
        }
    """
    slots: list[dict[str, object]] = []
    for n in range(1, max_rotations() + 1):
        p = slot_path(base, n)
        if p is None:
            continue
        slots.append({
            "n": n,
            "path": str(p),
            "compressed": is_compressed(p),
            "bytes": p.stat().st_size if p.is_file() else 0,
        })
    return {
        "active_path": str(base),
        "active_bytes": base.stat().st_size if base.is_file() else 0,
        "active_exists": base.is_file(),
        "threshold_bytes": max_bytes(),
        "max_rotations": max_rotations(),
        "rotate_daily": rotate_daily(),
        "total_bytes": total_size(base),
        "rotation_slots": slots,
    }


__all__ = [
    "COMPRESSION_SUFFIX",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ROTATIONS",
    "compressed_rotation_path",
    "is_compressed",
    "list_rotation_chain",
    "max_bytes",
    "max_rotations",
    "maybe_rotate",
    "open_rotation_text",
    "prune",
    "rotate",
    "rotate_daily",
    "rotation_path",
    "should_rotate",
    "slot_path",
    "status",
    "total_size",
]
