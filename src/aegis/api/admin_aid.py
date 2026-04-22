"""Admin endpoints for the AID circuit breaker (patent ¶[0063M] M14).

  GET  /admin/aid                — list quarantined AIDs
  GET  /admin/aid/{aid}          — detailed state + history for one AID
  POST /admin/aid/release        — manually release an AID
                                   (requires X-Aegis-Admin-Token header)

The signed-administrative-recovery-policy gate is approximated in T2 by
a static admin token shared via env var AEGIS_ADMIN_TOKEN. T3 verifies
a signed recovery quote from the hardware TEE.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from aegis.firewall.circuit_breaker import CircuitBreaker


class ReleaseRequest(BaseModel):
    aid: str
    reason: str


def make_router(*, breaker: CircuitBreaker) -> APIRouter:
    r = APIRouter()
    expected_token = os.environ.get("AEGIS_ADMIN_TOKEN", "dev-admin-token")

    def _auth(x_aegis_admin_token: str | None) -> None:
        if not x_aegis_admin_token or x_aegis_admin_token != expected_token:
            raise HTTPException(401, "missing or invalid X-Aegis-Admin-Token")

    @r.get("/admin/aid")
    def list_quarantined() -> dict[str, Any]:
        return {
            "quarantined": [
                {
                    "aid": st.aid, "violations": st.violations,
                    "quarantined_at_ns": st.quarantined_at_ns,
                    "reason": st.quarantine_reason,
                }
                for st in breaker.list_quarantined()
            ],
        }

    @r.get("/admin/aid/{aid}")
    def get_one(aid: str) -> dict[str, Any]:
        st = breaker.get(aid)
        if st is None:
            return {"aid": aid, "status": "normal", "violations": 0, "history": []}
        return {
            "aid": st.aid, "status": st.status,
            "violations": st.violations,
            "last_violation_ns": st.last_violation_ns,
            "quarantined_at_ns": st.quarantined_at_ns,
            "quarantine_reason": st.quarantine_reason,
            "history": st.history,
        }

    @r.post("/admin/aid/release")
    def release(
        req: ReleaseRequest,
        x_aegis_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(x_aegis_admin_token)
        st = breaker.release(req.aid, reason=req.reason)
        if st is None:
            raise HTTPException(404, f"unknown or non-quarantined aid: {req.aid}")
        return {"ok": True, "aid": st.aid, "status": st.status,
                "violations": st.violations}

    return r
