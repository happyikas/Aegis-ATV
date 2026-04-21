"""POST /evaluate — main entrypoint that composes the Firewall.

Stages, per patent v7.10 §5:

    build_atv                          (M8)
    run_firewall (310..340)            (existing)
    step 350  approval dispatch        (M9, for REQUIRE_APPROVAL)
    step 360  sign + append to audit   (M9)
    step 370  exec-gate annotation     (M9)
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from aegis.atv.builder import build_atv
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.firewall import step350_approval, step360_audit, step370_exec
from aegis.firewall.core import run_firewall
from aegis.schema import ATVInput, Verdict


def _evaluate_impl(
    inp: ATVInput,
    *,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
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

    # Firewall 310..340 (pre-commit gate)
    verdict = run_firewall(atv, inp, atv_id=atv_id)

    # Step 350: if REQUIRE_APPROVAL, dispatch a notification to the approver
    # channel. The HTTP response still returns immediately with the verdict —
    # the host is responsible for calling /approve afterwards.
    step350_approval.dispatch(verdict, inp)

    # Step 360: canonical serialize + Ed25519-sign + append to audit log.
    record = step360_audit.sign_and_append(
        atv=atv, verdict=verdict, inp=inp, key=key, db=db, log=log
    )
    verdict.signature = record["signature"]

    # Step 370: annotate the verdict with the execution recommendation
    # (PROCEED / SUPPRESS / DEFER) so downstream log consumers can
    # distinguish the firewall boundary.
    step370_exec.annotate(verdict)

    return verdict


def make_router(
    *,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
    burn_in_id: str | None = None,
) -> APIRouter:
    r = APIRouter()

    @r.post("/evaluate", response_model=Verdict)
    def evaluate(inp: ATVInput) -> Verdict:
        return _evaluate_impl(inp, key=key, db=db, log=log, burn_in_id=burn_in_id)

    return r
