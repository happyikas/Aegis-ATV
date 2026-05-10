"""Verify-path tests: happy + every failure-reason."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aegis.license import (
    EXPECTED_AUDIENCE,
    KNOWN_TIERS,
    LicenseVerifyError,
    verify_license,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ── happy path ────────────────────────────────────────────────────


def test_happy_path_returns_claims(mint: Callable[..., str]) -> None:
    token = mint()
    claims = verify_license(token)
    assert claims.tier == "pro"
    assert claims.aud == EXPECTED_AUDIENCE
    assert claims.license_id == "lic_TEST_01"
    assert claims.seats == 1
    assert claims.expires_in_seconds > 0


def test_known_tiers_all_accepted(mint: Callable[..., str]) -> None:
    for tier in KNOWN_TIERS:
        claims = verify_license(mint(tier=tier))
        assert claims.tier == tier


# ── malformed token ──────────────────────────────────────────────


def test_empty_token() -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license("")
    assert exc.value.reason == "malformed"


def test_non_string_token() -> None:
    with pytest.raises(LicenseVerifyError):
        verify_license(123)  # type: ignore[arg-type]


def test_wrong_segment_count() -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license("only.two")
    assert exc.value.reason == "malformed"


def test_malformed_header_base64(mint: Callable[..., str]) -> None:
    token = mint()
    parts = token.split(".")
    bad = "*" * 8 + "." + parts[1] + "." + parts[2]
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(bad)
    assert exc.value.reason == "malformed-header"


def test_malformed_header_not_json(
    issuer_priv: Ed25519PrivateKey, patch_issuer_key: str,
) -> None:
    # base64-decode-able but not JSON
    bad_header = _b64url(b"not json")
    payload = _b64url(json.dumps({"aud": EXPECTED_AUDIENCE}).encode())
    sig = _b64url(issuer_priv.sign(f"{bad_header}.{payload}".encode("ascii")))
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(f"{bad_header}.{payload}.{sig}")
    assert exc.value.reason == "malformed-header"


def test_unsupported_alg(
    issuer_priv: Ed25519PrivateKey, patch_issuer_key: str,
) -> None:
    header = _b64url(json.dumps(
        {"alg": "RS256", "typ": "JWT", "kid": patch_issuer_key}
    ).encode())
    payload = _b64url(json.dumps({"aud": EXPECTED_AUDIENCE}).encode())
    sig = _b64url(issuer_priv.sign(f"{header}.{payload}".encode("ascii")))
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(f"{header}.{payload}.{sig}")
    assert exc.value.reason == "unsupported-alg"


def test_missing_kid(
    issuer_priv: Ed25519PrivateKey, patch_issuer_key: str,
) -> None:
    header = _b64url(json.dumps({"alg": "EdDSA", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"aud": EXPECTED_AUDIENCE}).encode())
    sig = _b64url(issuer_priv.sign(f"{header}.{payload}".encode("ascii")))
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(f"{header}.{payload}.{sig}")
    assert exc.value.reason == "missing-claim"


def test_unknown_issuer_kid(mint: Callable[..., str]) -> None:
    """A kid that isn't in :data:`ISSUER_PUBLIC_KEYS` fails with
    ``unknown-issuer`` *before* the signature is even checked."""
    token = mint(kid="some-other-issuer")
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(token)
    assert exc.value.reason == "unknown-issuer"


def test_bad_signature_fails(
    mint: Callable[..., str],
) -> None:
    """Tamper the signature: still 3 segments but invalid sig."""
    token = mint()
    parts = token.split(".")
    # Flip the last byte before re-base64 — produces an invalid sig
    # of correct length.
    sig = bytearray(base64.urlsafe_b64decode(parts[2] + "=" * ((-len(parts[2])) % 4)))
    sig[-1] ^= 0xFF
    bad_sig = _b64url(bytes(sig))
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(f"{parts[0]}.{parts[1]}.{bad_sig}")
    assert exc.value.reason == "bad-signature"


def test_payload_tamper_invalidates_signature(
    mint: Callable[..., str],
) -> None:
    """Modify the payload after signing — sig fails because the
    signing input changed."""
    token = mint()
    parts = token.split(".")
    pad = (-len(parts[1])) % 4
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * pad))
    payload["tier"] = "enterprise"   # privilege escalation attempt
    new_payload = _b64url(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(f"{parts[0]}.{new_payload}.{parts[2]}")
    assert exc.value.reason == "bad-signature"


# ── claim validation ─────────────────────────────────────────────


def test_wrong_audience(mint: Callable[..., str]) -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(mint(aud="other-product"))
    assert exc.value.reason == "wrong-audience"


def test_unknown_tier(mint: Callable[..., str]) -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(mint(tier="ultraviolet"))
    assert exc.value.reason == "unknown-tier"


def test_expired_license(mint: Callable[..., str]) -> None:
    past = int(time.time()) - 60
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(mint(iat=past - 86400, exp=past))
    assert exc.value.reason == "expired"


def test_now_s_override_lets_test_clock_move(
    mint: Callable[..., str],
) -> None:
    """`now_s` parameter lets tests check the boundary cleanly."""
    token = mint()
    claims = verify_license(token, now_s=int(time.time()))
    # ... and a future-dated check fails.
    with pytest.raises(LicenseVerifyError, match="expired"):
        verify_license(token, now_s=claims.exp + 1)


def test_missing_claim_aud(
    issuer_priv: Ed25519PrivateKey, patch_issuer_key: str,
) -> None:
    header = _b64url(json.dumps(
        {"alg": "EdDSA", "typ": "JWT", "kid": patch_issuer_key}
    ).encode())
    payload = _b64url(json.dumps({  # no aud
        "iss": "x", "sub": "y", "tier": "pro",
        "iat": 1, "exp": 1_000_000_000_000, "license_id": "z",
    }).encode())
    sig = _b64url(issuer_priv.sign(f"{header}.{payload}".encode("ascii")))
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(f"{header}.{payload}.{sig}")
    assert exc.value.reason == "missing-claim"


def test_malformed_claim_seats_negative(mint: Callable[..., str]) -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(mint(seats=0))
    assert exc.value.reason == "malformed-claim"


def test_malformed_claim_features_not_list(mint: Callable[..., str]) -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(mint(features="not a list"))
    assert exc.value.reason == "malformed-claim"


def test_malformed_claim_features_with_nonstring(
    mint: Callable[..., str],
) -> None:
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(mint(features=["ok", 42]))
    assert exc.value.reason == "malformed-claim"


# ── burnin_bind ──────────────────────────────────────────────────


def test_burnin_bind_match(mint: Callable[..., str]) -> None:
    token = mint(burnin_bind="abc123")
    claims = verify_license(token, local_burnin_id="abc123")
    assert claims.burnin_bind == "abc123"


def test_burnin_bind_mismatch(mint: Callable[..., str]) -> None:
    token = mint(burnin_bind="machine-A")
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(token, local_burnin_id="machine-B")
    assert exc.value.reason == "burnin-bind-mismatch"


def test_burnin_bind_no_local(mint: Callable[..., str]) -> None:
    token = mint(burnin_bind="machine-A")
    # local_burnin_id not supplied → cannot verify the bind.
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(token)
    assert exc.value.reason == "burnin-bind-no-local"


def test_burnin_bind_optional(mint: Callable[..., str]) -> None:
    """When the license doesn't set burnin_bind, local_burnin_id is
    irrelevant."""
    claims = verify_license(mint(), local_burnin_id="anything-or-none")
    assert claims.burnin_bind is None


def test_burnin_bind_wrong_type(mint: Callable[..., str]) -> None:
    token = mint(burnin_bind=42)   # int, not str
    with pytest.raises(LicenseVerifyError) as exc:
        verify_license(token, local_burnin_id="x")
    assert exc.value.reason == "malformed-claim"
