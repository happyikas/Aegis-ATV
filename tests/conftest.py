"""Shared pytest fixtures + safe environment.

Forces dummy embedding/judge so tests never reach the network, and points
every filesystem-touching setting (signing key, audit DB, JSONL) at a
session-scoped temp dir so importing ``aegis.main`` doesn't litter the
repo with ./keys/ or ./data/ files.
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
os.environ.setdefault("AEGIS_AUDIT_DB", ":memory:")
os.environ.setdefault("AEGIS_AUDIT_JSONL", str(_TMP_ROOT / "audit.jsonl"))


@pytest.fixture
def aegis_app(tmp_path: Path) -> Iterator[object]:
    """Build a fresh FastAPI app per test, isolated key/db/log under tmp_path."""
    from aegis.audit.jsonl_store import JsonlStore
    from aegis.audit.sqlite_store import AuditDB
    from aegis.main import create_app
    from aegis.sign.ed25519 import load_or_create_key

    key = load_or_create_key(tmp_path / "ed25519.pem")
    db = AuditDB(":memory:")
    log = JsonlStore(tmp_path / "audit.jsonl")
    yield create_app(key=key, db=db, log=log)
    db.close()
