"""GET /burnin-status + POST /burnin/graduate + POST /burnin/label.

Surfaces the layered Burn-in state. T2 MVP exposes:
  - per-layer phase
  - sample counts
  - TPR/FPR/precision/override-rate (zero until /burnin/label feeds in)
  - composite anomaly score for an arbitrary input
  - manual graduation hook (for ops/testing)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aegis.burnin import BurnInController
from aegis.schema import ATVInput, Verdict


class GraduateRequest(BaseModel):
    layer_key: str  # e.g. "L4:demo-tenant:default-role"


class LabelRequest(BaseModel):
    inp: ATVInput
    verdict: Verdict
    ground_truth: str  # "benign" | "malicious"
    was_human_override: bool = False


def make_router(*, controller: BurnInController) -> APIRouter:
    r = APIRouter()

    @r.get("/burnin-status")
    def burnin_status() -> dict[str, Any]:
        return controller.status()

    @r.post("/burnin/graduate")
    def graduate(req: GraduateRequest) -> dict[str, Any]:
        ok, reason = controller.try_graduate(req.layer_key)
        if not ok:
            raise HTTPException(409, f"graduation blocked: {reason}")
        return {"ok": True, "layer_key": req.layer_key, "reason": reason}

    @r.post("/burnin/label")
    def label(req: LabelRequest) -> dict[str, Any]:
        if req.ground_truth not in ("benign", "malicious"):
            raise HTTPException(400, "ground_truth must be 'benign' or 'malicious'")
        controller.record_label(
            req.inp, req.verdict,
            ground_truth=req.ground_truth,
            was_human_override=req.was_human_override,
        )
        return {"ok": True}

    return r
