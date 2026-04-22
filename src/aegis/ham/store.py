"""Hierarchical Agent Memory store (patent §13A M16, T2 emulation).

The patent specifies a 4-level hierarchy:

    L1 (HBM) — accelerator key-value cache, immediate inference context
    L2 (CXL) — warm KV state, recently used tool-result cache
    L3 (CSD-DRAM) — active transaction logs, hot memory objects, journal buffers
    L4 (NAND) — encrypted long-term agent memory, temporal knowledge graph

T2 doesn't have HBM, CXL, or non-volatile CSD; we emulate the
operationally-meaningful subset:

    L1 — process-local cache (Python dict, capped) — fast recall
    L2 — same as L1 in T2 (no separate cache-coherent memory tier)
    L3 — same as L1 in T2 (no separate CSD-DRAM)
    L4 — encrypted SQLite store (durable + tenant/aid bound)

Patent ¶[0102B] requires every memory object be bound to:
    aid, tenant_id, transaction_sequence_number, hardware_timestamp,
    schema_version, policy_domain_id, encryption_metadata,
    cryptographic_digest

T2 stores the bind set in cleartext columns alongside the encrypted
body so range queries don't need decryption.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12
KEY_LEN = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ham_objects (
    object_id        TEXT PRIMARY KEY,
    aid              TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    ts_ns            INTEGER NOT NULL,
    schema_version   TEXT NOT NULL,
    policy_domain_id TEXT,
    digest           TEXT NOT NULL,        -- SHA3-256 of the cleartext payload
    nonce            TEXT NOT NULL,        -- base64 12-byte nonce
    ciphertext       TEXT NOT NULL,        -- base64 AES-GCM ct + tag
    tags             TEXT NOT NULL,        -- JSON list of strings, used by recall
    tombstoned       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ham_aid ON ham_objects(aid, ts_ns);
CREATE INDEX IF NOT EXISTS idx_ham_tenant ON ham_objects(tenant_id, ts_ns);
CREATE TABLE IF NOT EXISTS ham_seq (next_seq INTEGER NOT NULL);
INSERT OR IGNORE INTO ham_seq(next_seq) VALUES (1);
"""


