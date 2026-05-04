"""Per-tenant budget persistence — `aegis budget set` writes here,
step335 reads from here.

Until this PR ``TENANT_BUDGETS`` was a hardcoded dict in
:mod:`aegis.firewall.step335_cost`. Operators couldn't say
"team-frontend's daily cap is $50, team-data's is $200" without
editing source. This module backs the same dict with a tiny SQLite
WAL store so:

* ``aegis budget set --tenant T --daily X --per-call Y`` persists
  across hook restarts
* step335 reads the stored ceiling at every PreToolUse (cached for
  the hot path so we don't re-open SQLite per tool call)
* ``aegis budget show`` lists every persisted tenant + the default

Schema (one row per tenant)::

    tenant_id       TEXT PRIMARY KEY
    daily_dollars   REAL NOT NULL
    per_call_dollars REAL
    updated_at_ns   INTEGER NOT NULL

Storage path: ``$AEGIS_BUDGET_DB`` (env override) or
``~/.aegis/budgets.sqlite``. Plugin / sidecar share the same file.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_PATH: Path = Path.home() / ".aegis" / "budgets.sqlite"
DEFAULT_TENANT: str = "default"
DEFAULT_DAILY_DOLLARS: float = 1.0    # mirrors step335's prior hardcoded value

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    tenant_id        TEXT PRIMARY KEY,
    daily_dollars    REAL NOT NULL,
    per_call_dollars REAL,
    updated_at_ns    INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Budget:
    """One tenant's persisted budget."""

    tenant_id: str
    daily_dollars: float
    per_call_dollars: float | None = None
    updated_at_ns: int = field(default_factory=time.time_ns)


def _resolve_db_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    raw = os.environ.get("AEGIS_BUDGET_DB", "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


class BudgetStore:
    """Thread-safe SQLite-backed budget table.

    Lifecycle: one instance per process. ``get_for(tenant_id)``
    returns the persisted Budget or the default (DEFAULT_DAILY_DOLLARS).
    Reads are cheap; the singleton in :func:`step335_cost.run` keeps
    one open connection.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = _resolve_db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def set(
        self,
        tenant_id: str,
        *,
        daily_dollars: float,
        per_call_dollars: float | None = None,
    ) -> Budget:
        """Insert or update a tenant's budget. ``daily_dollars`` must
        be > 0 (a 0 ceiling would block every call — likely a typo)."""
        if daily_dollars <= 0:
            raise ValueError(
                f"daily_dollars must be > 0, got {daily_dollars!r}"
            )
        if per_call_dollars is not None and per_call_dollars <= 0:
            raise ValueError(
                f"per_call_dollars must be > 0 or None, "
                f"got {per_call_dollars!r}"
            )
        ts = time.time_ns()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(
                    """INSERT INTO budgets
                          (tenant_id, daily_dollars, per_call_dollars,
                           updated_at_ns)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(tenant_id) DO UPDATE SET
                          daily_dollars = excluded.daily_dollars,
                          per_call_dollars = excluded.per_call_dollars,
                          updated_at_ns = excluded.updated_at_ns""",
                    (tenant_id, float(daily_dollars), per_call_dollars, ts),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return Budget(
            tenant_id=tenant_id,
            daily_dollars=float(daily_dollars),
            per_call_dollars=(
                float(per_call_dollars) if per_call_dollars is not None else None
            ),
            updated_at_ns=ts,
        )

    def get_for(self, tenant_id: str) -> Budget:
        """Return the persisted Budget for ``tenant_id``. Falls back
        to the default tenant's row if present, else the hardcoded
        DEFAULT_DAILY_DOLLARS — never raises."""
        row = self.conn.execute(
            """SELECT tenant_id, daily_dollars, per_call_dollars, updated_at_ns
               FROM budgets WHERE tenant_id = ?""",
            (tenant_id,),
        ).fetchone()
        if row is not None:
            return Budget(
                tenant_id=row[0],
                daily_dollars=float(row[1]),
                per_call_dollars=(
                    float(row[2]) if row[2] is not None else None
                ),
                updated_at_ns=int(row[3]),
            )
        # Try default tenant row.
        default_row = self.conn.execute(
            """SELECT daily_dollars, per_call_dollars, updated_at_ns
               FROM budgets WHERE tenant_id = ?""",
            (DEFAULT_TENANT,),
        ).fetchone()
        if default_row is not None:
            return Budget(
                tenant_id=DEFAULT_TENANT,
                daily_dollars=float(default_row[0]),
                per_call_dollars=(
                    float(default_row[1]) if default_row[1] is not None else None
                ),
                updated_at_ns=int(default_row[2]),
            )
        return Budget(
            tenant_id=DEFAULT_TENANT,
            daily_dollars=DEFAULT_DAILY_DOLLARS,
        )

    def list_all(self) -> list[Budget]:
        """All persisted budgets, in insertion order."""
        rows = self.conn.execute(
            """SELECT tenant_id, daily_dollars, per_call_dollars, updated_at_ns
               FROM budgets ORDER BY updated_at_ns ASC"""
        ).fetchall()
        return [
            Budget(
                tenant_id=r[0],
                daily_dollars=float(r[1]),
                per_call_dollars=(
                    float(r[2]) if r[2] is not None else None
                ),
                updated_at_ns=int(r[3]),
            )
            for r in rows
        ]

    def delete(self, tenant_id: str) -> bool:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(
                    "DELETE FROM budgets WHERE tenant_id = ?", (tenant_id,)
                )
                deleted = cur.rowcount
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return deleted > 0

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# ─────────────────────────────────────────────────────────────────────
# Lazy per-process singleton — used by step335 hot path
# ─────────────────────────────────────────────────────────────────────


_SINGLETON: BudgetStore | None = None
_SINGLETON_LOCK = threading.Lock()


def get_default_store() -> BudgetStore | None:
    """Lazy module-level singleton. Returns ``None`` if init fails so
    step335 can fall back to its hardcoded TENANT_BUDGETS without
    crashing the firewall on a bad DB path."""
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            return _SINGLETON
        try:
            _SINGLETON = BudgetStore()
            return _SINGLETON
        except (sqlite3.Error, OSError):
            return None


def reset_singleton_for_tests() -> None:
    """Tests use this to drop the singleton between cases so each
    test sees a fresh DB at its own ``tmp_path``."""
    global _SINGLETON
    import contextlib
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            with contextlib.suppress(sqlite3.Error):
                _SINGLETON.close()
        _SINGLETON = None


__all__ = [
    "DEFAULT_DAILY_DOLLARS",
    "DEFAULT_DB_PATH",
    "DEFAULT_TENANT",
    "Budget",
    "BudgetStore",
    "get_default_store",
    "reset_singleton_for_tests",
]
