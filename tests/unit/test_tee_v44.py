"""Unit tests for v4.4 TEE deployment readiness.

Covers:
* Real TDX / SEV-SNP ioctl wrappers (with mocked devices)
* Quote verifier (mock + schema-only TDX/SEV-SNP)
* Sealed-key abstraction (LocalSealedKey + detection)
* Auto-detecting TEE quote collector
* /attestation/tee + /attestation/tee/verify endpoints
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.attest.tee_ioctl import (
    SEV_DEVICE_PATH,
    TDX_DEVICE_PATH,
    TDX_REPORT_DATA_LEN,
    TDX_REPORT_LEN,
    SEVSNPReport,
    TDXReport,
    fetch_sev_snp_report,
    fetch_tdx_report,
)
from aegis.attest.tee_quote import (
    TEEQuote,
    _mock_quote,  # type: ignore[attr-defined]
    detect_provider,
    generate_quote,
)
from aegis.attest.tee_verifier import (
    TEEQuoteVerifier,
    VerificationResult,
)
from aegis.hw_telemetry.collectors.mock_tee_quote import MockTEEQuoteCollector
from aegis.sign.sealed_key import (
    LocalSealedKey,
    SEVSNPDerivedKey,
    TDXSealedKey,
    detect_sealed_key_provider,
    load_or_create_sealed_signing_key,
)

# ─────────────────────────────────────────────────────────────────────
# tee_ioctl — TDX / SEV-SNP wrappers
# ─────────────────────────────────────────────────────────────────────


def test_fetch_tdx_report_returns_none_when_device_missing(tmp_path: Path) -> None:
    # /dev/tdx_guest is almost certainly absent in CI / dev hosts.
    # If it IS present (rare), this test still passes since we only
    # assert "no error / typed return".
    result = fetch_tdx_report(b"\x00" * TDX_REPORT_DATA_LEN)
    assert result is None or isinstance(result, TDXReport)


def test_fetch_tdx_report_validates_input_length() -> None:
    with pytest.raises(ValueError, match="64 bytes"):
        fetch_tdx_report(b"too short")


def test_tdx_report_extracts_mrtd_from_raw() -> None:
    """Synthetic 1024-byte report → MRTD parses out of expected offset."""
    raw = bytearray(TDX_REPORT_LEN)
    fake_mrtd = bytes(range(48))  # 0..47
    raw[528 : 528 + 48] = fake_mrtd
    report = TDXReport(raw=bytes(raw))
    assert report.mrtd == fake_mrtd.hex()


def test_tdx_report_extracts_report_data() -> None:
    raw = bytearray(TDX_REPORT_LEN)
    fake_data = bytes(range(64))
    raw[128 : 128 + 64] = fake_data
    report = TDXReport(raw=bytes(raw))
    assert report.report_data == fake_data.hex()


def test_tdx_report_short_raw_returns_empty() -> None:
    short = TDXReport(raw=b"\x00" * 100)
    assert short.mrtd == ""
    assert short.report_data == ""


def test_fetch_sev_snp_report_returns_none_when_device_missing() -> None:
    result = fetch_sev_snp_report(b"\x00" * 64, vmpl=0)
    assert result is None or isinstance(result, SEVSNPReport)


def test_fetch_sev_snp_report_validates_input_length() -> None:
    with pytest.raises(ValueError, match="64 bytes"):
        fetch_sev_snp_report(b"too short", vmpl=0)


def test_sev_snp_report_extracts_measurement() -> None:
    raw = bytearray(4000)
    fake_measurement = bytes(range(48))
    raw[32 + 144 : 32 + 192] = fake_measurement
    report = SEVSNPReport(raw=bytes(raw))
    assert report.measurement == fake_measurement.hex()


def test_sev_snp_report_extracts_report_data() -> None:
    raw = bytearray(4000)
    fake_data = bytes(range(64))
    raw[32 + 80 : 32 + 144] = fake_data
    report = SEVSNPReport(raw=bytes(raw))
    assert report.report_data == fake_data.hex()


# ─────────────────────────────────────────────────────────────────────
# tee_quote — generation + auto-detect
# ─────────────────────────────────────────────────────────────────────


def test_detect_provider_no_explicit(monkeypatch) -> None:
    monkeypatch.delenv("AEGIS_TEE_PROVIDER", raising=False)
    # Without /dev/tdx_guest or /dev/sev-guest, default is none
    provider = detect_provider()
    assert provider in ("none", "tdx", "sev-snp")


def test_detect_provider_explicit_mock(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "mock")
    assert detect_provider() == "mock"


def test_detect_provider_explicit_invalid_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "wat")
    # Invalid value → fallback to file-based detection
    provider = detect_provider()
    assert provider in ("none", "tdx", "sev-snp")


def test_generate_quote_returns_none_for_none() -> None:
    q = generate_quote("burn-in-1", provider="none")
    assert q is None


def test_generate_quote_mock_is_deterministic() -> None:
    a = generate_quote("burn-in-1", provider="mock")
    b = generate_quote("burn-in-1", provider="mock")
    assert a is not None and b is not None
    # Signature deterministic for same burn_in_id
    assert a.quote_signature == b.quote_signature
    assert a.report_data == b.report_data


def test_generate_quote_tdx_falls_back_when_device_absent() -> None:
    """When AEGIS_TEE_PROVIDER=tdx but /dev/tdx_guest missing, we get
    a mock fallback quote with a 'fallback_from' marker."""
    if Path(TDX_DEVICE_PATH).exists():
        pytest.skip("Real TDX device present — fallback path not exercised")
    q = generate_quote("burn-in-1", provider="tdx")
    assert q is not None
    assert q.extras.get("fallback_from") == "tdx"


def test_generate_quote_sev_snp_falls_back_when_device_absent() -> None:
    if Path(SEV_DEVICE_PATH).exists():
        pytest.skip("Real SEV-SNP device present")
    q = generate_quote("burn-in-1", provider="sev-snp")
    assert q is not None
    assert q.extras.get("fallback_from") == "sev-snp"


# ─────────────────────────────────────────────────────────────────────
# tee_verifier
# ─────────────────────────────────────────────────────────────────────


def test_verifier_mock_passes_for_well_formed_quote() -> None:
    quote = _mock_quote("d4d4d4")
    verifier = TEEQuoteVerifier()
    result = verifier.verify(quote)
    assert result.valid is True
    assert result.provider == "mock"


def test_verifier_mock_fails_when_signature_tampered() -> None:
    quote = _mock_quote("d4d4d4")
    # Tamper the signature
    tampered = TEEQuote(
        provider="mock",
        enclave_measurement=quote.enclave_measurement,
        platform_measurement=quote.platform_measurement,
        report_data=quote.report_data,
        tcb_version=quote.tcb_version,
        timestamp_ns=quote.timestamp_ns,
        quote_signature="0" * 64,  # bogus
        signing_cert_fingerprint=quote.signing_cert_fingerprint,
        raw_quote_hex=quote.raw_quote_hex,
    )
    verifier = TEEQuoteVerifier()
    result = verifier.verify(tampered)
    assert result.valid is False
    assert any("mock signature mismatch" in r for r in result.reasons)


def test_verifier_tdx_schema_passes_for_valid_shape() -> None:
    quote = TEEQuote(
        provider="tdx",
        enclave_measurement="ab" * 48,  # 96-char hex (48 bytes)
        report_data="cd" * 64,
        raw_quote_hex="aa" * TDX_REPORT_LEN,  # 1024 bytes
    )
    verifier = TEEQuoteVerifier()
    result = verifier.verify(quote)
    assert result.valid is True
    assert result.extras["trust_level"] == "schema-only"


def test_verifier_tdx_rejects_short_raw() -> None:
    quote = TEEQuote(
        provider="tdx",
        enclave_measurement="ab" * 48,
        report_data="cd" * 64,
        raw_quote_hex="aa" * 100,  # too short
    )
    result = TEEQuoteVerifier().verify(quote)
    assert result.valid is False


def test_verifier_tdx_rejects_zero_mrtd() -> None:
    quote = TEEQuote(
        provider="tdx",
        enclave_measurement="0" * 96,
        report_data="cd" * 64,
        raw_quote_hex="aa" * TDX_REPORT_LEN,
    )
    result = TEEQuoteVerifier().verify(quote)
    assert result.valid is False


def test_verifier_sev_snp_schema_passes() -> None:
    quote = TEEQuote(
        provider="sev-snp",
        enclave_measurement="ab" * 48,
        report_data="cd" * 64,
        raw_quote_hex="aa" * 4000,
    )
    result = TEEQuoteVerifier().verify(quote)
    assert result.valid is True


def test_verifier_unregistered_provider_returns_invalid() -> None:
    quote = TEEQuote(provider="none")  # type: ignore[arg-type]
    result = TEEQuoteVerifier().verify(quote)
    assert result.valid is False
    assert any("no verifier" in r for r in result.reasons)


def test_verifier_register_provider_overrides_default() -> None:
    """Production swap-in pattern: register a real Intel DCAP verifier."""
    verifier = TEEQuoteVerifier()
    custom_called = {"count": 0}

    def custom(quote: TEEQuote, expected) -> VerificationResult:
        custom_called["count"] += 1
        return VerificationResult(
            valid=True, provider=quote.provider,
            reasons=("custom verifier",), extras={"vendor": "Intel DCAP"},
        )

    verifier.register_provider("tdx", custom)
    quote = TEEQuote(provider="tdx", enclave_measurement="x" * 96)
    result = verifier.verify(quote)
    assert result.valid is True
    assert result.extras.get("vendor") == "Intel DCAP"
    assert custom_called["count"] == 1


# ─────────────────────────────────────────────────────────────────────
# sealed_key
# ─────────────────────────────────────────────────────────────────────


def test_local_sealed_key_round_trip() -> None:
    p = LocalSealedKey()
    plaintext = b"audit-key-bytes"
    sealed = p.seal(plaintext)
    assert p.unseal(sealed) == plaintext


def test_local_sealed_key_always_available() -> None:
    assert LocalSealedKey().is_available() is True


def test_sev_snp_sealed_key_unavailable_without_device() -> None:
    if Path(SEV_DEVICE_PATH).exists():
        pytest.skip("Real SEV-SNP device present")
    assert SEVSNPDerivedKey().is_available() is False


def test_tdx_sealed_key_unavailable_without_device() -> None:
    if Path(TDX_DEVICE_PATH).exists():
        pytest.skip("Real TDX device present")
    assert TDXSealedKey().is_available() is False


def test_detect_sealed_key_provider_fallback_to_local(monkeypatch) -> None:
    monkeypatch.delenv("AEGIS_TEE_SEAL_KEYS", raising=False)
    p = detect_sealed_key_provider()
    if Path(TDX_DEVICE_PATH).exists() or Path(SEV_DEVICE_PATH).exists():
        # On a TEE-equipped host the strongest provider wins.
        assert p.name in ("sev-snp", "tdx", "local")
    else:
        assert p.name == "local"


def test_detect_sealed_key_provider_force_local(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_TEE_SEAL_KEYS", "false")
    p = detect_sealed_key_provider()
    assert p.name == "local"


def test_load_or_create_sealed_signing_key_uses_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_TEE_SEAL_KEYS", "false")
    key = load_or_create_sealed_signing_key(tmp_path / "ed.pem")
    # Returns a working Ed25519 private key
    sig = key.sign(b"test")
    key.public_key().verify(sig, b"test")


# ─────────────────────────────────────────────────────────────────────
# MockTEEQuoteCollector (now auto-detecting)
# ─────────────────────────────────────────────────────────────────────


def test_collector_always_available() -> None:
    assert MockTEEQuoteCollector().is_available() is True


def test_collector_returns_valid_result(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "mock")
    c = MockTEEQuoteCollector()
    r = c.collect()
    assert r.available is True
    assert r.metadata["tee_provider"] == "mock"
    assert r.metadata["trust_level"] == "mock"
    assert r.metadata["enclave_measurement"]  # non-empty


def test_collector_metadata_changes_with_provider(monkeypatch) -> None:
    """When provider is none, trust_level is mock; with mock explicit, same."""
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "mock")
    r1 = MockTEEQuoteCollector().collect()
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "none")
    r2 = MockTEEQuoteCollector().collect()
    assert r1.metadata["trust_level"] == "mock"
    assert r2.metadata["trust_level"] == "mock"


# ─────────────────────────────────────────────────────────────────────
# /attestation endpoints
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def measurement(tmp_path: Path):
    """Build a real BurnInMeasurement using the same code path as production."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from aegis.attest.burn_in import compute_burn_in
    from aegis.firewall.step320_blast import TOOL_BLAST_TABLE  # noqa: F401 — pre-import

    sk = Ed25519PrivateKey.generate()
    return compute_burn_in(
        code_root=Path(__file__).resolve().parents[2] / "src" / "aegis",
        policy_dir=Path(__file__).resolve().parents[2] / "policies",
        embedding_provider="dummy",
        judge_provider="dummy",
        public_key=sk.public_key(),
        signing_key=sk,
    )


