"""Mock TEE attestation quote collector.

T2 placeholder for Intel TDX / AMD SEV-SNP / ARM CCA quote retrieval.
Returns a stub quote bytestring + advertises that the caller is
running outside an enclave (so HW-attested counters can't be trusted
to be tamper-proof).

T3 swap-in: a real backend reads ``/dev/tdx-attest`` (or the platform
equivalent), executes the quote-generation ioctl, and returns the
signed measurement blob. The collector contract is identical.
"""

from __future__ import annotations

import hashlib
import os

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class MockTEEQuoteCollector(HWCollector):
    name = "tee_quote"

    def is_available(self) -> bool:
        # Mock is always "available" so it appears in the report;
        # production should swap with a real provider that detects
        # /dev/tdx-attest etc.
        return True

    def collect(self) -> CollectorResult:
        # Deterministic mock quote: SHA3 of the host-name + AID.
        # Real TEE returns ~1KB signed quote; the size matters less
        # than the audit trail (every collected ATV references one).
        seed = (os.uname().nodename + "|aegis-mock-tee").encode("utf-8")
        quote = hashlib.sha3_256(seed).hexdigest()
        return CollectorResult(
            available=True,
            values={
                # Two slots in HWCounters can carry attestation signal:
                # hypervisor_ring_violations (T2 stays 0; T3 reports
                # actual ring violations) and watchdog_strikes.
                "hypervisor_ring_violations": 0.0,
                "watchdog_strikes": 0.0,
            },
            metadata={
                "quote_sha3": quote,
                "tee_provider": "mock",
                "trust_level": "unverified",
            },
        )
