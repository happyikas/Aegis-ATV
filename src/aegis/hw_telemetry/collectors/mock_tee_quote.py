"""TEE attestation quote collector (v4.4 — auto-detecting).

Auto-detects the running TEE provider (Intel TDX / AMD SEV-SNP) via
``/dev/tdx_guest`` / ``/dev/sev-guest`` and produces a real
attestation report when available. Falls back to the v4.1 deterministic
mock quote when no TEE is present.

Class name retained as ``MockTEEQuoteCollector`` for v4.1 backward
compatibility (the aggregator imports this exact name). Behaviour
upgrades automatically when a real device appears at runtime.

Trust-level mapping
-------------------
* ``/dev/tdx_guest`` present → ``trust_level=tdx-attested``
* ``/dev/sev-guest`` present → ``trust_level=sev-snp-attested``
* Neither → ``trust_level=mock``
"""

from __future__ import annotations

import hashlib
import os

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class MockTEEQuoteCollector(HWCollector):
    """v4.4 — auto-detecting TEE quote collector. Class name retained
    for backward compatibility with v4.1 aggregator imports."""

    name = "tee_quote"

    def is_available(self) -> bool:
        # Always-on; real-vs-mock distinction surfaces in the metadata.
        return True

    def collect(self) -> CollectorResult:
        from aegis.attest.tee_quote import (
            _mock_quote,
            detect_provider,
            generate_quote,
        )

        provider = detect_provider()
        seed_burn_in = "host-" + hashlib.sha3_256(
            os.uname().nodename.encode()
        ).hexdigest()[:16]
        quote = generate_quote(seed_burn_in, provider=provider)
        if quote is None:
            quote = _mock_quote(seed_burn_in)

        provider_to_trust = {
            "tdx": "tdx-attested",
            "sev-snp": "sev-snp-attested",
            "mock": "mock",
            "none": "mock",
        }

        return CollectorResult(
            available=True,
            values={
                "hypervisor_ring_violations": 0.0,
                "watchdog_strikes": 0.0,
            },
            metadata={
                "tee_provider": quote.provider,
                "trust_level": provider_to_trust.get(quote.provider, "unknown"),
                "enclave_measurement": quote.enclave_measurement,
                "report_data": quote.report_data,
                "raw_quote_size_bytes": len(quote.raw_quote_hex) // 2,
            },
        )
