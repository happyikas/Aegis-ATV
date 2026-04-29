"""GET /attestation — return the signed Burn-in measurement.

GET /attestation/tee-quote — PLAN_v3 M17: return a hardware-rooted
TEE quote (Intel TDX / AMD SEV-SNP) with the T2 burn_in_id embedded
in ``report_data``. Returns 503 when no TEE provider is available,
so the T2 callers keep working unchanged.

GET /attestation/tee — v4.4 alias of tee-quote that *also* runs the
quote through :class:`TEEQuoteVerifier` and returns the verification
result alongside the quote. Production deployments use this endpoint
to surface ``trust_level`` to upstream verifiers.

POST /attestation/tee/verify — v4.4: verify a quote previously
fetched (or fetched from a peer host) without re-issuing the ioctl.
Useful for cross-org verification.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aegis.attest.burn_in import BurnInMeasurement
from aegis.attest.tee_quote import TEEQuote, detect_provider, generate_quote
from aegis.attest.tee_verifier import TEEQuoteVerifier


class VerifyQuoteRequest(BaseModel):
    """Body for ``POST /attestation/tee/verify``."""

    quote: dict[str, Any]


def make_router(*, measurement: BurnInMeasurement) -> APIRouter:
    r = APIRouter()
    verifier = TEEQuoteVerifier()

    @r.get("/attestation")
    def attestation() -> dict[str, Any]:
        body = measurement.to_dict()
        body["tee_quote_ref"] = "/attestation/tee-quote"
        body["tee_endpoint_ref"] = "/attestation/tee"
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

    @r.get("/attestation/tee")
    def tee_with_verification() -> dict[str, Any]:
        """v4.4 — fetch live quote AND run it through the verifier."""
        provider = detect_provider()
        if provider == "none":
            raise HTTPException(
                status_code=503,
                detail="TEE attestation not available on this host.",
            )
        quote = generate_quote(measurement.burn_in_id, provider=provider)
        if quote is None:
            raise HTTPException(
                status_code=503,
                detail=f"TEE provider '{provider}' returned no quote.",
            )
        result = verifier.verify(quote)
        return {
            "burn_in_id": measurement.burn_in_id,
            "provider": quote.provider,
            "quote": quote.to_dict(),
            "verification": {
                "valid": result.valid,
                "reasons": list(result.reasons),
                "extras": result.extras,
            },
        }

    @r.post("/attestation/tee/verify")
    def verify_remote_quote(req: VerifyQuoteRequest) -> dict[str, Any]:
        """v4.4 — verify a quote received from a peer host."""
        try:
            quote = TEEQuote(**req.quote)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"malformed quote: {e}") from e
        result = verifier.verify(quote)
        return {
            "valid": result.valid,
            "provider": result.provider,
            "reasons": list(result.reasons),
            "extras": result.extras,
        }

    return r
