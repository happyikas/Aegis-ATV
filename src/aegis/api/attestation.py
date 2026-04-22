"""GET /attestation — return the signed Burn-in measurement.

GET /attestation/tee-quote — PLAN_v3 M17: return a hardware-rooted
TEE quote (Intel TDX / AMD SEV-SNP) with the T2 burn_in_id embedded
in ``report_data``. Returns 503 when no TEE provider is available,
so the T2 callers keep working unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from aegis.attest.burn_in import BurnInMeasurement
from aegis.attest.tee_quote import detect_provider, generate_quote


def make_router(*, measurement: BurnInMeasurement) -> APIRouter:
    r = APIRouter()

    @r.get("/attestation")
    def attestation() -> dict[str, Any]:
        body = measurement.to_dict()
        # Surface a pointer to the T3 endpoint so clients know it
        # exists. Value is a URL path, not a full URL, because the
        # host is the same.
        body["tee_quote_ref"] = "/attestation/tee-quote"
        return body

    @r.get("/attestation/tee-quote")
    def tee_quote() -> dict[str, Any]:
        provider = detect_provider()
        if provider == "none":
            raise HTTPException(
                status_code=503,
                detail=(
                    "TEE attestation not available on this host. "
                    "Set AEGIS_TEE_PROVIDER=mock for a CI-safe "
                    "simulator, or run on a TDX / SEV-SNP host."
                ),
            )
        quote = generate_quote(measurement.burn_in_id, provider=provider)
        if quote is None:
            raise HTTPException(
                status_code=503,
                detail=f"TEE provider '{provider}' returned no quote.",
            )
        return {
            "burn_in_id": measurement.burn_in_id,
            "provider": quote.provider,
            "quote": quote.to_dict(),
        }

    return r
