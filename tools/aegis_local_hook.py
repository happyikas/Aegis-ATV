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
# Model the host is actually serving — feeds the cost FLOPS table so
# CostEfficiencyMetrics.cumulative_dollars and step335 budget gate
# evaluate against the right $/token. Override per-deployment.
MODEL_FOR_COST = os.environ.get("AEGIS_MODEL_FOR_COST", "claude-haiku-4-5")
LOCAL_AUDIT_PATH = Path(
    os.environ.get(
        "AEGIS_LOCAL_AUDIT", str(Path.home() / ".aegis" / "audit.jsonl")
    )
)
# ATMU 2PC intent log (M10) — per-process SQLite WAL. Disabled when
# AEGIS_ATMU_DISABLE=1 so users running on read-only file systems can
# opt out without touching code. Default path: ~/.aegis/intent_log.sqlite.
LOCAL_INTENT_LOG_PATH = Path(
    os.environ.get(
        "AEGIS_INTENT_LOG_DB", str(Path.home() / ".aegis" / "intent_log.sqlite")
    )
)
ATMU_DISABLED = os.environ.get("AEGIS_ATMU_DISABLE", "0") == "1"


def _emit(msg: str) -> None:
    print(f"[aegis-local] {msg}", file=sys.stderr, flush=True)


def _build_explain_block(atv: Any, inp: Any, verdict: Any) -> dict[str, Any]:
    """Per-decision diagnostic block written into each audit record.

    Captures every signal the firewall *consulted* (not just the one
    that decided), so ``aegis report --explain TRACE`` can render a
    layer-by-layer "why" for any past decision. Every field is a pure
    function of the verdict / inp / atv tuple — we're just lifting
    information that already exists in memory at PreToolUse time so
    it survives in the audit chain instead of being lost.

    Fields written
    --------------
    * ``step_traces``   — the ``Verdict.step_traces`` dict, filtered
      to entries that are non-trivial (skipped / ok-only entries are
      noise; we keep BLOCK / REQUIRE_APPROVAL / hybrid / numeric
      payloads).
    * ``m13_top``       — top-5 (subfield, score) pairs from the M13
      attribution head, computed against the current ATV. Recomputed
      here rather than lifted from the verdict because step340 only
      sometimes embeds it in the reason string.
    * ``rag``           — count, top cosine, top label of the cases
      retrieved by step340 RAG (when BGE + memory are configured).
      ``null`` when RAG was inactive.
    * ``session_drift`` — ``topic_drift`` and ``n_calls`` for the
      session this call belongs to (when bge-local + session_id
      present, otherwise ``null``).
    * ``atv_dim`` / ``atv_sha3`` — sanity-check shape + content hash
      for downstream replay tools.

    Try/except around every sub-section: if any signal-gathering
    fails (e.g. M13 weights file unreadable), the block degrades to
    fewer fields rather than blocking the tool call.
    """
    explain: dict[str, Any] = {}

    # ── ATV shape + content fingerprint ──────────────────────────────
    try:
        import hashlib

        import numpy as np
        atv_arr = np.asarray(atv)
        explain["atv_dim"] = int(atv_arr.shape[0])
        explain["atv_sha3"] = hashlib.sha3_256(atv_arr.tobytes()).hexdigest()
    except Exception:  # noqa: BLE001
        pass

    # ── Step traces (filtered) ───────────────────────────────────────
    try:
        traces = dict(getattr(verdict, "step_traces", {}) or {})
        # Drop entries that are pure "ok" / "skipped" — they're noise.
        # Keep anything carrying numbers, BLOCK / REQUIRE_APPROVAL, or
        # hybrid output strings (which include attribution breakdowns).
        keep = {}
        for k, v in traces.items():
            if not isinstance(v, str):
                continue
            low = v.lower()
            keep_this = (
                "block" in low or "approval" in low or "hybrid" in low
                or "drift" in low or "loop" in low
                or any(c.isdigit() for c in v)
            )
            if keep_this:
                keep[k] = v[:200]
        explain["step_traces"] = keep
    except Exception:  # noqa: BLE001
        pass

    # ── M13 top-5 attribution ───────────────────────────────────────
    try:
        from aegis.judge.attribution_head import AttributionHead
        head = AttributionHead()
        v = head.evaluate_full("", atv=atv, inp=inp)
        attribution = dict(getattr(v, "subfield_attribution", {}) or {})
        if attribution:
            top = sorted(
                attribution.items(), key=lambda kv: -float(kv[1]),
            )[:5]
            explain["m13_top"] = [
                {"subfield": name, "score": round(float(score), 4)}
                for name, score in top
            ]
            explain["m13_score"] = round(
                float(getattr(v, "confidence", 0.0)), 4,
            )
    except Exception:  # noqa: BLE001
        pass

    # ── RAG retrieval ────────────────────────────────────────────────
    try:
        from aegis.config import settings as _settings
        if _settings.aegis_embedding_provider == "bge-local":
            import numpy as np

            from aegis.judge.case_memory import load_default_memory
            from aegis.schema import SLICE_AGENT_STATE_EMBEDDING
            mem = load_default_memory()
            if not mem.is_empty:
                q = np.asarray(
                    atv[SLICE_AGENT_STATE_EMBEDDING], dtype=np.float32,
                )
                hits = mem.search(q, k=3) if q.size == mem.dim else []
                if hits:
                    explain["rag"] = {
                        "n_retrieved": len(hits),
                        "top_cos": round(float(hits[0].similarity), 4),
                        "top_label": str(hits[0].label),
                        "top_text": str(hits[0].text)[:120],
                    }
    except Exception:  # noqa: BLE001
        pass

    # ── Session drift snapshot ───────────────────────────────────────
    try:
        from aegis.atv.session_drift import load_session
        sid = getattr(inp.header, "aid", "") or ""
        # Heuristic: aid in our local hook is the Claude Code session_id
        # (set in adapter._tool_args_to_input). Look for the session
        # state directly.
        state = load_session(sid)
        if state is not None and state.drift_history:
            explain["session_drift"] = {
                "topic_drift": round(float(state.drift_history[-1]), 4),
                "max_drift": round(float(max(state.drift_history)), 4),
                "n_calls": int(state.n_calls),
            }
    except Exception:  # noqa: BLE001
        pass

    return explain