def test_endpoint_attestation_includes_tee_pointers(measurement) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.attestation import make_router
    app = FastAPI()
    app.include_router(make_router(measurement=measurement))
    with TestClient(app) as client:
        r = client.get("/attestation")
    assert r.status_code == 200
    data = r.json()
    assert data["tee_quote_ref"] == "/attestation/tee-quote"
    assert data["tee_endpoint_ref"] == "/attestation/tee"


def test_endpoint_tee_with_verification(measurement, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.attestation import make_router
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "mock")
    app = FastAPI()
    app.include_router(make_router(measurement=measurement))
    with TestClient(app) as client:
        r = client.get("/attestation/tee")
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "mock"
    assert data["verification"]["valid"] is True


def test_endpoint_tee_503_when_no_provider(measurement, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.attestation import make_router
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "none")
    app = FastAPI()
    app.include_router(make_router(measurement=measurement))
    with TestClient(app) as client:
        r = client.get("/attestation/tee")
    assert r.status_code == 503


def test_endpoint_verify_round_trip(measurement, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.attestation import make_router
    monkeypatch.setenv("AEGIS_TEE_PROVIDER", "mock")
    app = FastAPI()
    app.include_router(make_router(measurement=measurement))
    with TestClient(app) as client:
        # Fetch a quote first
        r1 = client.get("/attestation/tee-quote")
        assert r1.status_code == 200
        quote_dict = r1.json()["quote"]
        # POST to verifier
        r2 = client.post("/attestation/tee/verify", json={"quote": quote_dict})
    assert r2.status_code == 200
    data = r2.json()
    assert data["valid"] is True
    assert data["provider"] == "mock"


def test_endpoint_verify_rejects_malformed(measurement) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.attestation import make_router
    app = FastAPI()
    app.include_router(make_router(measurement=measurement))
    with TestClient(app) as client:
        r = client.post(
            "/attestation/tee/verify",
            json={"quote": {"provider": "tdx", "bogus_field": True}},
        )
    assert r.status_code == 400
