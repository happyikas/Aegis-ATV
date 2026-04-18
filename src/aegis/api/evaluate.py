"""POST /evaluate — main entrypoint that runs the firewall, signs, and audits."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from aegis.atv.builder import build_atv
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.firewall.core import run_firewall
from aegis.schema import ATVInput, Verdict
from aegis.sign.ed25519 import sign_atv
from aegis.sign.merkle import record_hash


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
        inp = inp.model_copy(update={"header": inp.header.model_copy(update={"burn_in_id": burn_in_id})})

    atv = build_atv(inp)
    atv_id = str(uuid.uuid4())
    verdict = run_firewall(atv, inp, atv_id=atv_id)

    prev = db.get_head(inp.header.aid)
    header_dict = inp.header.model_dump() | {
        "decision": verdict.decision,
        "tool_name": inp.tool_name,
    }
    record = sign_atv(atv.tobytes(), header_dict, prev, key)
    record["atv_id"] = atv_id
    record["decision"] = verdict.decision
    record["this_hash"] = record_hash(record["payload"])

    log.append(record)
    db.append(record)
    verdict.signature = record["signature"]
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
