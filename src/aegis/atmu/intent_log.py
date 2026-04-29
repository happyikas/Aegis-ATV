"""ATMU (Agent Telemetry Management Unit) — Write-Ahead Intent Log
(patent ¶[0063A]-[0063H]).

SQLite-backed WAL that records every proposed tool invocation as a
tentative intent BEFORE it's released to the external tool. Each
record is linked to:
  - a monotonically increasing sequence number,
  - the ATV commitment (atv_hash),
  - a chain of transition markers (state_history column),
  - an optional checkpoint identifier (¶[0063E]),
  - optional tool-outcome fields (¶[0063H-1]) populated after release.

The WAL is a SEPARATE SQLite store from the main audit log. Both are
persisted under ``./data/`` in T2; in T3 the WAL lives in a reserved
non-volatile CSD region per patent §5A.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from aegis.atmu.state_machine import TxState, ensure_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intent_log (
    record_id      TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL UNIQUE,
    aid            TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT,
    ts_ns          INTEGER NOT NULL,
    tool_name      TEXT NOT NULL,
    tool_args_hash TEXT NOT NULL,
    blast_class    TEXT NOT NULL,
    atv_commitment TEXT,
    policy_hash    TEXT,
    checkpoint_id  TEXT,
    cost_profile   TEXT NOT NULL,
    oversight_state TEXT,
    current_state  TEXT NOT NULL,
    state_history  TEXT NOT NULL,  -- JSON list of {state, ts_ns, reason}
    tool_outcome   TEXT,           -- JSON (see append_tool_outcome)
    compensation_plan TEXT,        -- JSON {strategy, params, …}
    created_at_ns  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_aid_seq ON intent_log(aid, seq);
CREATE INDEX IF NOT EXISTS idx_intent_state ON intent_log(current_state);
CREATE TABLE IF NOT EXISTS intent_seq (
    next_seq INTEGER NOT NULL
);
INSERT OR IGNORE INTO intent_seq(next_seq) VALUES (1);
"""


def _blast_class_from_radius(blast: int) -> str:
    if blast >= 9:
        return "critical"
    if blast >= 7:
        return "high"
    if blast >= 4:
        return "medium"
    return "low"


