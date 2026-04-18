"""GET /attestation — return the signed Burn-in measurement."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aegis.attest.burn_in import BurnInMeasurement


def make_router(*, measurement: BurnInMeasurement) -> APIRouter:
    r = APIRouter()

    @r.get("/attestation")
    def attestation() -> dict[str, Any]:
        return measurement.to_dict()

    return r
