"""Persistent deny log for `aegis autonomy deny <trace_id>`.

v0.5.11 + v0.5.12 wired the reward shaping to recognise an
``EXPLICIT_DENY`` event when a record carries the
``aegis.autonomy.user_deny`` stamp. But there was no CLI to
*produce* that stamp — the strongest negative signal in the
learner was an unused pathway.

This module closes the loop with a simple append-only JSONL file
that the learner consults at training time:

  ~/.aegis/autonomy/denials.jsonl
    {"trace_id": "abc123", "ts_ns": 1778..., "note": "..."}
    {"trace_id": "def456", "ts_ns": 1778..., "note": ""}
    ...

The CLI ``aegis autonomy deny <trace_id> [--note ...]`` writes one
line. The learner, when assembling reward events, treats any
record whose ``trace_id`` appears in this set as an
``EXPLICIT_DENY`` — equivalent to ``β += 10`` on the pattern's
posterior.

Why a separate file rather than mutating ContextMemory?

* ContextMemory is **append-only** by audit contract; rewriting
  past records would invalidate the SHA3 hash chain that
  ``aegis verify-audit`` relies on.
* A denial is *operator metadata about a past decision*, not a
  new agent action — it belongs in its own log.
* The deny log is small (one line per explicit deny), so a
  full reload at every learn-time is cheap and avoids stateful
  caching.

The file is read defensively: missing file → empty set; malformed
line → skipped, never raises. The contract is "no deny entries
loaded" rather than "learner crashes", so a corrupted deny log
can never block training.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────


def denials_path() -> Path:
    """Resolve the canonical denials log path.

    Honours ``AEGIS_AUTONOMY_DENIALS`` for tests / multi-tenant
    deployments; defaults to ``~/.aegis/autonomy/denials.jsonl``."""
    raw = os.environ.get("AEGIS_AUTONOMY_DENIALS", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "autonomy" / "denials.jsonl"


# ──────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DenialRecord:
    """One operator-issued denial. ``ts_ns`` is when the deny was
    issued, NOT when the original record was created."""

    trace_id: str
    ts_ns: int
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "trace_id": self.trace_id,
                "ts_ns": self.ts_ns,
                "note": self.note,
            },
            ensure_ascii=False,
        )


# ──────────────────────────────────────────────────────────────────
# Append-only writer
# ──────────────────────────────────────────────────────────────────


def append_denial(
    trace_id: str,
    *,
    note: str = "",
    path: Path | None = None,
    now_ns: int | None = None,
) -> DenialRecord:
    """Append a denial to the log.

    The append is *not* atomic across processes (we don't lock —
    the deny log is operator-driven and never sees concurrent
    writes in practice). It IS line-atomic on POSIX, so a partial
    write can't corrupt the JSONL: the loader simply skips
    malformed lines.

    Returns the persisted record so the CLI can echo it back."""
    if not trace_id or not isinstance(trace_id, str):
        raise ValueError(f"trace_id must be a non-empty string; got {trace_id!r}")
    target = path if path is not None else denials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    record = DenialRecord(
        trace_id=trace_id,
        ts_ns=now_ns if now_ns is not None else time.time_ns(),
        note=note,
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(record.to_json() + "\n")
    return record


# ──────────────────────────────────────────────────────────────────
# Loader
# ──────────────────────────────────────────────────────────────────


def load_denials(path: Path | None = None) -> list[DenialRecord]:
    """Read all denials from disk. Order is preserved; the latest
    appended denial appears last.

    Returns an empty list on missing file or unparseable content —
    we never raise, because a missing deny log is the common
    case (most operators never issue an explicit deny)."""
    target = path if path is not None else denials_path()
    if not target.exists():
        return []
    out: list[DenialRecord] = []
    try:
        with target.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                tid = payload.get("trace_id")
                if not isinstance(tid, str) or not tid:
                    continue
                ts = payload.get("ts_ns")
                if not isinstance(ts, int):
                    continue
                out.append(DenialRecord(
                    trace_id=tid,
                    ts_ns=ts,
                    note=str(payload.get("note", "")),
                ))
    except OSError:
        return []
    return out


def load_denial_trace_ids(
    path: Path | None = None,
) -> frozenset[str]:
    """Convenience: just the set of denied trace_ids. The learner
    uses this rather than the full record list because it only
    needs the membership check."""
    return frozenset(d.trace_id for d in load_denials(path=path))


__all__ = [
    "DenialRecord",
    "append_denial",
    "denials_path",
    "load_denial_trace_ids",
    "load_denials",
]
