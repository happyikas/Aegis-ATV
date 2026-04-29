"""GET /audit/patrol/status, POST /audit/patrol/run (v4.0).

Operator-facing surface for the background AuditPatrol daemon.
Read-only ``status`` returns the most recent reports; ``run`` triggers
a one-shot patrol of any scope (useful for ops dashboards and
incident response).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aegis.audit.patrol import AuditPatrol


class PatrolRunRequest(BaseModel):
    scope: Literal["full", "sample", "sequence", "consistency", "cold"]
    fraction: float | None = None  # for "sample" only


def make_router(*, patrol: AuditPatrol | None) -> APIRouter:
    r = APIRouter()

    @r.get("/audit/patrol/status")
    def patrol_status() -> dict[str, Any]:
        if patrol is None:
            return {
                "enabled": False,
                "reason": "AEGIS_AUDIT_PATROL_ENABLED=false",
            }
        return {
            "enabled": True,
            **patrol.latest_status(),
            "history": patrol.recent_reports(limit=20),
        }

    @r.post("/audit/patrol/run")
    def patrol_run(req: PatrolRunRequest) -> dict[str, Any]:
        if patrol is None:
            raise HTTPException(503, "AuditPatrol not enabled")
        if req.scope == "full":
            report = patrol.patrol_full()
        elif req.scope == "sample":
            report = patrol.patrol_sample(fraction=req.fraction)
        elif req.scope == "sequence":
            report = patrol.patrol_sequence()
        elif req.scope == "consistency":
            report = patrol.patrol_consistency()
        elif req.scope == "cold":
            report = patrol.patrol_cold()
        else:
            raise HTTPException(400, f"unknown scope: {req.scope}")
        return report.to_dict()

    return r
