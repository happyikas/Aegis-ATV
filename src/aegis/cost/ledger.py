"""Cost Attestation Ledger (patent §12 + Claims 3, 30, 34).

A SEPARATE signed store for cost-attestation records. Distinct from the
main Audit Log so that:
  - the signing-key slot is independent (Claim 34) — customers /
    regulators can be granted cost-only access without exposing the
    broader telemetry chain;
  - each record carries its own ATV commitment (Claim 30) — a verifier
    presented with both the cost record and the (later disclosed) ATV
    can confirm the cost dimensions weren't tampered with;
  - the ledger can be selectively disclosed (Claim 29 — implementation
    deferred but the API is shaped for it).

T2 storage is SQLite (indexed) + JSONL (raw append-only), mirroring the
audit-log split.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aegis.cost.divergence import DivergenceMetrics
from aegis.schema import ATVHeader, CostEfficiencyMetrics
from aegis.sign.merkle import GENESIS_HASH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_attestation (
    record_id        TEXT PRIMARY KEY,
    aid              TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    trace_id         TEXT NOT NULL,
    span_id          TEXT NOT NULL,
    atv_commitment   TEXT NOT NULL,
    model_name       TEXT,
    sw_cost_metrics  TEXT NOT NULL,    -- JSON of CostEfficiencyMetrics 16-D
    hw_cost_metrics  TEXT NOT NULL,    -- JSON of hw_cost_attestation 16-D (T2 zeros)
    divergence       TEXT NOT NULL,    -- JSON of DivergenceMetrics
    prev_hash        TEXT NOT NULL,
    this_hash        TEXT NOT NULL,
    signature        TEXT NOT NULL,
    signed_at_ns     INTEGER NOT NULL,
    payload_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_aid_ts ON cost_attestation(aid, signed_at_ns);
CREATE INDEX IF NOT EXISTS idx_cost_tenant_ts ON cost_attestation(tenant_id, signed_at_ns);
CREATE TABLE IF NOT EXISTS cost_chain_head (
    aid TEXT PRIMARY KEY,
    last_hash TEXT NOT NULL
);
"""


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha3_256(_canonical_json(payload)).hexdigest()


class CostAttestationLedger:
    """Thread-safe append-only ledger with its own Ed25519 key."""

    def __init__(self, db_path: str, jsonl_path: Path | None, signing_key: Ed25519PrivateKey) -> None:
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.signing_key = signing_key
        self.conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        if jsonl_path is not None:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def _next_prev_hash(self, cur: sqlite3.Cursor, aid: str) -> str:
        row = cur.execute(
            "SELECT last_hash FROM cost_chain_head WHERE aid=?", (aid,),
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def append(
        self,
        *,
        atv_commitment: str,
        header: ATVHeader,
        sw_cost_metrics: CostEfficiencyMetrics,
        divergence: DivergenceMetrics,
        hw_cost_metrics: list[float] | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        """Sign + persist a Cost Attestation Record. Returns the full record."""
        record_id = str(uuid.uuid4())
        ts = time.time_ns()
        # T2: hw_cost_attestation subfield is 16-D zero-fill.
        hw_array = list(hw_cost_metrics) if hw_cost_metrics is not None else [0.0] * 16

        payload: dict[str, Any] = {
            "record_id": record_id,
            "atv_commitment": atv_commitment,
            "header": {
                "aid": header.aid,
                "tenant_id": header.tenant_id,
                "trace_id": header.trace_id,
                "span_id": header.span_id,
                "schema_version": header.schema_version,
                "tier_profile": header.tier_profile,
                "cost_attestation_profile": header.cost_attestation_profile,
            },
            "model_name": model_name or "unknown",
            "sw_cost_metrics": sw_cost_metrics.model_dump(),
            "hw_cost_metrics": hw_array,
            "divergence": {
                "token_to_flops": divergence.token_to_flops,
                "memory_cost":    divergence.memory_cost,
                "dollar_cost":    divergence.dollar_cost,
            },
            "signed_at_ns": ts,
        }

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                prev = self._next_prev_hash(cur, header.aid)
                payload["prev_hash"] = prev
                this_hash = _record_hash(payload)
                signature = self.signing_key.sign(_canonical_json(payload))
                record = {
                    **payload,
                    "this_hash": this_hash,
                    "signature": signature.hex(),
                    "algorithm": "Ed25519",
                }
                cur.execute(
                    """INSERT INTO cost_attestation
                    (record_id, aid, tenant_id, trace_id, span_id, atv_commitment,
                     model_name, sw_cost_metrics, hw_cost_metrics, divergence,
                     prev_hash, this_hash, signature, signed_at_ns, payload_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record_id, header.aid, header.tenant_id, header.trace_id,
                        header.span_id, atv_commitment,
                        record["model_name"],
                        json.dumps(payload["sw_cost_metrics"], separators=(",", ":")),
                        json.dumps(payload["hw_cost_metrics"], separators=(",", ":")),
                        json.dumps(payload["divergence"], separators=(",", ":")),
                        prev, this_hash, signature.hex(), ts,
                        json.dumps(record, separators=(",", ":")),
                    ),
                )
                cur.execute(
                    """INSERT INTO cost_chain_head(aid, last_hash) VALUES (?, ?)
                    ON CONFLICT(aid) DO UPDATE SET last_hash=excluded.last_hash""",
                    (header.aid, this_hash),
                )
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise

        # JSONL append for the raw stream (separate from the audit JSONL).
        if self.jsonl_path is not None:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
                f.flush()

        return record

    def list_by_aid(self, aid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM cost_attestation WHERE aid=? ORDER BY signed_at_ns",
            (aid,),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_by_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM cost_attestation WHERE tenant_id=? "
            "ORDER BY signed_at_ns",
            (tenant_id,),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM cost_attestation WHERE record_id=?", (record_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def head(self, aid: str) -> str:
        row = self.conn.execute(
            "SELECT last_hash FROM cost_chain_head WHERE aid=?", (aid,),
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def verify_chain(self, aid: str) -> tuple[bool, str | None]:
        """Walk the per-aid sub-chain and verify prev_hash linkage."""
        records = self.list_by_aid(aid)
        expected = GENESIS_HASH
        for i, rec in enumerate(records):
            if rec.get("prev_hash") != expected:
                return False, f"chain break at index {i}: expected prev={expected}"
            payload = {k: v for k, v in rec.items()
                       if k not in ("this_hash", "signature", "algorithm")}
            recomputed = _record_hash(payload)
            if rec.get("this_hash") != recomputed:
                return False, f"hash mismatch at index {i}: stored {rec.get('this_hash')!r}"
            expected = recomputed
        return True, None

    def close(self) -> None:
        self.conn.close()
