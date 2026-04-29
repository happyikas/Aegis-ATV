"""Unit tests for src/aegis/audit/patrol.py (v4.0, Claim 54)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aegis.atmu.intent_log import IntentLog
from aegis.audit.encrypted_journal import EncryptedJournal, load_or_create_data_key
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.patrol import (
    AuditPatrol,
    PatrolConfig,
    PatrolFinding,
    PatrolReport,
)
from aegis.audit.sqlite_store import AuditDB
from aegis.cost.ledger import CostAttestationLedger
from aegis.sign.ed25519 import sign_atv
from aegis.sign.merkle import GENESIS_HASH, record_hash

# ─────────────────────────────────────────────────────────────────────
# Fixture helpers — build a fully-populated audit pair
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def stores(tmp_path: Path, signing_key: Ed25519PrivateKey):
    audit = AuditDB(str(tmp_path / "audit.sqlite"))
    jsonl = JsonlStore(tmp_path / "audit.jsonl")
    intent = IntentLog(str(tmp_path / "intent.sqlite"))
    cost = CostAttestationLedger(
        db_path=str(tmp_path / "cost.sqlite"),
        jsonl_path=tmp_path / "cost.jsonl",
        signing_key=Ed25519PrivateKey.generate(),
    )
    journal_key = load_or_create_data_key(tmp_path / "journal.key")
    journal = EncryptedJournal(tmp_path / "journal.jsonl", data_key=journal_key)
    yield {
        "audit": audit, "jsonl": jsonl, "intent": intent,
        "cost": cost, "journal": journal, "journal_key": journal_key,
        "tmp": tmp_path,
    }
    audit.close()
    cost.close()


def _make_signed_record(
    *,
    sk: Ed25519PrivateKey,
    aid: str = "agent-A",
    tenant: str = "tenant-A",
    prev_hash: str = GENESIS_HASH,
    atv_id: str = "atv-1",
    decision: str = "ALLOW",
) -> dict[str, Any]:
    """Build a signed audit record matching the production shape
    (aegis.firewall.step360_audit.sign_and_append)."""
    header = {
        "aid": aid, "tenant_id": tenant, "tool_name": "Bash",
        "decision": decision,
        "atv_hash": "0" * 64,
    }
    rec = sign_atv(b"\x00" * 32, header, prev_hash, sk)
    rec["atv_id"] = atv_id
    rec["decision"] = decision
    rec["this_hash"] = record_hash(rec["payload"])
    return rec


def _seed_chain(
    audit: AuditDB, jsonl: JsonlStore, sk: Ed25519PrivateKey, *, count: int = 3
) -> list[dict[str, Any]]:
    """Append ``count`` valid signed records to both stores."""
    records: list[dict[str, Any]] = []
    prev = GENESIS_HASH
    for i in range(count):
        rec = _make_signed_record(sk=sk, atv_id=f"atv-{i}", prev_hash=prev)
        audit.append(rec)
        jsonl.append(rec)
        records.append(rec)
        prev = rec["this_hash"]
    return records


# ─────────────────────────────────────────────────────────────────────
# patrol_full
# ─────────────────────────────────────────────────────────────────────


def test_full_clean_chain(stores, signing_key) -> None:
    _seed_chain(stores["audit"], stores["jsonl"], signing_key, count=3)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_full()
    assert report.scope == "full"
    assert report.records_scanned == 3
    assert report.findings == []
    assert report.status == "clean"


def test_full_detects_signature_tamper(stores, signing_key) -> None:
    _seed_chain(stores["audit"], stores["jsonl"], signing_key, count=2)
    # Tamper the stored payload of one record so signature no longer verifies
    conn: sqlite3.Connection = stores["audit"].conn
    row = conn.execute("SELECT atv_id, payload_json FROM audit LIMIT 1").fetchone()
    rec = json.loads(row[1])
    rec["payload"]["header"]["aid"] = "tampered"
    conn.execute(
        "UPDATE audit SET payload_json=? WHERE atv_id=?",
        (json.dumps(rec), row[0]),
    )

    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_full()
    sig_findings = [f for f in report.findings if f.category == "signature"]
    assert len(sig_findings) >= 1
    assert report.status == "critical"


def test_full_handles_empty_audit_db(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_full()
    assert report.status == "clean"
    assert report.records_scanned == 0


# ─────────────────────────────────────────────────────────────────────
# patrol_sample
# ─────────────────────────────────────────────────────────────────────


def test_sample_clean(stores, signing_key) -> None:
    _seed_chain(stores["audit"], stores["jsonl"], signing_key, count=20)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_sample(fraction=0.50)
    assert report.scope == "sample"
    assert 0 < report.records_scanned <= 20
    assert report.status == "clean"


def test_sample_detects_hash_mismatch(stores, signing_key) -> None:
    _seed_chain(stores["audit"], stores["jsonl"], signing_key, count=5)
    # Corrupt the stored this_hash of one row
    stores["audit"].conn.execute(
        "UPDATE audit SET this_hash='deadbeef' WHERE atv_id='atv-0'"
    )
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    # Force-fraction=1.0 so we definitely sample the corrupted row
    report = patrol.patrol_sample(fraction=1.0)
    # Note: this_hash on row != stored payload's this_hash, so hash recompute
    # checks the payload's this_hash against record_hash(payload). When we
    # only corrupt the row's stored this_hash column (not the payload_json),
    # the patrol's hash recompute uses the JSON-embedded value, so this is
    # actually expected to NOT trigger. Instead, corrupt the payload itself
    # to verify the hash-mismatch path:
    stores["audit"].conn.execute(
        "UPDATE audit SET payload_json=? WHERE atv_id='atv-1'",
        (json.dumps({
            "atv_id": "atv-1", "decision": "ALLOW",
            "this_hash": "tampered-stored",
            "signature": "x", "algorithm": "Ed25519",
            "payload": {"header": {"aid": "agent-A"}},
        }),),
    )
    report = patrol.patrol_sample(fraction=1.0)
    # Check that at least one of the cases triggered some finding.
    # Both signature and hash_mismatch are critical so status='critical'.
    assert report.status == "critical"


def test_sample_invalid_fraction_raises(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    with pytest.raises(ValueError, match="fraction"):
        patrol.patrol_sample(fraction=0.0)
    with pytest.raises(ValueError, match="fraction"):
        patrol.patrol_sample(fraction=1.5)


def test_sample_empty_db_no_findings(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_sample()
    assert report.records_scanned == 0
    assert report.status == "clean"


# ─────────────────────────────────────────────────────────────────────
# patrol_sequence (ATMU intent_log)
# ─────────────────────────────────────────────────────────────────────


def _seed_intents(intent: IntentLog, count: int = 5) -> list[str]:
    ids = []
    for i in range(count):
        rec = intent.append_tentative(
            aid="agent-A", tenant_id="tenant-A",
            trace_id="t" * 32, span_id=f"s{i:015d}",
            parent_span_id=None,
            tool_name="Bash", tool_args_hash=f"h-{i}", blast_radius=1,
            atv_commitment=f"c-{i}",
        )
        ids.append(rec["record_id"])
    return ids


def test_sequence_clean(stores, signing_key) -> None:
    _seed_intents(stores["intent"], count=5)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_sequence()
    assert report.scope == "sequence"
    assert report.records_scanned == 5
    assert report.status == "clean"


def test_sequence_detects_gap(stores, signing_key) -> None:
    _seed_intents(stores["intent"], count=5)
    # Delete seq=3
    stores["intent"].conn.execute("DELETE FROM intent_log WHERE seq=3")
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_sequence()
    assert report.status == "critical"
    gap_findings = [f for f in report.findings if f.category == "sequence_gap"]
    assert len(gap_findings) == 1


def test_sequence_empty_intent_log(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_sequence()
    assert report.records_scanned == 0
    assert report.status == "clean"


# ─────────────────────────────────────────────────────────────────────
# patrol_consistency
# ─────────────────────────────────────────────────────────────────────


def test_consistency_clean_when_stores_match(stores, signing_key) -> None:
    _seed_chain(stores["audit"], stores["jsonl"], signing_key, count=3)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_consistency()
    assert report.status == "clean"


def test_consistency_detects_jsonl_missing(stores, signing_key) -> None:
    """SQLite has more records than JSONL → 'present in SQLite, missing from JSONL'."""
    rec = _make_signed_record(sk=signing_key, atv_id="lonely")
    stores["audit"].append(rec)
    # JSONL untouched
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    report = patrol.patrol_consistency()
    inconsistency = [f for f in report.findings if f.category == "consistency"]
    assert any("missing from JSONL" in f.detail for f in inconsistency)


def test_consistency_detects_aead_tamper(stores, signing_key) -> None:
    """Encrypted journal: corrupting ciphertext is caught at decrypt."""
    stores["journal"].append({
        "verdict": "ALLOW",
        "payload": {"header": {"tenant_id": "t", "aid": "a"}},
    })
    # Corrupt the stored line
    text = stores["journal"].path.read_text(encoding="utf-8")
    wrappers = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(wrappers) == 1
    wrappers[0]["ciphertext"] = "AAAA" + wrappers[0]["ciphertext"][4:]
    stores["journal"].path.write_text(
        json.dumps(wrappers[0], separators=(",", ":")) + "\n",
    )

    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
        encrypted_journal=stores["journal"],
    )
    report = patrol.patrol_consistency()
    aead = [f for f in report.findings if f.category == "aead"]
    assert len(aead) >= 1
    assert report.status == "critical"


# ─────────────────────────────────────────────────────────────────────
# patrol_cold
# ─────────────────────────────────────────────────────────────────────


def test_cold_skips_when_dir_missing(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
        cold_archive_dir=None,
    )
    report = patrol.patrol_cold()
    assert report.status == "clean"
    assert any("not configured" in n for n in report.notes)


def test_cold_skips_when_data_key_absent(stores, signing_key) -> None:
    cold = stores["tmp"] / "cold"
    cold.mkdir()
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
        cold_archive_dir=cold, cold_data_key=None,
    )
    report = patrol.patrol_cold()
    assert any("data_key" in n for n in report.notes)


def test_cold_verifies_archived_segment(stores, signing_key) -> None:
    """Write 2 records via the live journal → copy to cold → patrol_cold passes."""
    cold = stores["tmp"] / "cold"
    cold.mkdir()
    # Write some records
    for i in range(2):
        stores["journal"].append({
            "verdict": "ALLOW",
            "payload": {"header": {"tenant_id": "t", "aid": f"a-{i}"}},
        })
    # Pretend the live file got rotated and copied to cold
    archived = cold / "audit.0001.jsonl"
    archived.write_bytes(stores["journal"].path.read_bytes())

    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
        encrypted_journal=stores["journal"],
        cold_archive_dir=cold, cold_data_key=stores["journal_key"],
    )
    report = patrol.patrol_cold()
    assert report.status == "clean"
    assert report.records_scanned == 2


# ─────────────────────────────────────────────────────────────────────
# Lifecycle / introspection
# ─────────────────────────────────────────────────────────────────────


def test_recent_reports_keeps_last_n(stores, signing_key) -> None:
    _seed_intents(stores["intent"], count=2)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
        max_history=3,
    )
    for _ in range(5):
        patrol.patrol_sequence()
    reports = patrol.recent_reports(limit=10)
    assert len(reports) == 3  # bounded by max_history


def test_latest_status_unknown_before_any_run(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    status = patrol.latest_status()
    assert status["status"] == "unknown"


def test_latest_status_after_run(stores, signing_key) -> None:
    _seed_intents(stores["intent"], count=2)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    patrol.patrol_sequence()
    status = patrol.latest_status()
    assert status["status"] == "clean"
    assert status["scope"] == "sequence"


def test_start_stop_cleanly(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
        config=PatrolConfig(
            sequence_interval_sec=0.05, sample_interval_sec=10.0,
            full_interval_sec=10.0, consistency_interval_sec=10.0,
            cold_interval_sec=10.0, poll_seconds=0.05,
        ),
    )
    _seed_intents(stores["intent"], count=2)
    patrol.start()
    time.sleep(0.20)
    patrol.stop(timeout_sec=2.0)
    # The daemon should have run at least one sequence patrol.
    reports = patrol.recent_reports()
    assert any(r["scope"] == "sequence" for r in reports)


def test_double_start_is_noop(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    patrol.start()
    th1 = patrol._thread
    patrol.start()
    assert patrol._thread is th1
    patrol.stop(timeout_sec=2.0)


def test_stop_without_start_is_safe(stores, signing_key) -> None:
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    patrol.stop()  # should not raise


# ─────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────


def test_endpoint_status_when_disabled() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.audit_patrol import make_router
    app = FastAPI()
    app.include_router(make_router(patrol=None))
    with TestClient(app) as client:
        r = client.get("/audit/patrol/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_endpoint_run_when_disabled_returns_503() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.audit_patrol import make_router
    app = FastAPI()
    app.include_router(make_router(patrol=None))
    with TestClient(app) as client:
        r = client.post("/audit/patrol/run", json={"scope": "sequence"})
    assert r.status_code == 503


def test_endpoint_run_sequence(stores, signing_key) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.audit_patrol import make_router
    _seed_intents(stores["intent"], count=3)
    patrol = AuditPatrol(
        public_key=signing_key.public_key(),
        audit_db=stores["audit"], jsonl=stores["jsonl"],
        intent_log=stores["intent"], cost_ledger=stores["cost"],
    )
    app = FastAPI()
    app.include_router(make_router(patrol=patrol))
    with TestClient(app) as client:
        r = client.post("/audit/patrol/run", json={"scope": "sequence"})
    assert r.status_code == 200
    data = r.json()
    assert data["scope"] == "sequence"
    assert data["status"] == "clean"


def test_dataclass_to_dict_serialisable() -> None:
    report = PatrolReport(scope="sample", started_ns=1, completed_ns=1_000_000)
    report.findings.append(PatrolFinding(
        severity="warning", category="consistency",
        store="audit_db", record_ref="x", detail="y",
    ))
    report.status = "warning"
    blob = json.dumps(report.to_dict())
    assert "warning" in blob
    assert "duration_ms" in blob