class HierarchicalMemoryStore:
    """L3 + L4 software emulation of the patent's HAM hierarchy."""

    def __init__(self, db_path: str, data_key: bytes, l1_cache_size: int = 256) -> None:
        if len(data_key) != KEY_LEN:
            raise ValueError(f"data_key must be {KEY_LEN} bytes, got {len(data_key)}")
        self._aead = AESGCM(data_key)
        self.conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._l1: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._l1_cache_size = l1_cache_size

    def _next_seq(self, cur: sqlite3.Cursor) -> int:
        cur.execute("SELECT next_seq FROM ham_seq")
        (seq,) = cur.fetchone()
        cur.execute("UPDATE ham_seq SET next_seq = next_seq + 1")
        return int(seq)

    def _encrypt(self, plaintext: bytes, aad: bytes) -> tuple[str, str, str]:
        nonce = secrets.token_bytes(NONCE_LEN)
        ct = self._aead.encrypt(nonce, plaintext, aad)
        digest = hashlib.sha3_256(plaintext).hexdigest()
        return base64.b64encode(nonce).decode(), base64.b64encode(ct).decode(), digest

    def _decrypt(self, nonce_b64: str, ct_b64: str, aad: bytes) -> bytes:
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
        return self._aead.decrypt(nonce, ct, aad)

    def _aad_for(self, aid: str, tenant_id: str, seq: int) -> bytes:
        return f"{tenant_id}|{aid}|{seq}".encode()

    def _l1_put(self, object_id: str, obj: dict[str, Any]) -> None:
        self._l1[object_id] = obj
        self._l1.move_to_end(object_id)
        while len(self._l1) > self._l1_cache_size:
            self._l1.popitem(last=False)

    # ─────────────────────────────────────────────────────────────────
    # Patent ¶[0102C] operations
    # ─────────────────────────────────────────────────────────────────

    def memory(
        self,
        *,
        aid: str,
        tenant_id: str,
        body: dict[str, Any],
        tags: list[str] | None = None,
        policy_domain_id: str | None = None,
        schema_version: str = "HAM-v1",
    ) -> dict[str, Any]:
        """Store an agent memory item as an encrypted, AID-bound, temporal record."""
        ts = time.time_ns()
        object_id = str(uuid.uuid4())
        plaintext = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                seq = self._next_seq(cur)
                aad = self._aad_for(aid, tenant_id, seq)
                nonce_b64, ct_b64, digest = self._encrypt(plaintext, aad)
                cur.execute(
                    """INSERT INTO ham_objects
                    (object_id, aid, tenant_id, seq, ts_ns, schema_version,
                     policy_domain_id, digest, nonce, ciphertext, tags, tombstoned)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        object_id, aid, tenant_id, seq, ts, schema_version,
                        policy_domain_id, digest, nonce_b64, ct_b64,
                        json.dumps(tags or []),
                    ),
                )
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise

        record = {
            "object_id": object_id, "aid": aid, "tenant_id": tenant_id,
            "seq": seq, "ts_ns": ts, "schema_version": schema_version,
            "policy_domain_id": policy_domain_id, "digest": digest,
            "tags": tags or [], "body": body, "tombstoned": False,
        }
        self._l1_put(object_id, record)
        return record

    def recall(
        self,
        *,
        aid: str,
        tenant_id: str,
        tags: list[str] | None = None,
        limit: int = 10,
        include_body: bool = True,
    ) -> list[dict[str, Any]]:
        """Retrieve memory items by AID + tenant, optionally filtering by
        tag intersection. Sorted by recency (newest first)."""
        rows = self.conn.execute(
            """SELECT object_id, seq, ts_ns, schema_version, policy_domain_id,
                      digest, nonce, ciphertext, tags, tombstoned
               FROM ham_objects
               WHERE aid=? AND tenant_id=? AND tombstoned=0
               ORDER BY ts_ns DESC
               LIMIT ?""",
            (aid, tenant_id, max(1, min(limit * 4, 1000))),  # over-fetch for tag filter
        ).fetchall()
        out: list[dict[str, Any]] = []
        wanted = set(tags or [])
        for r in rows:
            (object_id, seq, ts_ns, sv, pdid, digest, nonce_b64, ct_b64,
             tags_json, tomb) = r
            obj_tags = json.loads(tags_json)
            if wanted and not (wanted & set(obj_tags)):
                continue
            entry: dict[str, Any] = {
                "object_id": object_id, "aid": aid, "tenant_id": tenant_id,
                "seq": seq, "ts_ns": ts_ns, "schema_version": sv,
                "policy_domain_id": pdid, "digest": digest,
                "tags": obj_tags, "tombstoned": bool(tomb),
            }
            if include_body:
                aad = self._aad_for(aid, tenant_id, seq)
                plaintext = self._decrypt(nonce_b64, ct_b64, aad)
                entry["body"] = json.loads(plaintext)
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def context(
        self,
        *,
        aid: str,
        tenant_id: str,
        tags: list[str] | None = None,
        max_items: int = 5,
    ) -> dict[str, Any]:
        """Inject authorized memory items into a (synthetic) agent context
        window. Returns the assembled context bundle + the source IDs."""
        items = self.recall(
            aid=aid, tenant_id=tenant_id, tags=tags, limit=max_items,
        )
        bundle = {
            "ts_ns": time.time_ns(),
            "items": [{"object_id": it["object_id"], "body": it["body"]} for it in items],
        }
        return {"bundle": bundle, "source_ids": [it["object_id"] for it in items]}

    def forget(
        self,
        *,
        object_id: str,
        aid: str,
        tenant_id: str,
        reason: str = "user_requested",
    ) -> bool:
        """Tombstone a memory object (¶[0102C] forget op).
        Selective redaction: the row is preserved for audit but flagged."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                cur.execute(
                    "UPDATE ham_objects SET tombstoned=1 "
                    "WHERE object_id=? AND aid=? AND tenant_id=?",
                    (object_id, aid, tenant_id),
                )
                affected = cur.rowcount
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise
        if object_id in self._l1:
            del self._l1[object_id]
        _ = reason  # reserved for audit-log integration in M17
        return affected > 0

    def summarize(
        self,
        *,
        aid: str,
        tenant_id: str,
        tags: list[str] | None = None,
        max_items: int = 20,
    ) -> dict[str, Any]:
        """Crude summary: count + tag histogram + most-recent timestamp.
        T3 / future will plug in an LLM-driven semantic summarizer."""
        items = self.recall(
            aid=aid, tenant_id=tenant_id, tags=tags, limit=max_items, include_body=False,
        )
        tag_hist: dict[str, int] = {}
        for it in items:
            for t in it["tags"]:
                tag_hist[t] = tag_hist.get(t, 0) + 1
        return {
            "aid": aid, "tenant_id": tenant_id,
            "item_count": len(items),
            "most_recent_ts_ns": items[0]["ts_ns"] if items else 0,
            "tag_histogram": tag_hist,
        }

    def ground(
        self,
        *,
        aid: str,
        tenant_id: str,
        claim: str,
        reference_ids: list[str],
    ) -> dict[str, Any]:
        """Bind an agent claim to one or more reference memory objects
        (¶[0102C] ground op). T2 returns a signed-style attestation
        bundle; M11 attestation could later sign it for real."""
        # Verify the references exist + belong to this aid/tenant.
        valid: list[str] = []
        for rid in reference_ids:
            row = self.conn.execute(
                "SELECT object_id FROM ham_objects "
                "WHERE object_id=? AND aid=? AND tenant_id=? AND tombstoned=0",
                (rid, aid, tenant_id),
            ).fetchone()
            if row is not None:
                valid.append(rid)
        bundle = {
            "ts_ns": time.time_ns(), "aid": aid, "tenant_id": tenant_id,
            "claim_hash": hashlib.sha3_256(claim.encode()).hexdigest(),
            "references": valid,
            "missing": [r for r in reference_ids if r not in valid],
        }
        return bundle

    def stats(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """Diagnostic counts (per-tenant or global)."""
        params: tuple[Any, ...]
        if tenant_id:
            base = "WHERE tenant_id=?"
            params = (tenant_id,)
        else:
            base = ""
            params = ()
        (n,) = self.conn.execute(
            f"SELECT COUNT(*) FROM ham_objects {base}", params,
        ).fetchone()
        (n_tomb,) = self.conn.execute(
            f"SELECT COUNT(*) FROM ham_objects {base}{' AND' if base else 'WHERE'} tombstoned=1",
            params,
        ).fetchone()
        return {
            "tenant_id": tenant_id,
            "total_objects": int(n),
            "tombstoned": int(n_tomb),
            "live": int(n) - int(n_tomb),
            "l1_cache_size": len(self._l1),
        }

    def close(self) -> None:
        self.conn.close()