_INTENT_LOG_SINGLETON: Any = None


def _get_intent_log() -> Any:
    """Lazy per-process IntentLog singleton.

    Returns ``None`` when ATMU is disabled or initialisation fails so
    every caller can short-circuit safely. The hook must NEVER block
    a tool call because of intent-log trouble — its job is forensic
    bookkeeping, not gating.
    """
    global _INTENT_LOG_SINGLETON
    if ATMU_DISABLED:
        return None
    if _INTENT_LOG_SINGLETON is not None:
        return _INTENT_LOG_SINGLETON
    try:
        LOCAL_INTENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        from aegis.atmu import IntentLog

        _INTENT_LOG_SINGLETON = IntentLog(str(LOCAL_INTENT_LOG_PATH))
        return _INTENT_LOG_SINGLETON
    except Exception as e:  # noqa: BLE001 — never block on infra failure
        if VERBOSE:
            _emit(f"ATMU disabled (init failed): {e}")
        return None


def _atmu_open_intent(inp: Any, atv: Any) -> str | None:
    """Phase 1 of 2PC — record TENTATIVE intent before the firewall runs.

    The ``record_id`` is the deterministic ``span_id`` so the
    PostToolUse hook (a separate process) can recompute and close
    the same record without a mapping table.

    Returns the record_id on success, ``None`` if ATMU is disabled
    or the append failed (in which case the caller proceeds without
    transactional bookkeeping).
    """
    log = _get_intent_log()
    if log is None:
        return None
    try:
        import hashlib

        from aegis.atmu.checkpoint import make_checkpoint
        from aegis.atmu.compensating import plan_for
        from aegis.firewall.step320_blast import (
            TOOL_BLAST_TABLE,
            UNKNOWN_TOOL_BLAST,
        )

        args_hash = hashlib.sha3_256(
            inp.tool_args_json.encode("utf-8")
        ).hexdigest()
        atv_commitment = hashlib.sha3_256(atv.tobytes()).hexdigest()
        blast = TOOL_BLAST_TABLE.get(inp.tool_name, UNKNOWN_TOOL_BLAST)
        checkpoint = make_checkpoint(inp, blast)
        rec = log.append_tentative(
            aid=inp.header.aid,
            tenant_id=inp.header.tenant_id,
            trace_id=inp.header.trace_id,
            span_id=inp.header.span_id,
            parent_span_id=inp.header.parent_span_id,
            tool_name=inp.tool_name,
            tool_args_hash=args_hash,
            blast_radius=blast,
            atv_commitment=atv_commitment,
            checkpoint_id=(
                checkpoint["checkpoint_id"] if checkpoint else None
            ),
            cost_profile=inp.header.cost_attestation_profile,
            record_id=inp.header.span_id,
        )
        comp = plan_for(inp.tool_name)
        if comp is not None:
            log.set_compensation_plan(rec["record_id"], comp)
        return str(rec["record_id"])
    except Exception as e:  # noqa: BLE001
        if VERBOSE:
            _emit(f"ATMU open_intent failed: {e}")
        return None


