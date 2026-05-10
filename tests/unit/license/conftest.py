"""Shared fixtures for the license tests.

The pinned issuer key in ``src/aegis/license/keys.py`` is a
placeholder — its private half has been discarded. Tests therefore
generate their own ephemeral Ed25519 keypair and patch
:data:`aegis.license.keys.ISSUER_PUBLIC_KEYS` so the test suite is
fully hermetic and never depends on the placeholder.

A ``mint`` fixture takes claim overrides and returns a signed JWS,
so individual tests stay focused on the path they're exercising.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aegis.license import keys as keys_mod
from aegis.license import set_active_license

TEST_KID = "aegis-license-TEST-KEY"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint_jws(
    priv: Ed25519PrivateKey,
    *,
    kid: str,
    payload: dict[str, Any],
) -> str:
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = priv.sign(f"{h_b64}.{p_b64}".encode("ascii"))
    return f"{h_b64}.{p_b64}.{_b64url(sig)}"


@pytest.fixture
def issuer_priv() -> Ed25519PrivateKey:
    """Ephemeral test issuer keypair — fresh for every test."""
    return Ed25519PrivateKey.generate()


@pytest.fixture
def patch_issuer_key(
    issuer_priv: Ed25519PrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Install the test public key into the runtime's pinned dict
    under :data:`TEST_KID`. Tests use that ``kid`` when minting.

    Cleanup is automatic via monkeypatch.
    """
    pub = issuer_priv.public_key()
    new_keys = dict(keys_mod.ISSUER_PUBLIC_KEYS)
    new_keys[TEST_KID] = pub
    monkeypatch.setattr(keys_mod, "ISSUER_PUBLIC_KEYS", new_keys)
    yield TEST_KID


@pytest.fixture
def mint(
    issuer_priv: Ed25519PrivateKey,
    patch_issuer_key: str,
) -> Callable[..., str]:
    """Returns a function ``mint(**overrides) -> jws_token``.

    Defaults produce a 1-year valid Pro license. Overrides replace
    individual claim fields; pass ``kid=...`` to force a different
    issuer key id (e.g. for the unknown-issuer test).
    """
    base_iat = int(time.time()) - 60   # 1 min ago, never future-dated
    base_exp = base_iat + 60 * 60 * 24 * 365

    def _mint(**overrides: Any) -> str:
        kid = overrides.pop("kid", patch_issuer_key)
        payload = {
            "iss": "https://license.test.example",
            "sub": "user_TEST",
            "aud": "aegis-mvp",
            "tier": "pro",
            "iat": base_iat,
            "exp": base_exp,
            "license_id": "lic_TEST_01",
            "seats": 1,
        }
        payload.update(overrides)
        return _mint_jws(issuer_priv, kid=kid, payload=payload)

    return _mint


@pytest.fixture
def reset_active_license() -> Iterator[None]:
    """Make sure the runtime starts each test in Solo Free state and
    leaves it that way too. Some tests `set_active_license(claims)`
    to test ``has_feature`` paths; this fixture cleans up after them
    so subsequent tests don't observe leftover state."""
    set_active_license(None)
    try:
        yield
    finally:
        set_active_license(None)
