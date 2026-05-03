"""Burn-in Shadow harness — record-only mode for collecting M13 training data.

The patent's Burn-in Shadow phase (¶[0079]) prescribes running the
firewall in **observation mode** — every tool call is allowed to
proceed (the hook returns exit 0), but the verdict that *would* have
been issued is logged with sufficient context to reconstruct the
input. Once enough shadow data is collected, the trainer in
``aegis.burnin.m13_train`` consumes it to learn v2+ weights from
real production traffic instead of synthetic seeds.

Wire format
-----------
Each line of the shadow log is one JSON object::

    {
      "ts_ns":          1714737610123456789,
      "tool_name":      "Bash",
      "tool_args_json": "{\\"command\\":\\"ls\\"}",
      "agent_state_text": "user wants to inspect dir",
      "plan_text":     "ls -la",
      "tenant_id":     "claude-code-local",
      "aid":           "agent-x",
      "trace_id":      "...",
      "span_id":       "...",
      "label":         "ALLOW" | "BLOCK" | "REQUIRE_APPROVAL",
      "reason":        "<the verdict reason from the hybrid judge>",
      "score":         0.62,
      "category":      "shadow"
    }

The schema matches what ``m13_train.py``'s ``--corpus`` mode reads:
load + train is a single command.

Defaults
--------
The shadow log lives at ``$AEGIS_SHADOW_LOG`` (env override) or
``~/.aegis/shadow.jsonl``. The hook script gates recording on the
``AEGIS_BURNIN_SHADOW`` env var being set to ``1`` so users opt in
explicitly — no surprise data collection.

Privacy: only the ATV-relevant fields are recorded (tool name + args
+ optional state text). No transcript content, no model output, no
full file contents — same surface step340 already audits.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from aegis.schema import ATVInput, Verdict


def _shadow_log_path() -> Path:
    raw = os.environ.get("AEGIS_SHADOW_LOG", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "shadow.jsonl"


def is_enabled() -> bool:
    """Returns True iff ``AEGIS_BURNIN_SHADOW=1`` (opt-in)."""
    return os.environ.get("AEGIS_BURNIN_SHADOW", "0") in ("1", "true", "True", "yes")


def record(inp: ATVInput, verdict: Verdict, *, score: float | None = None) -> None:
    """Append one shadow record. No-op when shadow mode is off.

    Called from the local-mode hook *after* the firewall verdict but
    *before* exit-code translation, so we capture the would-be verdict
    even when the hook is configured to never block (shadow mode is
    typically paired with ``AEGIS_APPROVE_AS_BLOCK=0`` or the
    hook-level pass-through).

    Errors are swallowed — shadow recording must never affect tool
    execution.
    """
    if not is_enabled():
        return
    path = _shadow_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts_ns": time.time_ns(),
            "tool_name": inp.tool_name,
            "tool_args_json": inp.tool_args_json,
            "agent_state_text": inp.agent_state_text,
            "plan_text": inp.plan_text,
            "tenant_id": inp.header.tenant_id,
            "aid": inp.header.aid,
            "trace_id": inp.header.trace_id,
            "span_id": inp.header.span_id,
            "label": verdict.decision,
            "reason": (verdict.reason or "")[:300],
            "score": float(score) if score is not None else None,
            "category": "shadow",
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:  # noqa: BLE001 — never block a tool call
        pass


def read_corpus(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read the shadow log into a list of dicts. Returns ``[]`` if absent.

    Used by the unit tests + the ``--corpus`` flag of
    ``aegis burnin train-m13`` (which converts each dict back into an
    ``ATVInput`` for feature extraction).
    """
    p = Path(path) if path else _shadow_log_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def shadow_stats(path: Path | str | None = None) -> dict[str, Any]:
    """Quick summary for ``aegis burnin shadow status``."""
    records = read_corpus(path)
    if not records:
        return {"n": 0, "by_label": {}, "earliest_ns": None, "latest_ns": None}
    by_label: dict[str, int] = {}
    for r in records:
        lab = r.get("label", "UNKNOWN")
        by_label[lab] = by_label.get(lab, 0) + 1
    return {
        "n": len(records),
        "by_label": by_label,
        "earliest_ns": min(r.get("ts_ns", 0) for r in records),
        "latest_ns": max(r.get("ts_ns", 0) for r in records),
    }


__all__ = [
    "is_enabled",
    "read_corpus",
    "record",
    "shadow_stats",
]
