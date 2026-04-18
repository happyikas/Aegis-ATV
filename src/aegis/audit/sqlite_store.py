"""SQLite-backed audit index + chain head tracking (PLAN 6.7)."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from aegis.sign.merkle import GENESIS_HASH

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    atv_id       TEXT PRIMARY KEY,
    aid          TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    decision     TEXT NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    prev_hash    TEXT,
    this_hash    TEXT NOT NULL,
    signature    TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aid_ts ON audit(aid, timestamp_ns);
CREATE TABLE IF NOT EXISTS chain_head (
    aid       TEXT PRIMARY KEY,
    last_hash TEXT NOT NULL
);
"""


class ChainBreakError(RuntimeError):
    """Raised when an append's prev_hash doesn't match the stored head."""


class AuditDB:
    """Thread-safe SQLite store for audit records.

    A single SQLite connection is used; appends serialize on a per-instance
    lock so the read-then-write head update is atomic. ``isolation_level``
    is set to None so we manage transactions explicitly with BEGIN IMMEDIATE.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(
            path,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        """Insert ``record`` if its ``prev_hash`` matches the current chain head."""
        import json as _json

        aid = record["payload"]["header"]["aid"]
        prev = record["payload"]["prev_hash"]

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    "SELECT last_hash FROM chain_head WHERE aid=?", (aid,)
                ).fetchone()
                current = row[0] if row else GENESIS_HASH
                if current != prev:
                    raise ChainBreakError(
                        f"chain break for aid={aid}: expected prev={current}, got {prev}"
                    )
                cur.execute(
                    """INSERT INTO audit
                    (atv_id, aid, tenant_id, tool_name, decision, timestamp_ns,
                     prev_hash, this_hash, signature, payload_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record["atv_id"],
                        aid,
                        record["payload"]["header"]["tenant_id"],
                        record["payload"]["header"].get("tool_name", ""),
                        record["decision"],
                        record["payload"]["signed_at_ns"],
                        prev,
                        record["this_hash"],
                        record["signature"],
                        _json.dumps(record, separators=(",", ":")),
                    ),
                )
                cur.execute(
                    """INSERT INTO chain_head(aid, last_hash) VALUES (?, ?)
                    ON CONFLICT(aid) DO UPDATE SET last_hash=excluded.last_hash""",
                    (aid, record["this_hash"]),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def get_head(self, aid: str) -> str:
        row = self.conn.execute(
            "SELECT last_hash FROM chain_head WHERE aid=?", (aid,)
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def get_chain(self, aid: str) -> list[dict[str, Any]]:
        import json as _json

        rows = self.conn.execute(
            "SELECT payload_json FROM audit WHERE aid=? ORDER BY timestamp_ns",
            (aid,),
        ).fetchall()
        return [_json.loads(r[0]) for r in rows]

    def close(self) -> None:
        self.conn.close()
