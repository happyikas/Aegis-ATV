"""Merkle hash helpers for the audit chain."""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "GENESIS"


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def record_hash(payload: dict[str, Any]) -> str:
    """SHA3-256 of the canonical JSON of a signed-record payload."""
    return hashlib.sha3_256(canonical_json(payload)).hexdigest()


def verify_chain(records: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Walk a list of records (oldest first) and check prev_hash links.

    Returns ``(ok, error)`` where ``error`` describes the first broken link.
    """
    expected = GENESIS_HASH
    for i, rec in enumerate(records):
        actual_prev = rec["payload"]["prev_hash"]
        if actual_prev != expected:
            return (
                False,
                f"chain break at index {i}: expected prev={expected}, got {actual_prev}",
            )
        computed = record_hash(rec["payload"])
        if rec.get("this_hash") != computed:
            return (
                False,
                f"hash mismatch at index {i}: stored {rec.get('this_hash')!r} != "
                f"computed {computed!r}",
            )
        expected = computed
    return True, None
