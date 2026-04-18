"""GET /audit/{aid} — return the signed audit chain for an agent."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aegis.audit.sqlite_store import AuditDB
from aegis.sign.merkle import verify_chain


def make_router(*, db: AuditDB) -> APIRouter:
    r = APIRouter()

    @r.get("/audit/{aid}")
    def audit_chain(aid: str) -> dict[str, Any]:
        chain = db.get_chain(aid)
        ok, err = verify_chain(chain)
        return {
            "aid": aid,
            "head": db.get_head(aid),
            "length": len(chain),
            "chain_valid": ok,
            "chain_error": err,
            "chain": chain,
        }

    return r
