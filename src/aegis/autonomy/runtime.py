"""Autonomy runtime — trust table persistence + Verdict bypass shim.

This module sits between the firewall and the hook surface. After
``run_firewall`` produces a Verdict, the hook calls
:func:`apply_autonomy_bypass` to consult the learned trust table.
If the verdict is REQUIRE_APPROVAL **and** matches a high-trust
pattern, the Verdict is downgraded to ALLOW with a permanent
``aegis.autonomy.step331.run`` stamp in ``step_traces`` so the
audit chain captures the bypass.

The hook side is opt-in via ``AEGIS_AUTONOMY_ENABLED=1``; when the
flag is off, :func:`apply_autonomy_bypass` returns the verdict
unchanged, preserving byte-identical legacy behaviour.

Trust table on disk
-------------------

Persisted to ``~/.aegis/autonomy/trust_table.json`` (override via
``AEGIS_AUTONOMY_TRUST_TABLE`` env). The JSON shape:

.. code-block:: json

    {
      "learned_at": "2026-05-16T...",
      "learned_from_records": 8500,
      "min_samples": 5,
      "min_clean_rate": 0.95,
      "patterns": [
        {
          "tool_name": "Bash",
          "reason_signature": "loop:Bash",
          "n_seen": 142,
          "n_followed_by_block": 3,
          "clean_rate": 0.979,
          "trust_score": 0.92,
          "last_seen_ns": 1778...,
          "sample_trace_ids": ["abc123", "def456", "ghi789"]
        },
        ...
      ]
    }

Re-learning is explicit (``aegis autonomy learn``); the trust
table never auto-evolves at runtime. This preserves the audit
property that "the autonomy decisions in this window come from
this exact trust table snapshot".
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from aegis.autonomy.learner import (
    MIN_TRUST_FOR_BYPASS,
    AutonomyVerdict,
    TrustedPattern,
    autonomy_enabled,
    evaluate_autonomy_request,
)
from aegis.schema import Verdict

# Stamp the firewall writes into step_traces on every bypass. The
# outlier walker keys on this prefix; keep the format stable.
STEP_TRACE_KEY = "aegis.autonomy.step331.run"
STEP_TRACE_PREFIX = "step331: auto-approved"


def trust_table_path() -> Path:
    """Return the canonical on-disk path for the trust table."""
    raw = os.environ.get("AEGIS_AUTONOMY_TRUST_TABLE", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "autonomy" / "trust_table.json"


def save_trust_table(
    table: dict[tuple[str, str], TrustedPattern],
    *,
    path: Path | None = None,
    learned_from_records: int = 0,
    min_samples: int = 0,
    min_clean_rate: float = 0.0,
) -> Path:
    """Persist the trust table to disk. Returns the path written.

    Atomic write via tempfile + rename so a concurrent reader
    never sees a partial file. The metadata fields (learned_at,
    learned_from_records, …) let the show/outliers commands
    explain *when* the table was built without re-mining."""
    target = path if path is not None else trust_table_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "learned_at": datetime.now(UTC).isoformat(),
        "learned_from_records": int(learned_from_records),
        "min_samples": int(min_samples),
        "min_clean_rate": float(min_clean_rate),
        "patterns": [asdict(p) for p in table.values()],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def load_trust_table(
    path: Path | None = None,
) -> dict[tuple[str, str], TrustedPattern]:
    """Read the trust table from disk. Returns an empty dict if
    the file doesn't exist or is malformed — both cases imply
    "no bypass" which is the safe default."""
    target = path if path is not None else trust_table_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    patterns_list = payload.get("patterns", [])
    if not isinstance(patterns_list, list):
        return {}
    out: dict[tuple[str, str], TrustedPattern] = {}
    for raw in patterns_list:
        if not isinstance(raw, dict):
            continue
        try:
            p = TrustedPattern(
                tool_name=str(raw["tool_name"]),
                reason_signature=str(raw["reason_signature"]),
                n_seen=int(raw["n_seen"]),
                n_followed_by_block=int(raw["n_followed_by_block"]),
                clean_rate=float(raw["clean_rate"]),
                trust_score=float(raw["trust_score"]),
                last_seen_ns=int(raw["last_seen_ns"]),
                sample_trace_ids=tuple(raw.get("sample_trace_ids", []) or ()),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out[p.key] = p
    return out


def trust_table_metadata(
    path: Path | None = None,
) -> dict[str, object]:
    """Return the on-disk metadata (learned_at, sample counts, …)
    without re-deserialising patterns. Empty dict on missing file."""
    target = path if path is not None else trust_table_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        k: payload.get(k) for k in (
            "learned_at", "learned_from_records",
            "min_samples", "min_clean_rate",
        )
    }


def apply_autonomy_bypass(
    verdict: Verdict,
    *,
    tool_name: str,
    reason: str,
    trust_table: dict[tuple[str, str], TrustedPattern] | None = None,
    min_trust: float = MIN_TRUST_FOR_BYPASS,
) -> tuple[Verdict, AutonomyVerdict]:
    """Consult the trust table; downgrade REQUIRE_APPROVAL to
    ALLOW when a high-trust pattern matches.

    Returns ``(new_verdict, autonomy_verdict)``. ``new_verdict``
    is either the original ``verdict`` (no bypass) or a copy with
    ``decision = "ALLOW"`` plus an additional step_trace entry
    keyed :data:`STEP_TRACE_KEY`. The ``autonomy_verdict`` carries
    the AutonomyVerdict diagnostic for forensics / logging
    regardless of which path was taken.

    Never raises. When ``AEGIS_AUTONOMY_ENABLED`` is unset, the
    function short-circuits at the top and returns ``(verdict,
    ask_human_verdict)`` without touching the trust table.
    """
    # Short-circuit when the operator hasn't opted in.
    if not autonomy_enabled():
        return verdict, AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason="AEGIS_AUTONOMY_ENABLED is off",
        )

    # Only REQUIRE_APPROVAL verdicts are candidates for bypass.
    if verdict.decision != "REQUIRE_APPROVAL":
        return verdict, AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason="verdict is not REQUIRE_APPROVAL",
        )

    table = trust_table if trust_table is not None else load_trust_table()
    av = evaluate_autonomy_request(
        tool_name=tool_name,
        reason=reason,
        trust_table=table,
        min_trust=min_trust,
    )
    if not av.auto_approve:
        return verdict, av

    # Build the new verdict — ALLOW with a stamped step_traces.
    assert av.matched_pattern is not None  # narrow for type checker
    stamp = (
        f"{STEP_TRACE_PREFIX} by trust table "
        f"tool={tool_name} signature={av.matched_pattern.reason_signature} "
        f"trust={av.matched_pattern.trust_score:.2f} "
        f"(was REQUIRE_APPROVAL: {verdict.reason!r})"
    )
    new_traces = dict(verdict.step_traces)
    new_traces[STEP_TRACE_KEY] = stamp
    new_verdict = Verdict(
        decision="ALLOW",
        reason=(
            f"auto-approved by autonomy bypass — pattern "
            f"{av.matched_pattern.reason_signature!r} trusted at "
            f"{av.matched_pattern.trust_score:.2f}"
        ),
        atv_id=verdict.atv_id,
        signature=verdict.signature,
        confidence=verdict.confidence,
        step_traces=new_traces,
        step_timings_us=verdict.step_timings_us,
    )
    return new_verdict, av


__all__ = [
    "STEP_TRACE_KEY",
    "STEP_TRACE_PREFIX",
    "apply_autonomy_bypass",
    "load_trust_table",
    "save_trust_table",
    "trust_table_metadata",
    "trust_table_path",
]
