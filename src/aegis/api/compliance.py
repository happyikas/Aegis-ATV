"""POST /compliance/evidence — generate compliance evidence packet.

Operator endpoint that runs the v4.3 EvidenceCollector against a chosen
framework over a time period and returns the evidence packet as JSON
(machine-readable) or Markdown (human-readable for auditors).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from aegis.atmu.intent_log import IntentLog
from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.sqlite_store import AuditDB
from aegis.compliance import (
    AVAILABLE_FRAMEWORKS,
    EvidenceCollector,
    get_framework,
)
from aegis.cost.ledger import CostAttestationLedger


class EvidenceRequest(BaseModel):
    framework: Literal["soc2", "eu_ai_act", "hipaa", "iso_42001"]
    period_start_ns: int
    period_end_ns: int
    format: Literal["json", "markdown"] = "json"
    patrol_reports: list[dict[str, Any]] = []


def make_router(
    *,
    audit_db: AuditDB | None,
    intent_log: IntentLog | None,
    cost_ledger: CostAttestationLedger | None,
    encrypted_journal: EncryptedJournal | None,
) -> APIRouter:
    r = APIRouter()

    @r.get("/compliance/frameworks")
    def list_frameworks() -> dict[str, Any]:
        """Inventory of supported compliance frameworks."""
        return {
            "frameworks": [
                {
                    "id": k,
                    "name": v.name,
                    "version": v.version,
                    "description": v.description,
                    "control_count": len(v.controls),
                }
                for k, v in AVAILABLE_FRAMEWORKS.items()
            ],
        }

    @r.post("/compliance/evidence")
    def generate_evidence(req: EvidenceRequest) -> Response:
        """Run the collector against the chosen framework and return the
        evidence packet."""
        try:
            framework = get_framework(req.framework)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e

        collector = EvidenceCollector(
            audit_db=audit_db,
            intent_log=intent_log,
            cost_ledger=cost_ledger,
            encrypted_journal=encrypted_journal,
            patrol_reports=req.patrol_reports,
        )
        report = collector.collect(
            framework,
            period_start_ns=req.period_start_ns,
            period_end_ns=req.period_end_ns,
        )

        if req.format == "markdown":
            return PlainTextResponse(content=report.to_markdown(), media_type="text/markdown")
        return PlainTextResponse(content=report.to_json(), media_type="application/json")

    return r
