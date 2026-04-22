"""Agent-facing Hierarchical Agent Memory endpoints (patent ¶[0102C]).

  POST /ham/memory       — store an item (encrypted, AID-bound)
  POST /ham/recall       — retrieve by aid + tenant + tag filter
  POST /ham/context      — assemble a context bundle from N most-recent items
  POST /ham/forget       — tombstone an object
  POST /ham/summarize    — counts + tag histogram
  POST /ham/ground       — bind a claim to one or more memory objects
  GET  /ham/stats        — diagnostic counts
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aegis.ham import HierarchicalMemoryStore


class MemoryRequest(BaseModel):
    aid: str
    tenant_id: str
    body: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    policy_domain_id: str | None = None
    schema_version: str = "HAM-v1"


class RecallRequest(BaseModel):
    aid: str
    tenant_id: str
    tags: list[str] = Field(default_factory=list)
    limit: int = 10
    include_body: bool = True


class ContextRequest(BaseModel):
    aid: str
    tenant_id: str
    tags: list[str] = Field(default_factory=list)
    max_items: int = 5


class ForgetRequest(BaseModel):
    object_id: str
    aid: str
    tenant_id: str
    reason: str = "user_requested"


class SummarizeRequest(BaseModel):
    aid: str
    tenant_id: str
    tags: list[str] = Field(default_factory=list)
    max_items: int = 20


class GroundRequest(BaseModel):
    aid: str
    tenant_id: str
    claim: str
    reference_ids: list[str]


def make_router(*, store: HierarchicalMemoryStore | None = None) -> APIRouter:
    r = APIRouter()

    def _store() -> HierarchicalMemoryStore:
        if store is None:
            raise HTTPException(503, "HAM store not configured")
        return store

    @r.post("/ham/memory")
    def memory(req: MemoryRequest) -> dict[str, Any]:
        rec = _store().memory(
            aid=req.aid, tenant_id=req.tenant_id, body=req.body,
            tags=req.tags, policy_domain_id=req.policy_domain_id,
            schema_version=req.schema_version,
        )
        return {"ok": True, "object_id": rec["object_id"], "seq": rec["seq"],
                "digest": rec["digest"], "ts_ns": rec["ts_ns"]}

    @r.post("/ham/recall")
    def recall(req: RecallRequest) -> dict[str, Any]:
        items = _store().recall(
            aid=req.aid, tenant_id=req.tenant_id, tags=req.tags,
            limit=req.limit, include_body=req.include_body,
        )
        return {"length": len(items), "items": items}

    @r.post("/ham/context")
    def context(req: ContextRequest) -> dict[str, Any]:
        return _store().context(
            aid=req.aid, tenant_id=req.tenant_id, tags=req.tags,
            max_items=req.max_items,
        )

    @r.post("/ham/forget")
    def forget(req: ForgetRequest) -> dict[str, Any]:
        ok = _store().forget(
            object_id=req.object_id, aid=req.aid, tenant_id=req.tenant_id,
            reason=req.reason,
        )
        if not ok:
            raise HTTPException(404, f"object {req.object_id} not found for aid {req.aid}")
        return {"ok": True, "object_id": req.object_id}

    @r.post("/ham/summarize")
    def summarize(req: SummarizeRequest) -> dict[str, Any]:
        return _store().summarize(
            aid=req.aid, tenant_id=req.tenant_id, tags=req.tags,
            max_items=req.max_items,
        )

    @r.post("/ham/ground")
    def ground(req: GroundRequest) -> dict[str, Any]:
        return _store().ground(
            aid=req.aid, tenant_id=req.tenant_id,
            claim=req.claim, reference_ids=req.reference_ids,
        )

    @r.get("/ham/stats")
    def stats(tenant_id: str | None = None) -> dict[str, Any]:
        return _store().stats(tenant_id=tenant_id)

    return r
