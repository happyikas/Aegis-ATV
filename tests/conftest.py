"""Shared pytest fixtures + safe environment.

Forces dummy embedding/judge so tests never reach the network, and points
every filesystem-touching setting (signing key, audit DB, JSONL,
ATMU (Agent Telemetry Management Unit) intent log, cost ledger,
cost signing key) at a session-scoped temp dir
so importing ``aegis.main`` doesn't litter the repo with ./keys/ or
./data/ files.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="aegis-test-"))
os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_SIGNING_KEY_PATH", str(_TMP_ROOT / "ed25519.pem"))
os.environ.setdefault("AEGIS_PUBLIC_KEY_PATH", str(_TMP_ROOT / "ed25519.pub"))
os.environ.setdefault("AEGIS_COST_SIGNING_KEY_PATH", str(_TMP_ROOT / "ed25519_cost.pem"))
os.environ.setdefault("AEGIS_COST_PUBLIC_KEY_PATH", str(_TMP_ROOT / "ed25519_cost.pub"))
os.environ.setdefault("AEGIS_AUDIT_DB", ":memory:")
os.environ.setdefault("AEGIS_AUDIT_JSONL", str(_TMP_ROOT / "audit.jsonl"))
os.environ.setdefault("AEGIS_INTENT_LOG_DB", ":memory:")
os.environ.setdefault("AEGIS_COST_LEDGER_DB", ":memory:")
os.environ.setdefault("AEGIS_COST_LEDGER_JSONL", str(_TMP_ROOT / "cost.jsonl"))
os.environ.setdefault("AEGIS_HAM_DB", ":memory:")
os.environ.setdefault("AEGIS_HAM_DATA_KEY_PATH", str(_TMP_ROOT / "ham_data.key"))


@pytest.fixture
def aegis_app(tmp_path: Path) -> Iterator[object]:
    """Build a fresh FastAPI app per test, isolated stores under tmp_path."""
    from aegis.atmu import IntentLog
    from aegis.audit.encrypted_journal import EncryptedJournal, load_or_create_data_key
    from aegis.audit.jsonl_store import JsonlStore
    from aegis.audit.sqlite_store import AuditDB
    from aegis.cost.ledger import CostAttestationLedger
    from aegis.main import create_app
    from aegis.sign.ed25519 import load_or_create_key

    key = load_or_create_key(tmp_path / "ed25519.pem")
    cost_key = load_or_create_key(tmp_path / "ed25519_cost.pem")
    journal_key = load_or_create_data_key(tmp_path / "journal_data.key")
    db = AuditDB(":memory:")
    log = JsonlStore(tmp_path / "audit.jsonl")
    intent_log = IntentLog(":memory:")
    cost_ledger = CostAttestationLedger(
        db_path=":memory:",
        jsonl_path=tmp_path / "cost.jsonl",
        signing_key=cost_key,
    )
    encrypted_journal = EncryptedJournal(
        path=tmp_path / "audit_encrypted.jsonl",
        data_key=journal_key,
    )
    from aegis.ham import HierarchicalMemoryStore
    ham_key = load_or_create_data_key(tmp_path / "ham_data.key")
    ham_store = HierarchicalMemoryStore(
        db_path=":memory:",
        data_key=ham_key,
    )
    # v2.1.3: reset the per-process loop detector so each test starts
    # with empty per-session counts (the detector is a module-level
    # singleton inside aegis.monitor.loop_detector).
    from aegis.monitor.loop_detector import reset_default_detector
    reset_default_detector()
    yield create_app(
        key=key, db=db, log=log,
        intent_log=intent_log, cost_ledger=cost_ledger,
        encrypted_journal=encrypted_journal,
        ham_store=ham_store,
    )
    db.close()
    intent_log.close()
    cost_ledger.close()
    ham_store.close()
    reset_default_detector()
