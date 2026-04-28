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


def _last_hash(path: Path) -> str:
    """Return the ``this_hash`` of the last line, or GENESIS_HASH."""
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS_HASH
    try:
        # Cheap: read the last 4 KB and parse the trailing line.
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
    return GENESIS_HASH


def append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append ``record`` with a chained ``prev_hash`` + ``this_hash``.

    Returns the augmented record (caller can inspect it). Audit failures
    raise ``OSError`` — callers in the hot path should wrap this in
    try/except so a write error never blocks a user's tool call.
    """
    prev_hash = _last_hash(path)
    chained = {**record, "prev_hash": prev_hash}
    chained["this_hash"] = _hash_record(prev_hash, chained)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(chained, default=str) + "\n")
    return chained


def verify_chain(path: Path) -> tuple[bool, int, int]:
    """Walk the chain end-to-end. ``(ok, broken_at_index, total)``.

    * ``ok`` is True iff every line's ``this_hash`` matches the recompute
      and every ``prev_hash`` chains to the prior line's ``this_hash``.
    * ``broken_at_index`` is 0-indexed (first broken line) or -1 if ok.
    * ``total`` counts non-blank, non-malformed lines walked.
    """
    if not path.exists():
        return True, -1, 0
    expected_prev = GENESIS_HASH
    total = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, total, total
            if rec.get("prev_hash") != expected_prev:
                return False, total, total
            recomputed = _hash_record(rec["prev_hash"], rec)
            if recomputed != rec.get("this_hash"):
                return False, total, total
            expected_prev = rec["this_hash"]
            total += 1
    return True, -1, total