def _atmu_finalize_intent(record_id: str | None, decision: str, reason: str) -> None:
    """Phase 1.5 of 2PC — drive the intent record to its post-firewall state.

    * ``ALLOW``           — TENTATIVE → PREPARED → COMMITTED
    * ``REQUIRE_APPROVAL`` — TENTATIVE → PREPARED (committed at /approve time)
    * ``BLOCK``            — TENTATIVE → ABORTED
    """
    if record_id is None:
        return
    log = _get_intent_log()
    if log is None:
        return
    try:
        from aegis.atmu import TxState

        if decision == "BLOCK":
            log.transition(
                record_id,
                new_state=TxState.ABORTED,
                reason=f"firewall block: {reason}"[:200],
            )
        else:
            log.transition(
                record_id,
                new_state=TxState.PREPARED,
                reason=f"firewall {decision.lower()}",
            )
            if decision == "ALLOW":
                log.transition(
                    record_id,
                    new_state=TxState.COMMITTED,
                    reason="audit signed, host may execute",
                )
    except Exception as e:  # noqa: BLE001
        if VERBOSE:
            _emit(f"ATMU finalize_intent failed: {e}")


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

    from aegis.atv.adapter import (
        from_claude_code_payload,
        from_claude_code_payload_enhanced,
    )
    from aegis.atv.builder import build_atv
    from aegis.firewall.core import run_firewall

    t0 = time.perf_counter_ns()
    # Transcript-aware ATV builder when Claude Code passed transcript_path
    # (PreToolUse always does in v1.0+). Populates agent_state_text /
    # plan_text / recent_actions / memory_fingerprint / cost_estimate /
    # session_behavior — making step335 budget gate, novelty, and
    # session-drift signals real instead of zero. Falls back to the
    # sparse builder on missing / unparseable transcript so the hook
    # NEVER blocks a tool call because of transcript I/O.
    try:
        inp = from_claude_code_payload_enhanced(
            event, tenant_id=TENANT, model_for_cost=MODEL_FOR_COST,
        )
    except Exception as e:  # noqa: BLE001 — fall back, never block
        if VERBOSE:
            _emit(f"enhanced adapter failed ({e}) — falling back to sparse")
        inp = from_claude_code_payload(event, tenant_id=TENANT)

    # v2.3 HW telemetry — AEGIS_HW_PROVIDER=sim populates the 200-D HW
    # band so step337 + M12 cost-divergence can fire on real signals.
    # Returns None (zero-fill) when the env var is unset/none, matching
    # the sidecar's behaviour in src/aegis/api/evaluate.py.
    hw_counters = None
    try:
        from aegis.hw_telemetry.simulator import simulate_from_env

        hw_counters = simulate_from_env(inp)
    except Exception:  # noqa: BLE001 — HW telemetry must never block
        hw_counters = None

    atv: np.ndarray = build_atv(inp, hw=hw_counters)

    # M10 ATMU phase 1 — record TENTATIVE intent BEFORE the firewall.
    # Returns ``None`` if ATMU is disabled / initialisation failed; the
    # rest of the path runs unchanged in that case.
    intent_record_id = _atmu_open_intent(inp, atv)

    verdict = run_firewall(atv, inp, atv_id=inp.header.span_id)

    # M10 ATMU phase 1.5 — transition based on the firewall verdict.
    _atmu_finalize_intent(intent_record_id, verdict.decision, verdict.reason or "")

    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

    tool_name = event.get("tool_name", "") or inp.tool_name
    decision = verdict.decision
    reason = verdict.reason or ""

    # Build the audit record. The base fields are unchanged from the
    # pre-#26 schema (`aegis verify-audit` and any downstream parsers
    # keep working). The new ``explain`` block carries the per-decision
    # signals so ``aegis report --explain`` can render the full "why"
    # — step traces, M13 attribution, RAG cases, session drift. Every
    # signal is a pure function of (atv, inp, verdict), so adding them
    # here doesn't change firewall behaviour.
    explain_block = _build_explain_block(atv, inp, verdict)
    if intent_record_id is not None:
        explain_block["intent_record_id"] = intent_record_id
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
            "explain": explain_block,
        }
    )

    # Burn-in Shadow recording (opt-in via AEGIS_BURNIN_SHADOW=1).
    # Records the (ATVInput, verdict) pair for later M13 v2 retraining.
    # The shadow module is a no-op when the env flag is unset, so this
    # adds zero cost to the default Solo Free hot path.
    try:
        from aegis.burnin.shadow import record as _shadow_record
        _shadow_record(inp, verdict)
    except Exception:  # noqa: BLE001 — shadow must never block the tool
        pass

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
