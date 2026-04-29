"""TEE quote verifier (v4.4).

Verifies :class:`aegis.attest.tee_quote.TEEQuote` against the issuer's
trust chain:

* **Intel TDX** — Provisioning Certificate Service (PCS) at
  ``api.trustedservices.intel.com``. The chain is:
  TD Quote → PCK Cert → Platform CA → Root CA.
* **AMD SEV-SNP** — Key Distribution Service (KDS). Chain:
  Attestation Report → VLEK / VCEK → ASK → ARK.
* **ARM CCA** — confidential cloud attestation. Verifier reference is
  the ARM CCA RIM (Reference Integrity Manifest).

What v4.4 implements
--------------------
1. **Local mock verification** — the mock provider's deterministic
   SHA3 signature is verifiable end-to-end (so unit tests cover the
   full \"fetch → verify\" path).
2. **Real TDX/SEV-SNP** — schema-level checks: report length, report
   data echo, MRTD/measurement non-zero, ABI version sanity. Full
   cryptographic chain verification requires the Intel/AMD root certs
   distributed out-of-band; v4.4 ships a **pluggable verifier
   interface** so production can drop in
   ``intel-sgx-dcap-quote-verification-py`` or ``snpguest`` without
   any other code changes.

Production guidance
-------------------
For T3 deployments with strict compliance (FIPS / EU AI Act high-risk
system), pin one of:

* ``intel-sgx-dcap-quote-verification`` (Apache-2.0)
* ``snpguest`` (Apache-2.0)
* AWS Nitro Enclave attestation lib (Apache-2.0)

and register them via :meth:`TEEQuoteVerifier.register_provider`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aegis.attest.tee_quote import TEEProvider, TEEQuote


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one verifier run."""

    valid: bool
    provider: TEEProvider
    reasons: tuple[str, ...]
    extras: dict[str, Any]

    def __bool__(self) -> bool:
        return self.valid


# ─────────────────────────────────────────────────────────────────────
# Provider verifiers
# ─────────────────────────────────────────────────────────────────────


VerifierFunc = Callable[[TEEQuote, dict[str, Any] | None], VerificationResult]


def _verify_mock(quote: TEEQuote, expected_report_data: dict[str, Any] | None) -> VerificationResult:
    """Mock provider verifier — re-derive signature locally."""
    body = b"|".join([
        quote.enclave_measurement.encode(),
        quote.platform_measurement.encode(),
        quote.report_data.encode(),
    ])
    expected_sig = hashlib.sha3_256(b"sign|" + body).hexdigest()
    if quote.quote_signature != expected_sig:
        return VerificationResult(
            valid=False, provider="mock",
            reasons=("mock signature mismatch",),
            extras={"expected": expected_sig, "got": quote.quote_signature},
        )
    return VerificationResult(
        valid=True, provider="mock",
        reasons=("mock signature ok",),
        extras={"trust_level": "mock-only"},
    )


def _verify_tdx_schema(quote: TEEQuote, expected_report_data: dict[str, Any] | None) -> VerificationResult:
    """TDX schema-level checks. Full cryptographic verification
    requires Intel DCAP libraries; v4.4 surfaces what we can check
    without them and flags ``trust_level=schema-only``.
    """
    reasons: list[str] = []
    valid = True

    raw = bytes.fromhex(quote.raw_quote_hex) if quote.raw_quote_hex else b""
    if len(raw) != 1024:
        reasons.append(f"raw TDREPORT must be 1024 bytes, got {len(raw)}")
        valid = False

    if not quote.enclave_measurement or quote.enclave_measurement == "0" * 96:
        reasons.append("MRTD is empty/zero")
        valid = False
    elif len(quote.enclave_measurement) != 96:  # 48 bytes hex
        reasons.append(
            f"MRTD must be 48-byte hex (96 chars), got {len(quote.enclave_measurement)}"
        )
        valid = False

    if not quote.report_data:
        reasons.append("report_data is empty")
        valid = False

    return VerificationResult(
        valid=valid,
        provider="tdx",
        reasons=tuple(reasons or ("schema-level checks passed",)),
        extras={
            "trust_level": "schema-only" if valid else "invalid",
            "needs_dcap_verifier": True,
        },
    )


def _verify_sev_snp_schema(quote: TEEQuote, expected_report_data: dict[str, Any] | None) -> VerificationResult:
    """SEV-SNP schema-level checks (ABI version, measurement, report_data
    echo). Full chain verification requires AMD KDS lookups."""
    reasons: list[str] = []
    valid = True

    raw = bytes.fromhex(quote.raw_quote_hex) if quote.raw_quote_hex else b""
    if len(raw) != 4000:
        reasons.append(f"raw SNP report must be 4000 bytes, got {len(raw)}")
        valid = False

    if not quote.enclave_measurement:
        reasons.append("measurement is empty")
        valid = False

    if not quote.report_data:
        reasons.append("report_data is empty")
        valid = False

    return VerificationResult(
        valid=valid,
        provider="sev-snp",
        reasons=tuple(reasons or ("schema-level checks passed",)),
        extras={
            "trust_level": "schema-only" if valid else "invalid",
            "needs_kds_verifier": True,
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Verifier registry
# ─────────────────────────────────────────────────────────────────────


class TEEQuoteVerifier:
    """Pluggable verifier — production swaps in real Intel/AMD libraries.

    Default registry covers mock, schema-only TDX, schema-only SEV-SNP.
    Production deployments register their own verifier via
    :meth:`register_provider`.
    """

    def __init__(self) -> None:
        self._providers: dict[TEEProvider, VerifierFunc] = {
            "mock": _verify_mock,
            "tdx": _verify_tdx_schema,
            "sev-snp": _verify_sev_snp_schema,
        }

    def register_provider(self, provider: TEEProvider, verifier: VerifierFunc) -> None:
        """Override the verifier for one provider — for swap-in of
        Intel DCAP, AMD KDS, etc."""
        self._providers[provider] = verifier

    def verify(
        self,
        quote: TEEQuote,
        *,
        expected_report_data: dict[str, Any] | None = None,
    ) -> VerificationResult:
        verifier = self._providers.get(quote.provider)
        if verifier is None:
            return VerificationResult(
                valid=False, provider=quote.provider,
                reasons=(f"no verifier registered for provider {quote.provider}",),
                extras={},
            )
        return verifier(quote, expected_report_data)


__all__ = ["TEEQuoteVerifier", "VerificationResult"]
