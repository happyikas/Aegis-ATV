"""JWS (Ed25519) license-token verification.

The license format is a compact JWS (RFC 7515) with three
base64url-encoded segments separated by ``.``:

    <header>.<payload>.<signature>

Header (JSON):
    { "alg": "EdDSA", "typ": "JWT", "kid": "aegis-license-2026" }

Payload claims (JSON):
    {
      "iss": "https://license.aegisdata.example",
      "sub": "user_01HRXY...",
      "aud": "aegis-mvp",
      "tier": "pro",                       # free | pro | team | enterprise
      "iat": 1762675200,
      "exp": 1794211200,
      "license_id": "lic_01HRXY...",
      "seats": 1,
      "features": ["advisor.full", ...],   # optional explicit allow-list
      "burnin_bind": "<sha3-256 hex>" or null
    }

Verification flow (matches ``docs/LICENSE_KEY.md`` §2):

  1. Split + base64-decode the three segments.
  2. Validate header alg is ``EdDSA`` and look up the pinned public
     key for the ``kid``.
  3. Verify the Ed25519 signature over ``<header>.<payload>``.
  4. Parse claims; check ``aud == "aegis-mvp"``, ``tier`` is known,
     ``exp`` is in the future. Optional ``burnin_bind`` matches the
     local Burn-in id if the *license* sets it.
  5. Return a typed :class:`LicenseClaims` on success or raise
     :class:`LicenseVerifyError` with a structured ``reason`` on
     failure.

Failures are **always raised**; the storage / CLI / runtime layers
catch the exception and decide how to react (degrade silently to
Solo Free, or surface to the user via ``aegis license status``).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from aegis.license.keys import get_issuer_public_key

# The fixed audience claim every Aegis license must declare. Matches
# the runtime's identifier; a license minted for a different aud
# (e.g. a future product line) won't be accepted.
EXPECTED_AUDIENCE: Final[str] = "aegis-mvp"

# Tier names the runtime understands. Anything else fails the check.
KNOWN_TIERS: Final[frozenset[str]] = frozenset(
    {"free", "pro", "team", "enterprise"},
)


class LicenseVerifyError(ValueError):
    """A license token failed verification.

    The ``reason`` attribute is a short, machine-stable string that
    callers can match on (e.g. ``"expired"`` for the "renew now" UX
    in :func:`aegis license status`).
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class LicenseClaims:
    """Verified claims from a license JWS.

    Frozen so callers can pass it freely without worrying about
    mutation. Pure data — :func:`features_for` derives the feature
    set from this.
    """

    tier: str
    iss: str
    sub: str
    aud: str
    iat: int
    exp: int
    license_id: str
    seats: int
    features: tuple[str, ...]
    burnin_bind: str | None
    # Convenience: the original ``kid`` used at verify-time, so
    # callers can record which issuer key was trusted.
    kid: str

    @property
    def expires_in_seconds(self) -> int:
        return self.exp - int(time.time())


def _b64url_decode(seg: str) -> bytes:
    # JWS uses URL-safe base64 *without* padding. Restore the padding
    # before feeding the stdlib decoder.
    pad = (-len(seg)) % 4
    return base64.urlsafe_b64decode(seg + ("=" * pad))


def _split_jws(token: str) -> tuple[str, str, str]:
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise LicenseVerifyError(
            "malformed", f"expected 3 dot-separated segments, got {len(parts)}",
        )
    return parts[0], parts[1], parts[2]


def _decode_header(raw: str) -> dict[str, Any]:
    try:
        body = json.loads(_b64url_decode(raw))
    except (ValueError, json.JSONDecodeError) as e:
        raise LicenseVerifyError("malformed-header", str(e)) from e
    if not isinstance(body, dict):
        raise LicenseVerifyError("malformed-header", "header is not a JSON object")
    return body


def _decode_payload(raw: str) -> dict[str, Any]:
    try:
        body = json.loads(_b64url_decode(raw))
    except (ValueError, json.JSONDecodeError) as e:
        raise LicenseVerifyError("malformed-payload", str(e)) from e
    if not isinstance(body, dict):
        raise LicenseVerifyError(
            "malformed-payload", "payload is not a JSON object",
        )
    return body


def _verify_signature(
    pub: Ed25519PublicKey, header_b64: str, payload_b64: str, sig_b64: str,
) -> None:
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        sig = _b64url_decode(sig_b64)
    except ValueError as e:
        raise LicenseVerifyError("malformed-signature", str(e)) from e
    try:
        pub.verify(sig, signing_input)
    except InvalidSignature as e:
        raise LicenseVerifyError(
            "bad-signature",
            "Ed25519 signature does not verify under the pinned issuer key",
        ) from e