class IntentLog:
    """Thread-safe WAL. One instance per process; shares SQLite WAL mode
    across appends."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def _next_seq(self, cur: sqlite3.Cursor) -> int:
        cur.execute("SELECT next_seq FROM intent_seq")
        (seq,) = cur.fetchone()
        cur.execute("UPDATE intent_seq SET next_seq = next_seq + 1")
        return int(seq)

    def append_tentative(
        self,
        *,
        aid: str,
        tenant_id: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        tool_name: str,
        tool_args_hash: str,
        blast_radius: int,
        atv_commitment: str | None,
        policy_hash: str | None = None,
        checkpoint_id: str | None = None,
        cost_profile: str = "software",
        oversight_state: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new tentative record. Returns the full row as a dict."""
        now = time.time_ns()
        record_id = str(uuid.uuid4())
        history = [{"state": TxState.TENTATIVE.value, "ts_ns": now, "reason": "intent"}]
        blast_class = _blast_class_from_radius(blast_radius)
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                seq = self._next_seq(cur)
                cur.execute(
                    """INSERT INTO intent_log
                    (record_id, seq, aid, tenant_id, trace_id, span_id, parent_span_id,
                     ts_ns, tool_name, tool_args_hash, blast_class, atv_commitment,
                     policy_hash, checkpoint_id, cost_profile, oversight_state,
                     current_state, state_history, tool_outcome, compensation_plan,
                     created_at_ns)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record_id, seq, aid, tenant_id, trace_id, span_id, parent_span_id,
                        now, tool_name, tool_args_hash, blast_class,
                        atv_commitment, policy_hash, checkpoint_id, cost_profile, oversight_state,
                        TxState.TENTATIVE.value, json.dumps(history), None, None, now,
                    ),
                )
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise
        # Return the canonical view of the just-inserted row WITHOUT a follow-up
        # SELECT — avoids racing with other threads on a shared sqlite handle.
        return {
            "record_id": record_id,
            "seq": seq,
            "aid": aid,
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "ts_ns": now,
            "tool_name": tool_name,
            "tool_args_hash": tool_args_hash,
            "blast_class": blast_class,
            "atv_commitment": atv_commitment,
            "policy_hash": policy_hash,
            "checkpoint_id": checkpoint_id,
            "cost_profile": cost_profile,
            "oversight_state": oversight_state,
            "current_state": TxState.TENTATIVE.value,
            "state_history": history,
            "tool_outcome": None,
            "compensation_plan": None,
            "created_at_ns": now,
        }

    def transition(
        self,
        record_id: str,
        *,
        new_state: TxState,
        reason: str,
    ) -> dict[str, Any]:
        """Validate + persist a state transition. Raises InvalidTransition
        if the move is illegal per APPENDIX B."""
        now = time.time_ns()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                row = cur.execute(
                    "SELECT current_state, state_history FROM intent_log WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown intent_log record_id={record_id}")
                current_str, history_json = row
                current = TxState(current_str)
                ensure_transition(current, new_state)   # raises InvalidTransition
                history = json.loads(history_json)
                history.append({"state": new_state.value, "ts_ns": now, "reason": reason})
                cur.execute(
                    "UPDATE intent_log SET current_state=?, state_history=? WHERE record_id=?",
                    (new_state.value, json.dumps(history), record_id),
                )
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise
        return self.get(record_id) or {}

    def append_tool_outcome(
        self,
        record_id: str,
        *,
        status: str,
        result_hash: str,
        side_effect_receipt: str | None = None,
        return_ts_ns: int | None = None,
    ) -> dict[str, Any]:
        """Attach a tool-outcome record (¶[0063H-1]).

        ``status`` ∈ {success, failure, timeout, partial, compensated}.
        Must be called on records that are already committed.
        """
        if status not in ("success", "failure", "timeout", "partial", "compensated"):
            raise ValueError(f"invalid outcome status: {status}")
        outcome = {
            "status": status,
            "result_hash": result_hash,
            "side_effect_receipt": side_effect_receipt,
            "return_ts_ns": return_ts_ns or time.time_ns(),
        }
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                cur.execute(
                    "UPDATE intent_log SET tool_outcome=? WHERE record_id=?",
                    (json.dumps(outcome), record_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"unknown intent_log record_id={record_id}")
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise
        return self.get(record_id) or {}

    def set_compensation_plan(self, record_id: str, plan: dict[str, Any]) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            in_tx = True
            try:
                cur.execute(
                    "UPDATE intent_log SET compensation_plan=? WHERE record_id=?",
                    (json.dumps(plan), record_id),
                )
                cur.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    cur.execute("ROLLBACK")
                raise

    def get(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM intent_log WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM intent_log LIMIT 0").description]
        rec = dict(zip(cols, row, strict=False))
        rec["state_history"] = json.loads(rec["state_history"])
        if rec.get("tool_outcome"):
            rec["tool_outcome"] = json.loads(rec["tool_outcome"])
        if rec.get("compensation_plan"):
            rec["compensation_plan"] = json.loads(rec["compensation_plan"])
        return rec

    def list_by_aid(self, aid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT record_id FROM intent_log WHERE aid=? ORDER BY seq", (aid,)
        ).fetchall()
        return [r for r in (self.get(rid) for (rid,) in rows) if r is not None]

    def count_state(self, state: TxState) -> int:
        (n,) = self.conn.execute(
            "SELECT COUNT(*) FROM intent_log WHERE current_state=?", (state.value,)
        ).fetchone()
        return int(n)

    def close(self) -> None:
        self.conn.close()
