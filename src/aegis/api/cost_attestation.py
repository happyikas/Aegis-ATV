"""GET /cost-attestation/{aid} + helpers — patent §12 + Claim 29 (selectively
disclosable Cost Attestation Ledger).

The cost ledger is signed with a key DISTINCT from the audit signing
key (Claim 34), so a customer or regulator can be granted read-only
access here without seeing the broader audit log.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aegis.cost.ledger import CostAttestationLedger


def make_router(*, ledger: CostAttestationLedger) -> APIRouter:
    r = APIRouter()

    @r.get("/cost-attestation/{aid}")
    def by_aid(aid: str) -> dict[str, Any]:
        records = ledger.list_by_aid(aid)
        valid, err = ledger.verify_chain(aid)
        return {
            "aid": aid,
            "head": ledger.head(aid),
            "length": len(records),
            "chain_valid": valid,
            "chain_error": err,
            "records": records,
        }

    @r.get("/cost-attestation/by-tenant/{tenant_id}")
    def by_tenant(tenant_id: str) -> dict[str, Any]:
        records = ledger.list_by_tenant(tenant_id)
        return {
            "tenant_id": tenant_id,
            "length": len(records),
            "records": records,
        }

    return r