def _validate_claims(
    claims: dict[str, Any],
    *,
    now_s: int,
    local_burnin_id: str | None,
    kid: str,
) -> LicenseClaims:
    # Required fields.
    for required in ("aud", "tier", "exp", "iat", "license_id", "iss", "sub"):
        if required not in claims:
            raise LicenseVerifyError(
                "missing-claim", f"required claim '{required}' is absent",
            )

    aud = claims["aud"]
    if aud != EXPECTED_AUDIENCE:
        raise LicenseVerifyError(
            "wrong-audience", f"aud={aud!r}, expected {EXPECTED_AUDIENCE!r}",
        )

    tier = claims["tier"]
    if tier not in KNOWN_TIERS:
        raise LicenseVerifyError(
            "unknown-tier", f"tier={tier!r}, expected one of {sorted(KNOWN_TIERS)}",
        )

    try:
        exp = int(claims["exp"])
        iat = int(claims["iat"])
    except (TypeError, ValueError) as e:
        raise LicenseVerifyError(
            "malformed-claim", f"exp/iat must be integers: {e}",
        ) from e

    if exp <= now_s:
        raise LicenseVerifyError(
            "expired",
            f"license expired at {exp}, now is {now_s}",
        )

    # ``burnin_bind`` is optional — when set on the license, the
    # local Burn-in id must match. Solo Pro keys are unbound (any
    # machine); Enterprise keys can be bound for non-portability.
    bind = claims.get("burnin_bind")
    if bind is not None:
        if not isinstance(bind, str):
            raise LicenseVerifyError(
                "malformed-claim",
                f"burnin_bind must be string, got {type(bind).__name__}",
            )
        if local_burnin_id is None:
            raise LicenseVerifyError(
                "burnin-bind-no-local",
                "license is burn-in-bound but the local Burn-in id is not "
                "available — cannot verify the bind",
            )
        if bind != local_burnin_id:
            raise LicenseVerifyError(
                "burnin-bind-mismatch",
                "license is bound to a different machine's Burn-in id",
            )

    seats_raw = claims.get("seats", 1)
    try:
        seats = int(seats_raw)
    except (TypeError, ValueError) as e:
        raise LicenseVerifyError(
            "malformed-claim", f"seats must be int: {e}",
        ) from e
    if seats < 1:
        raise LicenseVerifyError(
            "malformed-claim", f"seats must be >= 1, got {seats}",
        )

    # ``features`` is optional — the tier→features expansion is the
    # primary source. When present, it must be a list of strings.
    features_raw = claims.get("features", [])
    if not isinstance(features_raw, list) or not all(
        isinstance(f, str) for f in features_raw
    ):
        raise LicenseVerifyError(
            "malformed-claim", "features must be a list of strings",
        )

    return LicenseClaims(
        tier=tier,
        iss=str(claims["iss"]),
        sub=str(claims["sub"]),
        aud=aud,
        iat=iat,
        exp=exp,
        license_id=str(claims["license_id"]),
        seats=seats,
        features=tuple(features_raw),
        burnin_bind=bind,
        kid=kid,
    )


def verify_license(
    token: str,
    *,
    now_s: int | None = None,
    local_burnin_id: str | None = None,
) -> LicenseClaims:
    """Verify a JWS license token end-to-end.

    Args:
        token: The compact JWS — three base64url segments joined by
            ``.``.
        now_s: Override "now" in seconds since epoch (for tests).
            Defaults to wall clock.
        local_burnin_id: SHA3-256 hex of the running Aegis binary
            measurement. Must be supplied when the license has
            ``burnin_bind`` set, otherwise the bind check fails.

    Returns:
        :class:`LicenseClaims` on success.

    Raises:
        :class:`LicenseVerifyError`. Inspect ``.reason`` for one of:
        ``"malformed"``, ``"malformed-header"``, ``"malformed-payload"``,
        ``"malformed-signature"``, ``"unsupported-alg"``,
        ``"unknown-issuer"``, ``"bad-signature"``, ``"missing-claim"``,
        ``"wrong-audience"``, ``"unknown-tier"``, ``"expired"``,
        ``"burnin-bind-no-local"``, ``"burnin-bind-mismatch"``,
        ``"malformed-claim"``.
    """
    if not token or not isinstance(token, str):
        raise LicenseVerifyError("malformed", "token is empty or not a string")

    header_b64, payload_b64, sig_b64 = _split_jws(token)
    header = _decode_header(header_b64)

    alg = header.get("alg")
    if alg != "EdDSA":
        raise LicenseVerifyError(
            "unsupported-alg",
            f"alg={alg!r}; this runtime only accepts EdDSA",
        )

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise LicenseVerifyError("missing-claim", "header.kid is required")

    pub = get_issuer_public_key(kid)
    if pub is None:
        raise LicenseVerifyError(
            "unknown-issuer",
            f"no pinned public key for kid={kid!r}",
        )

    _verify_signature(pub, header_b64, payload_b64, sig_b64)
    payload = _decode_payload(payload_b64)
    return _validate_claims(
        payload,
        now_s=int(now_s) if now_s is not None else int(time.time()),
        local_burnin_id=local_burnin_id,
        kid=kid,
    )
