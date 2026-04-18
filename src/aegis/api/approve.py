"""POST /approve — record a human approval verdict for a previously
REQUIRE_APPROVAL atv_id. MVP just appends an approval record into the chain.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.sign.ed25519 import sign_atv
from aegis.sign.merkle import record_hash


class ApprovalRequest(BaseModel):
    atv_id: str
    aid: str
    tenant_id: str
    approver: str
    decision: str  # "ALLOW" or "BLOCK"
    note: str | None = None


def make_router(*, key: Any, db: AuditDB, log: JsonlStore) -> APIRouter:
    r = APIRouter()

    @r.post("/approve")
    def approve(req: ApprovalRequest) -> dict[str, Any]:
        if req.decision not in ("ALLOW", "BLOCK"):
            raise HTTPException(400, "decision must be ALLOW or BLOCK")
        prev = db.get_head(req.aid)
        header = {
            "aid": req.aid,
            "tenant_id": req.tenant_id,
            "tool_name": "<approval>",
            "approver": req.approver,
            "subject_atv_id": req.atv_id,
            "decision": req.decision,
            "note": req.note,
        }
        record = sign_atv(b"", header, prev, key)
        record["atv_id"] = f"approval-{req.atv_id}"
        record["decision"] = req.decision
        record["this_hash"] = record_hash(record["payload"])
        log.append(record)
        db.append(record)
        return {
            "ok": True,
            "atv_id": record["atv_id"],
            "head": db.get_head(req.aid),
        }

    return r
