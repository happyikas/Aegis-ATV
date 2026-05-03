"""Local-mode audit chain (v2.1.5, Day-1 #8).

Sidecar mode signs every audit record with Ed25519 + Merkle (M5/M9/M15).
Local mode (Solo Free, in-process plugin) ships without that
cryptographic stack to keep the install footprint minimal — but the
audit log was previously plain-text JSONL with no integrity proof.

This module adds a deterministic SHA3-256 hash chain. Each appended
record carries:

* ``prev_hash``  — the ``this_hash`` of the previous line (or
                   :data:`GENESIS_HASH` for the first line).
* ``this_hash``  — SHA3-256 over the canonical-JSON of the record
                   *minus* ``this_hash`` itself.

Tampering with any historical line breaks every subsequent
``this_hash``. :func:`verify_chain` walks the file and reports the
first break it finds, returning ``(ok, broken_index, total)``.

Why not Ed25519? — local mode is single-developer, no key management.
The chain alone proves the log hasn't been mutated post-write; the
threat model is "did a process modify lines after the hook wrote
them" not "did an authenticated principal sign these". Sidecar mode
remains the path for cryptographic non-repudiation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def _hash_record(prev_hash: str, record: dict[str, Any]) -> str:
    payload = {"prev_hash": prev_hash, **{k: v for k, v in record.items() if k != "this_hash"}}
    return hashlib.sha3_256(_canonical_json(payload)).hexdigest()


def _last_hash_in_file(path: Path) -> str | None:
    """Tail-read one file. Returns its last ``this_hash`` or None.

    Cheap: reads the last 4 KB and parses the trailing JSON line.
    Returns ``None`` (not GENESIS_HASH) when the file is missing,
    empty, or has no parseable trailing record — :func:`_last_hash`
    uses None to fall through to the next-most-recent rotation.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(-4096, 2)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", errors="replace")
        for raw in reversed(tail.splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = rec.get("this_hash")
            if isinstance(h, str) and h:
                return h
    except OSError:
        pass
    return None


def _last_hash(path: Path) -> str:
    """Return the ``this_hash`` of the last record, with rotation fallback.

    The chain crosses file boundaries: if the active file is empty (we
    just rotated, or this is the first call ever), we fall back to
    ``audit.jsonl.1`` so the new file's first record carries a
    ``prev_hash`` matching the rotated file's last record. Without
    this, every rotation would inject a GENESIS_HASH break and
    ``aegis verify-audit`` would fail at the file boundary.
    """
    h = _last_hash_in_file(path)
    if h is not None:
        return h

    # Active file is empty / missing → look at the most-recent rotation.
    from aegis.audit.rotation import rotation_path

    fallback = rotation_path(path, 1)
    h = _last_hash_in_file(fallback)
    return h if h is not None else GENESIS_HASH


def append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append ``record`` with a chained ``prev_hash`` + ``this_hash``.

    Opportunistic size-based rotation: if the file exceeds the
    configured threshold we rotate it (``audit.jsonl`` →
    ``audit.jsonl.1``) before appending. The new file's first record
    inherits ``prev_hash`` from the just-rotated file, so the chain
    stays unbroken across the file boundary.

    Returns the augmented record (caller can inspect it). Audit
    failures raise ``OSError`` — callers in the hot path should wrap
    this in try/except so a write error never blocks the tool call.
    Rotation failures are swallowed (best-effort) — better to keep
    appending to a too-large file than to drop the audit record.
    """
    # Rotate if file is over threshold. maybe_rotate() is a no-op
    # when AEGIS_AUDIT_MAX_BYTES=0 or the file is below threshold.
    try:
        from aegis.audit.rotation import maybe_rotate
        maybe_rotate(path)
    except Exception:  # noqa: BLE001 — rotation is best-effort
        pass

    prev_hash = _last_hash(path)
    chained = {**record, "prev_hash": prev_hash}
    chained["this_hash"] = _hash_record(prev_hash, chained)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(chained, default=str) + "\n")
    return chained


def verify_chain(path: Path) -> tuple[bool, int, int]:
    """Walk the full audit chain end-to-end. ``(ok, broken_at_index, total)``.

    Walks rotated files first (oldest → newest) then the active file,
    so the chain is verified across rotation boundaries. The hash
    handoff is implicit: the last record of ``audit.jsonl.K`` provides
    ``prev_hash`` for the first record of ``audit.jsonl.{K-1}``, since
    rotation preserves the hash chain via :func:`_last_hash`.

    Retention-eviction handling
    ---------------------------
    When ``AEGIS_AUDIT_MAX_ROTATIONS`` policy has dropped the oldest
    file(s), the oldest *retained* record's ``prev_hash`` won't equal
    ``GENESIS_HASH`` — it points at a record that's been evicted. We
    treat the first retained record's ``prev_hash`` as a **trust
    anchor** and verify chain integrity forward from there. The
    function still reports total records walked, but the verification
    proves "the retained portion has not been mutated" rather than
    "the chain extends back to genesis".

    * ``ok`` is True iff every line's ``this_hash`` matches the recompute
      AND every ``prev_hash`` chains to the prior line's ``this_hash``
      across all retained rotation files.
    * ``broken_at_index`` is the global 0-indexed position of the first
      broken line (counting across rotations), or -1 if ok.
    * ``total`` counts non-blank, non-malformed lines walked across
      the retained rotation set.
    """
    from aegis.audit.rotation import list_rotation_chain

    files = list_rotation_chain(path)
    if not files:
        return True, -1, 0

    # Anchor at the first retained record's prev_hash. If retention
    # has dropped earlier files, this is just our trust point; if no
    # eviction has happened it'll be GENESIS_HASH automatically.
    anchor = _first_record_prev_hash(files[0]) or GENESIS_HASH

    expected_prev = anchor
    total = 0
    for f in files:
        ok, broken, n_in_file = _verify_file_chain(f, expected_prev, total)
        total += n_in_file
        if not ok:
            return False, broken, total
        # The last record's this_hash becomes the next file's expected prev.
        last = _last_hash_in_file(f)
        if last is not None:
            expected_prev = last
    return True, -1, total


def _first_record_prev_hash(path: Path) -> str | None:
    """Read the very first record's ``prev_hash`` (cheap — one line)."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    return None
                p = rec.get("prev_hash")
                return p if isinstance(p, str) else None
    except OSError:
        pass
    return None


def _verify_file_chain(
    path: Path, expected_prev: str, base_index: int,
) -> tuple[bool, int, int]:
    """Walk one file's chain. Returns (ok, broken_global_index, n_in_file)."""
    if not path.exists():
        return True, -1, 0
    n_in_file = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, base_index + n_in_file, n_in_file
            if rec.get("prev_hash") != expected_prev:
                return False, base_index + n_in_file, n_in_file
            recomputed = _hash_record(rec["prev_hash"], rec)
            if recomputed != rec.get("this_hash"):
                return False, base_index + n_in_file, n_in_file
            expected_prev = rec["this_hash"]
            n_in_file += 1
    return True, -1, n_in_file
