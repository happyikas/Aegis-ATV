"""Unit tests for the M17 TEE attestation module."""

from __future__ import annotations

import os
from unittest import mock

from aegis.attest.tee_quote import (
    TEEQuote,
    derive_report_data,
    detect_provider,
    generate_quote,
)


# ─────────────────────────────────────────────────────────────────────
# detect_provider
# ─────────────────────────────────────────────────────────────────────
class TestDetectProvider:
    def test_explicit_env_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"AEGIS_TEE_PROVIDER": "mock"}):
            assert detect_provider() == "mock"

    def test_explicit_none(self) -> None:
        with mock.patch.dict(os.environ, {"AEGIS_TEE_PROVIDER": "none"}):
            assert detect_provider() == "none"

    def test_explicit_invalid_falls_to_autodetect(self) -> None:
        with mock.patch.dict(os.environ, {"AEGIS_TEE_PROVIDER": "totally-bogus"}):
            # Auto-detect path is exercised; on CI hardware this
            # almost always lands on "none".
            result = detect_provider()
            assert result in ("none", "tdx", "sev-snp", "mock")

    def test_default_when_no_env(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "AEGIS_TEE_PROVIDER"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = detect_provider()
            # CI box has no TEE devices; this should be "none".
            assert result in ("none", "tdx", "sev-snp")


# ─────────────────────────────────────────────────────────────────────
# derive_report_data
# ─────────────────────────────────────────────────────────────────────
class TestDeriveReportData:
    def test_64_bytes(self) -> None:
        rd = derive_report_data("burn-in-id-xyz")
        assert len(bytes.fromhex(rd)) == 64  # TDX/SEV-SNP report_data is 64 B

    def test_deterministic(self) -> None:
        a = derive_report_data("same-input")
        b = derive_report_data("same-input")
        assert a == b

    def test_changes_with_input(self) -> None:
        a = derive_report_data("input-A")
        b = derive_report_data("input-B")
        assert a != b


# ─────────────────────────────────────────────────────────────────────
# generate_quote — none provider
# ─────────────────────────────────────────────────────────────────────
class TestGenerateQuoteNone:
    def test_returns_none_when_provider_is_none(self) -> None:
        assert generate_quote("burn-id", provider="none") is None


# ─────────────────────────────────────────────────────────────────────
# generate_quote — mock provider
# ─────────────────────────────────────────────────────────────────────
class TestGenerateQuoteMock:
    def test_returns_quote_object(self) -> None:
        q = generate_quote("burn-id", provider="mock")
        assert isinstance(q, TEEQuote)
        assert q.provider == "mock"

    def test_quote_has_all_fields_populated(self) -> None:
        q = generate_quote("burn-id", provider="mock")
        assert q is not None
        assert q.enclave_measurement
        assert q.platform_measurement
        assert q.report_data
        assert q.tcb_version
        assert q.timestamp_ns > 0
        assert q.quote_signature
        assert q.signing_cert_fingerprint
        assert q.raw_quote_hex

    def test_quote_is_deterministic_per_burn_id(self) -> None:
        """Mock quote depends on burn_in_id (via report_data) AND on
        the source-tree hash (enclave_measurement). Same inputs → same
        enclave_measurement and report_data; signature/raw differ only
        by timestamp."""
        q1 = generate_quote("same-id", provider="mock")
        q2 = generate_quote("same-id", provider="mock")
        assert q1 is not None and q2 is not None
        assert q1.enclave_measurement == q2.enclave_measurement
        assert q1.report_data == q2.report_data
        assert q1.platform_measurement == q2.platform_measurement

    def test_different_burn_id_changes_report_data(self) -> None:
        q1 = generate_quote("id-A", provider="mock")
        q2 = generate_quote("id-B", provider="mock")
        assert q1 is not None and q2 is not None
        assert q1.report_data != q2.report_data

    def test_extras_carry_mock_warning(self) -> None:
        q = generate_quote("burn-id", provider="mock")
        assert q is not None
        assert q.extras.get("is_mock") is True
        assert "warning" in q.extras

    def test_to_dict_serializes_cleanly(self) -> None:
        q = generate_quote("burn-id", provider="mock")
        assert q is not None
        d = q.to_dict()
        for key in (
            "provider", "schema_version", "enclave_measurement",
            "platform_measurement", "report_data", "tcb_version",
            "timestamp_ns", "quote_signature", "signing_cert_fingerprint",
            "raw_quote_hex", "extras",
        ):
            assert key in d


# ─────────────────────────────────────────────────────────────────────
# generate_quote — tdx / sev-snp providers (degrade to mock when
# device missing)
# ─────────────────────────────────────────────────────────────────────
class TestGenerateQuoteHardwareProviders:
    def test_tdx_falls_back_to_mock_when_device_missing(self) -> None:
        # CI doesn't have /dev/tdx_guest. We expect a mock-with-warning quote.
        q = generate_quote("burn-id", provider="tdx")
        assert q is not None
        assert q.provider == "mock"
        # Marked as a fallback in extras.
        assert q.extras.get("fallback_from") == "tdx"

    def test_sev_snp_falls_back_to_mock_when_device_missing(self) -> None:
        q = generate_quote("burn-id", provider="sev-snp")
        assert q is not None
        assert q.provider == "mock"
        assert q.extras.get("fallback_from") == "sev-snp"


# ─────────────────────────────────────────────────────────────────────
# Integration: report_data binds to burn_in_id
# ─────────────────────────────────────────────────────────────────────
class TestReportDataBinding:
    def test_quote_report_data_decodes_to_burn_id_hash(self) -> None:
        """The report_data MUST be a deterministic function of the
        T2 burn_in_id. External verifiers will recompute and compare."""
        burn_id = "test-burn-id-abc123"
        expected = derive_report_data(burn_id)
        q = generate_quote(burn_id, provider="mock")
        assert q is not None
        assert q.report_data == expected
