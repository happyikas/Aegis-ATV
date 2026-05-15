"""ContextMemory writer + reader — append-only JSONL store.

Defensive contract:
* :func:`append` swallows OSErrors silently so the firewall verdict
  path is never blocked by a storage failure (matches the audit log
  and shadow.jsonl semantics — analytics writes must not affect
  decisions).
* Readers ignore malformed lines (skip-on-parse-error) so a
  partial-line write during a crash doesn't break later analytics.

Path convention
---------------

Default: ``~/.aegis/context_memory.jsonl``.
Env override: ``AEGIS_CONTEXT_MEMORY_PATH`` (full path, including filename).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from aegis.context_memory.record import ContextMemoryRecord


def context_memory_path() -> Path:
    """Return the canonical ContextMemory path.

    Override via ``AEGIS_CONTEXT_MEMORY_PATH``. Default is
    ``~/.aegis/context_memory.jsonl`` (sibling to the audit log).
    """
    raw = os.environ.get("AEGIS_CONTEXT_MEMORY_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "context_memory.jsonl"


# ── append ────────────────────────────────────────────────────────


def append(
    record: ContextMemoryRecord | dict[str, Any],
    *,
    path: Path | None = None,
    mode: str = "local",
) -> bool:
    """Append one record. Returns ``True`` on success, ``False`` on
    any storage failure. NEVER raises.

    Accepts either a fully-formed :class:`ContextMemoryRecord` or
    the raw audit-record dict (in which case
    :meth:`ContextMemoryRecord.from_audit_record` is invoked).

    ``mode`` is used only when projecting from an audit dict that
    doesn't carry its own ``mode`` field.
    """
    try:
        rec = (
            record
            if isinstance(record, ContextMemoryRecord)
            else ContextMemoryRecord.from_audit_record(record, mode=mode)
        )
        p = path if path is not None else context_memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec.to_dict(), sort_keys=True, ensure_ascii=False)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError):
        # Never block the verdict path. Failures are silent here;
        # operators see anomaly via `aegis doctor` reporting zero
        # records or an unrelated discrepancy with the audit log.
        return False


# ── read ──────────────────────────────────────────────────────────


def iter_records(path: Path | None = None) -> Iterator[ContextMemoryRecord]:
    """Yield records one at a time. Memory-friendly for large stores
    (silicon-ready: the same stream becomes a DMA scan)."""
    p = path if path is not None else context_memory_path()
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield ContextMemoryRecord.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    # Skip malformed line — see "Defensive contract" above.
                    continue
    except OSError:
        return


def read_all(path: Path | None = None) -> list[ContextMemoryRecord]:
    """Read all records into memory. Convenience for small stores
    and tests; for large stores prefer :func:`iter_records`."""
    return list(iter_records(path))


def read_window(
    since_ns: int = 0,
    until_ns: int | None = None,
    *,
    path: Path | None = None,
) -> list[ContextMemoryRecord]:
    """Records with ``ts_ns`` in ``[since_ns, until_ns]``.

    Convenience for ``aegis doctor --since`` style queries. When
    ``until_ns`` is ``None`` the window extends to the end of the
    store. Memory cost is O(matches), not O(file) — but the file is
    scanned linearly (silicon will replace this with an indexed
    range query).
    """
    out: list[ContextMemoryRecord] = []
    for rec in iter_records(path):
        if rec.ts_ns < since_ns:
            continue
        if until_ns is not None and rec.ts_ns > until_ns:
            continue
        out.append(rec)
    return out


__all__ = [
    "append",
    "context_memory_path",
    "iter_records",
    "read_all",
    "read_window",
]
