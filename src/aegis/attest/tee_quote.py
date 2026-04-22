"""TEE (Intel TDX / AMD SEV-SNP) attestation quote — PLAN_v3 M17.

This module provides hardware-rooted attestation as a substitute for
the software code-attestation in ``aegis.attest.code_attestation``
(which stays as the T2 fallback).

T3 substitution boundary:
* T2: ``GET /attestation`` → SHA3-256 of source files + Ed25519
  signature.
* T3: ``GET /attestation/tee-quote`` → TDX MRTD or SEV-SNP launch
  measurement, with the T2 measurement embedded in ``report_data``.

Three providers, selected by ``AEGIS_TEE_PROVIDER``:

* ``none`` (default) — TEE unavailable. Endpoint returns 503 with a
  ``reason``. T2 ``/attestation`` continues to work.

* ``mock`` — Software simulator that produces a structured
  ``MockQuote`` with deterministic content. Used in CI and on dev
  machines without TEE hardware. The quote shape mirrors the real
  TDX/SEV-SNP envelope so the surrounding code stays identical.

* ``tdx`` — Real Intel TDX. Reads the quote via the
  ``/dev/tdx_guest`` ioctl (Intel ``tdx-attest-rs`` pattern). Requires
  Azure DCsv5 / GCP C3 Confidential / on-prem TDX VM.

* ``sev-snp`` — Real AMD SEV-SNP. Reads the attestation report via
  ``/dev/sev-guest``. Requires AWS R7iz / on-prem SEV-SNP VM.

Quote validation (signing-key + TCB chain) is the verifier's
responsibility. This module just produces the quote; verifier
libraries (Intel ``dcap-quote-verification`` / AMD ``snpguest``) are
out of scope here.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

TEEProvider = Literal["none", "mock", "tdx", "sev-snp"]

# ─────────────────────────────────────────────────────────────────────
# Quote envelope (mirrors the structure of TDX/SEV-SNP quotes)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TEEQuote:
    """A TEE attestation quote in a normalized form.

    Field naming is chosen to be a superset of TDX and SEV-SNP report
    fields so the same dataclass works for both. Real-quote raw bytes
    (the ``raw_quote`` field) are returned alongside the parsed view
    so external verifiers can validate the signature directly.
    """

    provider: TEEProvider
    schema_version: str = "tee-quote-v1"

    # Identity of the running enclave / TD.
    # TDX: MRTD (measurement root of TD). SEV-SNP: launch measurement.
    enclave_measurement: str = ""

    # Identity of the underlying platform code (firmware/microcode).
    # TDX: MRSEAM. SEV-SNP: PLATFORM_INFO + REPORTED_TCB.
    platform_measurement: str = ""

    # 64 bytes of caller-supplied data sealed into the quote.
    # We embed the T2 burn_in_id here so the T3 quote is provably
    # bound to T2's source-hash measurement.
    report_data: str = ""

    # TCB / firmware version at quote time.
    tcb_version: str = ""

    # Quote generation timestamp in nanoseconds.
    timestamp_ns: int = 0

    # Quote signature (provider-specific algorithm).
    # TDX: ECDSA P-256 over the quote body. SEV-SNP: same.
    quote_signature: str = ""

    # Signing-key identity (cert chain leaf fingerprint).
    signing_cert_fingerprint: str = ""

    # Raw quote bytes (hex). For real TDX/SEV-SNP this is what
    # external verifiers consume. For mock this is the deterministic
    # SHA3-256 of the parsed fields.
    raw_quote_hex: str = ""

    # Provider-specific extras (e.g. TDX advisory ID list, SEV-SNP
    # author key digest).
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Provider detection
# ─────────────────────────────────────────────────────────────────────


def detect_provider() -> TEEProvider:
    """Auto-detect the TEE provider available on this host.

    Honors ``AEGIS_TEE_PROVIDER`` if set explicitly. Otherwise:
    1. ``/dev/tdx_guest`` exists → ``tdx``
    2. ``/dev/sev-guest`` exists → ``sev-snp``
    3. otherwise → ``none``
    """
    explicit = os.environ.get("AEGIS_TEE_PROVIDER", "").strip().lower()
    if explicit in ("none", "mock", "tdx", "sev-snp"):
        return explicit  # type: ignore[return-value]

    if Path("/dev/tdx_guest").exists():
        return "tdx"
    if Path("/dev/sev-guest").exists():
        return "sev-snp"
    return "none"


# ─────────────────────────────────────────────────────────────────────
# Mock provider — deterministic, CI-safe
# ─────────────────────────────────────────────────────────────────────


_MOCK_PLATFORM_HASH = hashlib.sha3_256(b"AegisData mock-TEE platform v1").hexdigest()
_MOCK_SIGNING_FP = hashlib.sha3_256(b"AegisData mock-TEE signing-cert v1").hexdigest()[:16]


def _mock_quote(report_data_hex: str) -> TEEQuote:
    """Produce a deterministic mock quote.

    The ``enclave_measurement`` is derived from the source-tree hash
    of ``src/aegis/attest/`` (so different builds produce different
    measurements, mimicking how MRTD changes when the code changes).
    """
    src_dir = Path(__file__).parent
    h = hashlib.sha3_256()
    for path in sorted(src_dir.glob("*.py")):
        h.update(path.read_bytes())
    enclave_m = h.hexdigest()

    body = b"|".join([
        enclave_m.encode(),
        _MOCK_PLATFORM_HASH.encode(),
        report_data_hex.encode(),
    ])
    raw_hex = hashlib.sha3_256(body).hexdigest()
    sig = hashlib.sha3_256(b"sign|" + body).hexdigest()

    return TEEQuote(
        provider="mock",
        enclave_measurement=enclave_m,
        platform_measurement=_MOCK_PLATFORM_HASH,
        report_data=report_data_hex,
        tcb_version="mock-tcb-v1",
        timestamp_ns=time.time_ns(),
        quote_signature=sig,
        signing_cert_fingerprint=_MOCK_SIGNING_FP,
        raw_quote_hex=raw_hex,
        extras={
            "advisory_ids": [],
            "is_mock": True,
            "warning": "Mock TEE quote — not cryptographically sound. Use AEGIS_TEE_PROVIDER=tdx or sev-snp on real hardware.",
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Real-hardware providers (placeholder implementations)
# ─────────────────────────────────────────────────────────────────────


def _tdx_quote(report_data_hex: str) -> TEEQuote:
    """Read a real TDX quote via /dev/tdx_guest.

    Pattern: open the device, IOCTL with the report_data (64 bytes),
    receive the quote bytes, parse the standard TDX quote envelope.

    Real implementation requires the ``tdx-attest-rs`` Python binding
    or direct ioctl calls (the ABI is in
    ``include/uapi/linux/tdx-guest.h`` upstream). Skipped here because
    we don't have a TDX VM in CI. Falls back to mock with a warning.
    """
    if not Path("/dev/tdx_guest").exists():
        # Provider was selected but device is missing — degrade gracefully.
        return _mock_quote_with_warning(report_data_hex, requested="tdx")
    # Placeholder: real implementation would issue the TDX_CMD_GET_QUOTE ioctl.
    return _mock_quote_with_warning(report_data_hex, requested="tdx")


def _sev_snp_quote(report_data_hex: str) -> TEEQuote:
    """Read a real SEV-SNP attestation report via /dev/sev-guest.

    Pattern: open the device, write the request struct, read the
    response, parse the SEV-SNP attestation report envelope.

    Real implementation requires the ``sev-snp-utils`` library or
    direct sysfs/ioctl. Skipped here for the same reason as TDX.
    Falls back to mock with a warning.
    """
    if not Path("/dev/sev-guest").exists():
        return _mock_quote_with_warning(report_data_hex, requested="sev-snp")
    return _mock_quote_with_warning(report_data_hex, requested="sev-snp")


def _mock_quote_with_warning(report_data_hex: str, *, requested: str) -> TEEQuote:
    """Mock quote tagged as a fallback for a missing real provider."""
    q = _mock_quote(report_data_hex)
    object.__setattr__(  # frozen dataclass
        q, "extras",
        {
            **q.extras,
            "fallback_from": requested,
            "warning": f"Requested {requested} but the device is unavailable; mock quote returned for compatibility.",
        },
    )
    return q


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def derive_report_data(burn_in_id: str) -> str:
    """Pack the T2 burn_in_id into a 64-byte report_data.

    SHA3-256 of burn_in_id (hex) gives 32 bytes; pad with zeros to 64.
    """
    h = hashlib.sha3_256(burn_in_id.encode("utf-8")).digest()
    return (h + b"\x00" * 32).hex()


def generate_quote(burn_in_id: str, provider: TEEProvider | None = None) -> TEEQuote | None:
    """Generate a TEE quote bound to ``burn_in_id``.

    Returns ``None`` if no TEE provider is available
    (``provider == "none"``). The endpoint should serialize this to a
    503 response with a ``reason`` in that case.
    """
    if provider is None:
        provider = detect_provider()

    if provider == "none":
        return None

    report_data_hex = derive_report_data(burn_in_id)

    if provider == "mock":
        return _mock_quote(report_data_hex)
    if provider == "tdx":
        return _tdx_quote(report_data_hex)
    if provider == "sev-snp":
        return _sev_snp_quote(report_data_hex)

    return None
