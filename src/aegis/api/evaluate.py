"""POST /evaluate — main entrypoint that composes the Firewall.

Stages, per patent v7.10 §5 + §5A:

    build_atv                          (M8)
    ATMU.append_tentative              (M10)
    run_firewall (310..340)            (existing)
    ATMU.transition → prepared/aborted (M10)
    step 350  approval dispatch        (M9, for REQUIRE_APPROVAL)
    step 360  sign + append to audit   (M9)
    ATMU.transition → committed        (M10, after sign)
    step 370  exec-gate annotation     (M9)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter

from aegis.atmu import (
    IntentLog,
    TxState,
    make_checkpoint,
    plan_for,
)
from aegis.atv.builder import build_atv
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.firewall import step350_approval, step360_audit, step370_exec
from aegis.firewall.core import run_firewall
from aegis.firewall.step320_blast import TOOL_BLAST_TABLE, UNKNOWN_TOOL_BLAST
from aegis.schema import ATVInput, Verdict


def _evaluate_impl(
    inp: ATVInput,
    *,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
    intent_log: IntentLog | None = None,
    burn_in_id: str | None = None,
) -> Verdict:
    # Auto-fill the Burn-in id into the ATV header so every audit record
    # carries the measurement of the software that produced it. Caller-
    # supplied burn_in_id always wins (useful for cross-attesting a
    # different deployment).
    if burn_in_id and not inp.header.burn_in_id:
        inp = inp.model_copy(
            update={"header": inp.header.model_copy(update={"burn_in_id": burn_in_id})}
        )

    # M8: build the 30-subfield ATV
    atv = build_atv(inp)
    atv_id = str(uuid.uuid4())
    atv_commitment = hashlib.sha3_256(atv.tobytes()).hexdigest()
    args_hash = hashlib.sha3_256(inp.tool_args_json.encode("utf-8")).hexdigest()
    blast = TOOL_BLAST_TABLE.get(inp.tool_name, UNKNOWN_TOOL_BLAST)

    # M10: ATMU — record tentative intent BEFORE firewall runs (§5A)
    intent_record_id: str | None = None
    if intent_log is not None:
        checkpoint = make_checkpoint(inp, blast)
        rec = intent_log.append_tentative(
            aid=inp.header.aid,
            tenant_id=inp.header.tenant_id,
            trace_id=inp.header.trace_id,
            span_id=inp.header.span_id,
            parent_span_id=inp.header.parent_span_id,
            tool_name=inp.tool_name,
            tool_args_hash=args_hash,
            blast_radius=blast,
            atv_commitment=atv_commitment,
            checkpoint_id=checkpoint["checkpoint_id"] if checkpoint else None,
            cost_profile=inp.header.cost_attestation_profile,
        )
        intent_record_id = rec["record_id"]
        # For tools with irreversible side effects, attach a compensation
        # plan now so it's persisted alongside the intent.
        comp = plan_for(inp.tool_name)
        if comp is not None:
            intent_log.set_compensation_plan(intent_record_id, comp)

    # Firewall 310..340 (pre-commit gate)
    verdict = run_firewall(atv, inp, atv_id=atv_id)

    # M10: ATMU first transition — prepared (firewall passed) or aborted (block).
    # APPROVAL stays in TENTATIVE → PREPARED so the human can later commit.
    if intent_log is not None and intent_record_id:
        if verdict.decision == "BLOCK":
            intent_log.transition(
                intent_record_id, new_state=TxState.ABORTED,
                reason=f"firewall block: {verdict.reason}",
            )
        else:
            intent_log.transition(
                intent_record_id, new_state=TxState.PREPARED,
                reason=f"firewall {verdict.decision.lower()}",
            )

    # Step 350: REQUIRE_APPROVAL → notification dispatch
    step350_approval.dispatch(verdict, inp)

    # Step 360: canonical serialize + Ed25519 sign + append to audit log
    record = step360_audit.sign_and_append(
        atv=atv, verdict=verdict, inp=inp, key=key, db=db, log=log
    )
    verdict.signature = record["signature"]

    # M10: ATMU final transition — ALLOW commits, APPROVAL stays prepared
    # (committed at /approve time), BLOCK already aborted above.
    if intent_log is not None and intent_record_id and verdict.decision == "ALLOW":
        intent_log.transition(
            intent_record_id, new_state=TxState.COMMITTED,
            reason="audit signed, host may execute",
        )

    # Step 370: exec-recommendation annotation
    step370_exec.annotate(verdict)

    # Surface the ATMU record_id in the Verdict step_traces so the host
    # can refer to it later via /tool-outcome.
    if intent_record_id:
        verdict.step_traces["aegis.atmu.intent_log"] = (
            f"intent_record_id={intent_record_id}"
        )

    return verdict


def make_router(
    *,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
    intent_log: IntentLog | None = None,
    burn_in_id: str | None = None,
) -> APIRouter:
    r = APIRouter()

    @r.post("/evaluate", response_model=Verdict)
    def evaluate(inp: ATVInput) -> Verdict:
        return _evaluate_impl(
            inp, key=key, db=db, log=log,
            intent_log=intent_log, burn_in_id=burn_in_id,
        )

    return r
