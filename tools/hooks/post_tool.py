#!/usr/bin/env python3
"""Claude Code PostToolUse hook — capture tool result + close ATMU intent.

Fires after every tool execution. Reads the same payload as PreToolUse
plus a ``tool_response`` field carrying the tool's actual output. We
use it to:

1. Close the ATMU intent record (committed / aborted) that PreToolUse
   opened — this is the second phase of 2PC (Claim 2/15).
2. Optionally POST to ``/tool-outcome`` so the perf-feedback EWMA
   (v3.2) gets fresh measurements.
3. Append a PostToolUse audit event so the chain reflects actual
   execution (PreToolUse only logs the *intent*).

Install via ``aegis install`` (which registers all hooks) or manually
in ``~/.claude/settings.json``:

    {
      "hooks": {
        "PostToolUse": [{
          "hooks": [{
            "type": "command",
            "command": "python3 /ABS/PATH/MVP/tools/hooks/post_tool.py"
          }]
        }]
      }
    }

Failure modes
-------------
This hook **never blocks Claude Code** — exit code is always 0.
Errors are logged to stderr and swallowed. The Pre-hook already
gated execution; PostTool's job is forensic capture only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

LOCAL_AUDIT_PATH = Path(
    os.environ.get(
        "AEGIS_LOCAL_AUDIT", str(Path.home() / ".aegis" / "audit.jsonl")
    )
)
LOCAL_INTENT_LOG_PATH = Path(
    os.environ.get(
        "AEGIS_INTENT_LOG_DB", str(Path.home() / ".aegis" / "intent_log.sqlite")
    )
)
ATMU_DISABLED = os.environ.get("AEGIS_ATMU_DISABLE", "0") == "1"
SIDECAR_URL = os.environ.get("AEGIS_SIDECAR_URL", "")
TOOL_OUTCOME_TIMEOUT_S = float(os.environ.get("AEGIS_POST_TIMEOUT_S", "1.0"))


def _emit(msg: str) -> None:
    print(f"[aegis-post] {msg}", file=sys.stderr, flush=True)


def _append_audit(record: dict[str, Any]) -> None:
    """Append to the local SHA3-chained audit log (same as PreToolUse)."""
    try:
        from aegis.audit.local_chain import append as chain_append

        chain_append(LOCAL_AUDIT_PATH, record)
    except OSError:
        pass


def _atmu_record_id_from_invocation(invocation_id: str) -> str:
    """Recompute the deterministic record_id PreToolUse used.

    Mirrors :func:`aegis.atv.adapter._trace_ids_from` — span_id is the
    second 16 hex chars of ``sha3_256(invocation_id)``. The PreToolUse
    hook passes that span_id to ``IntentLog.append_tentative`` as the
    record_id, so PostToolUse can find the same row without needing
    a mapping table.
    """
    h = hashlib.sha3_256(invocation_id.encode("utf-8")).hexdigest()
    return h[32:48]


def _atmu_close_intent(
    invocation_id: str, status: str, result_hash: str
) -> None:
    """Phase 2 of 2PC — attach the tool outcome to the intent record.

    Best-effort: never raises. ATMU disabled, missing DB, or unknown
    record (e.g. PreToolUse was bypassed) all result in a silent
    no-op so PostToolUse never blocks Claude Code.
    """
    if ATMU_DISABLED or not invocation_id:
        return
    try:
        from aegis.atmu import IntentLog

        record_id = _atmu_record_id_from_invocation(invocation_id)
        if not LOCAL_INTENT_LOG_PATH.exists():
            return
        log = IntentLog(str(LOCAL_INTENT_LOG_PATH))
        try:
            log.append_tool_outcome(
                record_id, status=status, result_hash=result_hash
            )
        finally:
            log.close()
    except Exception:  # noqa: BLE001 — forensic-only, must not crash
        pass


def _post_tool_outcome(payload: dict[str, Any]) -> None:
    """Best-effort POST to the sidecar's /tool-outcome endpoint.

    Skipped silently when AEGIS_SIDECAR_URL is unset (local-only mode).
    """
    if not SIDECAR_URL:
        return
    try:
        import urllib.request

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{SIDECAR_URL.rstrip('/')}/tool-outcome",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TOOL_OUTCOME_TIMEOUT_S):
            pass
    except Exception:  # noqa: BLE001 — never crash on transport failure
        pass


def _result_hash(tool_response: Any) -> str:
    """SHA3-256 of the tool's response — commit hash of side-effect."""
    try:
        body = json.dumps(
            tool_response, sort_keys=True, default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        body = repr(tool_response).encode("utf-8")
    return hashlib.sha3_256(body).hexdigest()


def _classify_status(tool_response: Any, exit_code: int | None) -> str:
    """Map Claude Code's tool result into ATMU status enum.

    success | failure | timeout | partial — 4 of the 5 ATMU
    terminal states. ``compensated`` is set later by an explicit
    rollback CLI / API call.
    """
    if exit_code is not None:
        if exit_code == 0:
            return "success"
        if exit_code in (124, 137, 143):  # timeout signals
            return "timeout"
        return "failure"
    # Heuristic on tool_response: presence of "error" key = failure
    if isinstance(tool_response, dict) and (
        tool_response.get("is_error") or tool_response.get("error")
    ):
        return "failure"
    if tool_response is None:
        return "partial"
    return "success"


def handle_posttool(stdin: Any, stdout: Any) -> int:
    raw = stdin.read()
    if not raw or not raw.strip():
        return 0
    try:
        event: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    if event.get("hook_event_name") not in (None, "", "PostToolUse"):
        return 0

    session_id = event.get("session_id", "")
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    tool_response = event.get("tool_response")
    exit_code_raw = event.get("exit_code")
    exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    invocation_id = event.get("invocation_id", "")

    rh = _result_hash(tool_response)
    status = _classify_status(tool_response, exit_code)
    now_ns = time.time_ns()

    # Run all four PostToolUse analyzers (classification, backtrack,
    # redundancy, duration). Wrapped in try/except — analysis failure
    # must never crash the hook (forensic-only contract).
    post_analysis_block: dict[str, Any] | None = None
    try:
        from aegis.cost.post_analysis import (
            analyse_post_tool_event,
            to_audit_dict,
        )
        record_id = (
            _atmu_record_id_from_invocation(invocation_id)
            if invocation_id else None
        )
        analysis = analyse_post_tool_event(
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            tool_response=tool_response,
            exit_code=exit_code,
            audit_path=LOCAL_AUDIT_PATH,
            intent_log_path=LOCAL_INTENT_LOG_PATH,
            record_id=record_id,
        )
        post_analysis_block = to_audit_dict(
            analysis,
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else {},
        )
    except Exception:  # noqa: BLE001 — never crash the hook
        post_analysis_block = None

    record = {
        "ts_ns": now_ns,
        "tool": tool_name,
        "aid": session_id,
        "invocation_id": invocation_id,
        "hook": "PostToolUse",
        "status": status,
        "result_hash": rh,
        "exit_code": exit_code,
        "tool_input_keys": sorted(tool_input.keys()) if isinstance(tool_input, dict) else [],
        "mode": "local",
    }
    explain_block: dict[str, Any] = {}
    if post_analysis_block is not None:
        # Live next to step traces in the explain block so `aegis report
        # --explain` can render it; sits in `explain.post_analysis` so
        # downstream tools that already walk `explain` see it without
        # changes.
        explain_block["post_analysis"] = post_analysis_block

    # v2.7 PR-ψ-retrospective — compare PreToolUse advice (predicted)
    # vs the actual tool outcome (observed here). Best-effort: if the
    # PreToolUse advice can't be located, the block is omitted. Never
    # raises — PostToolUse is forensic-only.
    try:
        from aegis.judge.retrospective import (
            evaluate_retrospective,
            retrospective_to_dict,
        )

        retrospective = evaluate_retrospective(
            invocation_id=invocation_id or "",
            tool_name=tool_name,
            actual_status=status,  # type: ignore[arg-type]
            audit_path=LOCAL_AUDIT_PATH,
        )
        if retrospective is not None:
            explain_block["retrospective_advice"] = (
                retrospective_to_dict(retrospective)
            )
            # Surface notable mismatches on stderr so the operator sees
            # them in real time (Claude Code only shows stderr).
            if retrospective.accuracy in ("missed_signal", "false_alarm"):
                _emit(
                    f"retrospective: {retrospective.accuracy} — "
                    f"{retrospective.notes}"
                )
    except Exception:  # noqa: BLE001 — forensic-only, never crash
        pass

    if explain_block:
        record["explain"] = explain_block
    _append_audit(record)

    # M10 ATMU phase 2 — attach the tool_outcome to the intent record
    # opened by PreToolUse (2PC commit/abort half). Local-only; sidecar
    # mode handles this via the ``/tool-outcome`` POST below.
    _atmu_close_intent(invocation_id, status, rh)

    # Best-effort sidecar /tool-outcome POST so perf EWMA updates.
    _post_tool_outcome({
        "record_id": invocation_id or session_id,
        "status": status,
        "result_hash": rh,
        "tenant_id": os.environ.get("AEGIS_TENANT_ID", "claude-code-local"),
        "aid": session_id,
        # PostToolUse doesn't carry latency/token info — leave to host
        # to report via /tool-outcome with explicit metrics.
    })

    return 0


def main() -> int:
    return handle_posttool(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
