"""Parse Claude Code transcript ``.jsonl`` to extract per-message usage (D5).

Donor: aegis-mvp v1.0.0 ``cost/transcript.py``.

Transcript line shapes (Claude Code 1.x)::

    {"type":"user_message", ...}
    {"type":"assistant_message",
     "message":{"usage":{"input_tokens":N, "output_tokens":N,
                          "cache_read_input_tokens":N,
                          "cache_creation_input_tokens":N},
                "model":"claude-sonnet-4-6", ...},
     "tool_uses":[{"id":"toolu_…","name":"Bash","input":{...}}]}
    {"type":"tool_result", "tool_use_id":"toolu_…", ...}

Strategy:

* Iterate transcript chronologically.
* For each ``assistant_message`` with usage, record the per-turn token
  counts and the list of ``tool_use_id`` values it produced.
* :func:`import_into_wal` is the entry point :class:`tools.aegis_cli`'s
  ``cost-import transcript`` calls. The donor wired this directly into
  its own WAL's ``outcomes`` table; under MVP/, the M12 Cost
  Attestation Ledger has a different (signed, ATV-anchored) shape, so
  the ledger write is staged via :data:`ledger_writer`. The default
  writer is a no-op — Phase 5 binds it to
  :class:`aegis.cost.ledger.CostAttestationLedger`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any


def _stream(path: Path) -> Iterator[dict[str, Any]]:
    with path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _usage_of(rec: dict[str, Any]) -> dict[str, Any] | None:
    msg = rec.get("message") or {}
    u = msg.get("usage") or rec.get("usage")
    if not u:
        return None
    return {
        "input_tokens": int(u.get("input_tokens", 0)),
        "output_tokens": int(u.get("output_tokens", 0)),
        "cache_read": int(u.get("cache_read_input_tokens", 0)),
        "cache_creation": int(u.get("cache_creation_input_tokens", 0)),
        "model": msg.get("model") or rec.get("model", ""),
    }


def _tool_uses(rec: dict[str, Any]) -> list[dict[str, Any]]:
    msg = rec.get("message") or {}
    uses_raw = msg.get("tool_uses") or rec.get("tool_uses") or []
    uses: list[dict[str, Any]] = list(uses_raw) if isinstance(uses_raw, list) else []
    # Newer schema embeds tool_use entries in message.content[*]
    content = msg.get("content") or []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                uses.append(c)
    return uses


def parse_transcript(path: Path) -> list[dict[str, Any]]:
    """Return per-assistant-turn usage with the tool_use_ids it produced.

    Each turn dict contains ``model``, ``input_tokens``, ``output_tokens``,
    ``cache_read``, ``cache_creation`` and ``tool_use_ids: list[str]``.
    """
    turns: list[dict[str, Any]] = []
    for rec in _stream(path):
        if rec.get("type") not in ("assistant_message", "assistant"):
            continue
        usage = _usage_of(rec)
        if not usage:
            continue
        tu = _tool_uses(rec)
        turns.append(
            {
                **usage,
                "tool_use_ids": [t.get("id") for t in tu if t.get("id")],
            }
        )
    return turns


def _no_op_writer(_turns: list[dict[str, Any]], _session_id: str) -> dict[str, Any]:
    """Default :data:`ledger_writer`: returns counts without writing.

    Phase 5 (plugin packaging) replaces this with a writer that calls
    :class:`aegis.cost.ledger.CostAttestationLedger` so each transcript
    turn produces a signed cost attestation record.
    """
    n_tool_attributed = sum(len(t.get("tool_use_ids", [])) for t in _turns)
    n_aggregate = sum(1 for t in _turns if not t.get("tool_use_ids"))
    return {
        "tool_attributed": n_tool_attributed,
        "aggregate": n_aggregate,
        "total_usd": 0.0,
    }


# Hook: ``ledger_writer(turns, session_id) -> dict`` writes parsed turns
# to a cost backend and returns aggregated counts. Defaults to a parse-only
# no-op writer; Phase 5 rebinds to a CostAttestationLedger-backed writer.
ledger_writer: Callable[[list[dict[str, Any]], str], dict[str, Any]] = _no_op_writer


def import_into_wal(path: Path, *, session_id: str = "") -> dict[str, Any]:
    """Parse a transcript and dispatch parsed turns to :data:`ledger_writer`.

    The shape returned matches the donor's contract so the
    ``aegis cost-import transcript`` CLI subcommand keeps working.
    """
    if not path.exists():
        return {"status": "no_transcript", "path": str(path)}
    turns = parse_transcript(path)
    counts = ledger_writer(turns, session_id)
    return {
        "status": "imported",
        "turns": len(turns),
        "tool_attributed": counts.get("tool_attributed", 0),
        "aggregate": counts.get("aggregate", 0),
        "total_usd": counts.get("total_usd", 0.0),
        "path": str(path),
    }
