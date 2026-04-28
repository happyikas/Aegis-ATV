#!/usr/bin/env python3
"""Claude Code PreToolUse hook — in-process firewall (Phase 5, --mode local).

Donor: aegis-mvp v1.0.0 ``claude_hooks/pretool.py`` (in-process pattern),
adapted to MVP's 30-subfield ATV-2080-v1 firewall.

Solo Free deployment: Claude Code calls this script for every tool;
the script builds an :class:`aegis.schema.ATVInput` from the hook
payload, runs the firewall pipeline (310 → 311 → 312 → 320 → 330 → 335
→ 340) in-process, and signals the verdict back via exit code:

* ``0``  ALLOW             — tool runs.
* ``2``  BLOCK / REQUIRE_APPROVAL (when AEGIS_APPROVE_AS_BLOCK=1, the
                              default) — Claude Code aborts the tool
                              and surfaces the stderr message.

No HTTP, no docker, no audit signing — pure firewall in process.
:func:`aegis.cost.transcript.import_into_wal` is invoked separately by
the Stop hook (``tools/hooks/session_end.py``) for cost back-fill.

Env vars::

    AEGIS_TENANT_ID         claude-code-local   tag for every record
    AEGIS_LOCAL_AUDIT       ~/.aegis/audit.jsonl   per-call decision log
    AEGIS_APPROVE_AS_BLOCK  1                   set 0 to let
                                                  REQUIRE_APPROVAL pass
                                                  with a stderr warning
                                                  instead of blocking
    AEGIS_HOOK_VERBOSE      0                   1 → print ALLOWs to stderr
    AEGIS_POLICY_DIR        ./policies          path to sensitive_paths.json
                                                  + safe_bash_subcommands.json

The ``aegis install --mode local`` command (D3 / Phase 5) embeds the
right ``AEGIS_POLICY_DIR`` and ``PYTHONPATH`` into the registered hook
command line, so users never set these by hand.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

TENANT = os.environ.get("AEGIS_TENANT_ID", "claude-code-local")
APPROVE_AS_BLOCK = os.environ.get("AEGIS_APPROVE_AS_BLOCK", "1") == "1"
VERBOSE = os.environ.get("AEGIS_HOOK_VERBOSE", "0") == "1"
LOCAL_AUDIT_PATH = Path(
    os.environ.get(
        "AEGIS_LOCAL_AUDIT", str(Path.home() / ".aegis" / "audit.jsonl")
    )
)


def _emit(msg: str) -> None:
    print(f"[aegis-local] {msg}", file=sys.stderr, flush=True)


def _append_audit(record: dict[str, Any]) -> None:
    """Append a chained audit record (v2.1.5 local-mode integrity).

    Each line carries ``prev_hash`` linking to the previous line's
    ``this_hash``, plus its own SHA3-256 ``this_hash``. Tampering
    with any historical line breaks every subsequent recompute, so
    ``aegis verify-audit`` (local mode) catches mutations.
    """
    try:
        from aegis.audit.local_chain import append as chain_append

        chain_append(LOCAL_AUDIT_PATH, record)
    except OSError:
        # Audit failure must never block the user's tool call.
        pass


def handle_pretool(stdin: Any, stdout: Any) -> int:
    raw = stdin.read()
    if not raw or not raw.strip():
        if VERBOSE:
            _emit("no stdin payload — allowing")
        return 0
    try:
        event: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        _emit(f"invalid PreToolUse JSON ({e}) — allowing")
        return 0

    if event.get("hook_event_name") not in (None, "", "PreToolUse"):
        return 0

    # Lazy imports keep startup small for the no-stdin / malformed case.
    import numpy as np

    from aegis.atv.adapter import from_claude_code_payload
    from aegis.atv.builder import build_atv
    from aegis.firewall.core import run_firewall

    t0 = time.perf_counter_ns()
    inp = from_claude_code_payload(event, tenant_id=TENANT)
    atv: np.ndarray = build_atv(inp)
    verdict = run_firewall(atv, inp, atv_id=inp.header.span_id)
    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

    tool_name = event.get("tool_name", "") or inp.tool_name
    decision = verdict.decision
    reason = verdict.reason or ""

    _append_audit(
        {
            "ts_ns": time.time_ns(),
            "tool": tool_name,
            "aid": inp.header.aid,
            "decision": decision,
            "reason": reason,
            "trace_id": inp.header.trace_id,
            "latency_ms": round(elapsed_ms, 3),
            "mode": "local",
        }
    )

    if decision == "ALLOW":
        if VERBOSE:
            _emit(
                f"ALLOW  {tool_name}  trace={inp.header.trace_id[:8]}  "
                f"({elapsed_ms:.1f}ms)"
            )
        return 0

    if decision == "REQUIRE_APPROVAL" and not APPROVE_AS_BLOCK:
        _emit(
            f"WARN   {tool_name} would REQUIRE_APPROVAL — letting through "
            f"(AEGIS_APPROVE_AS_BLOCK=0)\n"
            f"           reason: {reason}"
        )
        return 0

    _emit(
        f"{decision}  {tool_name}  trace={inp.header.trace_id[:8]}  "
        f"({elapsed_ms:.1f}ms)\n"
        f"           reason: {reason}"
    )
    return 2


def main() -> int:
    return handle_pretool(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
