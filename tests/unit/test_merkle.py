"""Tests for Merkle chain helpers + SQLite chain integrity."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from aegis.audit.sqlite_store import AuditDB, ChainBreakError
from aegis.sign.ed25519 import load_or_create_key, sign_atv
from aegis.sign.merkle import GENESIS_HASH, record_hash, verify_chain


def _make_record(key, aid: str, prev: str, idx: int) -> dict[str, object]:
    rec = sign_atv(
        f"atv-bytes-{idx}".encode(),
        {"aid": aid, "tenant_id": "t", "tool_name": "read_file"},
        prev,
        key,
    )
    rec["atv_id"] = f"atv-{idx}"
    rec["decision"] = "ALLOW"
    rec["this_hash"] = record_hash(rec["payload"])  # type: ignore[arg-type]
    return rec


def test_record_hash_is_deterministic_and_canonical() -> None:
    payload = {"x": 1, "y": [2, 3], "z": {"a": True}}
    h1 = record_hash(payload)
    # same content, different key insertion order -> same hash
    payload2 = {"z": {"a": True}, "y": [2, 3], "x": 1}
    h2 = record_hash(payload2)
    assert h1 == h2
    assert h1 == hashlib.sha3_256(b'{"x":1,"y":[2,3],"z":{"a":true}}').hexdigest()


def test_verify_chain_empty_is_ok() -> None:
    ok, err = verify_chain([])
    assert ok and err is None


def test_verify_chain_links_clean_chain(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    chain: list[dict[str, object]] = []
    prev = GENESIS_HASH
    for i in range(5):
        rec = _make_record(key, "a-1", prev, i)
        chain.append(rec)
        prev = rec["this_hash"]  # type: ignore[assignment]
    ok, err = verify_chain(chain)
    assert ok and err is None


def test_verify_chain_detects_break(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    a = _make_record(key, "x", GENESIS_HASH, 0)
    b = _make_record(key, "x", "WRONG_PREV", 1)
    ok, err = verify_chain([a, b])
    assert not ok
    assert err is not None and "chain break" in err


def test_verify_chain_detects_hash_tamper(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    a = _make_record(key, "x", GENESIS_HASH, 0)
    a["this_hash"] = "deadbeef"
    ok, err = verify_chain([a])
    assert not ok
    assert err is not None and "hash mismatch" in err


def test_audit_db_appends_and_returns_chain(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    db = AuditDB(":memory:")
    prev = GENESIS_HASH
    for i in range(3):
        r = _make_record(key, "agent-z", prev, i)
        db.append(r)
        prev = r["this_hash"]  # type: ignore[assignment]
    chain = db.get_chain("agent-z")
    assert len(chain) == 3
    ok, err = verify_chain(chain)
    assert ok, err
    assert db.get_head("agent-z") == prev


def test_audit_db_rejects_chain_break(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    db = AuditDB(":memory:")
    db.append(_make_record(key, "a", GENESIS_HASH, 0))
    bad = _make_record(key, "a", "WRONG_PREV", 1)
    with pytest.raises(ChainBreakError):
        db.append(bad)


def test_audit_db_concurrent_appends_serialize(tmp_path: Path) -> None:
    """100 concurrent appends from one builder thread must keep the chain intact.

    We can't have 100 truly concurrent writers since each next record's prev_hash
    depends on the previous record's this_hash. The realistic concurrency
    pattern is: many threads each computing a record under a lock that serializes
    against the DB head. We verify here that under thread contention with a
    serialized builder, no records are lost and the chain stays linked.
    """

    key = load_or_create_key(tmp_path / "k.pem")
    db_path = str(tmp_path / "audit.sqlite")
    db = AuditDB(db_path)
    n = 100
    builder_lock = threading.Lock()
    counter = {"i": 0}

    def worker() -> None:
        for _ in range(10):
            with builder_lock:
                idx = counter["i"]
                counter["i"] += 1
                prev = db.get_head("agent-z")
                rec = _make_record(key, "agent-z", prev, idx)
                db.append(rec)

    threads = [threading.Thread(target=worker) for _ in range(n // 10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    chain = db.get_chain("agent-z")
    assert len(chain) == n
    ok, err = verify_chain(chain)
    assert ok, err
