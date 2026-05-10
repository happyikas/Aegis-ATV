"""Pinned issuer public key for license-token verification.

The runtime trusts exactly one public key per ``kid`` (key ID). When a
license JWS arrives, :func:`get_issuer_public_key` looks up the pinned
key for the ``kid`` in the JWS header and returns it; if the ``kid`` is
unknown, verification fails.

Why this is **pinned in source** rather than fetched from a network
endpoint: the Solo Free contract guarantees zero outbound network
requests by default. A "phone home to fetch the issuer key" step would
poison that contract for everyone, so we ship the public key as
constant bytes inside the binary.

Why a **placeholder** key for the v0.2 PR landing this module:

* The corresponding *private* key has been discarded — without it,
  nobody (including the maintainer) can issue a license that this
  runtime accepts. That's the correct contract for a "no-op gate" PR:
  the plumbing is in place, but the gate doesn't gate anything until
  the issuer service (separate repo) ships and we rotate to a real
  key.
* Tests patch :data:`ISSUER_PUBLIC_KEYS` with a key they own (see
  ``tests/unit/license/conftest.py``) so the test suite is fully
  hermetic.

Rotating to a real issuer key (planned for the issuer-service PR):

1. Generate a fresh keypair offline (HSM if available, otherwise
   ``cryptography`` on an air-gapped laptop).
2. Replace the PEM block below with the new public key. Bump
   ``DEFAULT_KID`` to e.g. ``"aegis-license-2027"``.
3. Keep the old key in :data:`ISSUER_PUBLIC_KEYS` for ~30 days so
   licenses minted under the previous key continue to verify; remove
   afterwards. (See ``docs/LICENSE_KEY.md`` §3 — "Layer A: short
   expiry".)
"""

from __future__ import annotations

from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# The default ``kid`` (key ID) header value the issuer stamps on
# tokens it mints. Bumped on each annual key rotation.
DEFAULT_KID: Final[str] = "aegis-license-2026-placeholder"


# Placeholder Ed25519 public key. The matching private key has been
# discarded — see module docstring. Replace at issuer-service launch.
_PLACEHOLDER_PUBLIC_KEY_PEM: Final[bytes] = (
    b"-----BEGIN PUBLIC KEY-----\n"
    b"MCowBQYDK2VwAyEAJ4PmJTsRfM5dUcqDZvaY8LYb8LjbYf2T5K40dhSEwyI=\n"
    b"-----END PUBLIC KEY-----\n"
)


def _load_pem(pem: bytes) -> Ed25519PublicKey:
    pub = serialization.load_pem_public_key(pem)
    if not isinstance(pub, Ed25519PublicKey):
        # Should never happen unless someone replaces the PEM with a
        # non-Ed25519 key. Surfaced as TypeError so any swap is loud.
        raise TypeError(
            f"pinned issuer key is not Ed25519: {type(pub).__name__}"
        )
    return pub


# ``kid`` → public key. The runtime only trusts kids in this dict.
# Multi-entry support is for the rotation window described in the
# module docstring — ship N+1 alongside N for ~30 days, then drop N.
ISSUER_PUBLIC_KEYS: Final[dict[str, Ed25519PublicKey]] = {
    DEFAULT_KID: _load_pem(_PLACEHOLDER_PUBLIC_KEY_PEM),
}


def get_issuer_public_key(kid: str) -> Ed25519PublicKey | None:
    """Look up the pinned public key for ``kid``.

    Returns ``None`` when the ``kid`` is unknown — caller should treat
    that as "verification failed (untrusted issuer)" without raising,
    so a malformed JWS doesn't crash the firewall path.
    """
    return ISSUER_PUBLIC_KEYS.get(kid)
