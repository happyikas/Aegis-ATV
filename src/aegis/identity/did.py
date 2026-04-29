"""W3C DID resolver + identity verifier.

W3C DID standard: ``did:<method>:<method-specific-id>``. AegisData
ships native support for two methods plus a stub for a third:

* ``did:aegis:<tenant>:<aid>`` (NEW METHOD — defined here)
  Trust root is the AegisData audit Ed25519 key. Resolution is local
  (``./keys/`` lookup). Suitable for single-org deployments.

* ``did:key:<multibase-encoded-pubkey>``
  Self-resolving DID — the public key IS the identifier. Suitable for
  cross-org trust without a registry.

* ``did:web:<host>:<path>`` (STUB — fetches DID document over HTTPS)
  Reference contract for production swap-in. Verification deferred.

Forward-compat: any new method just registers a resolver via
:meth:`DIDResolver.register_method`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from aegis.identity.agent_id import (
    DelegationChain,
    IdentityProof,
    fingerprint_pubkey,
)

AEGIS_DID_METHOD = "aegis"


# ─────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────


class UnknownDIDMethodError(ValueError):
    """Raised when a DID method has no registered resolver."""


class UnverifiedIdentityError(ValueError):
    """Raised when an IdentityProof's signature does not verify."""


# ─────────────────────────────────────────────────────────────────────
# DID document
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DIDDocument:
    """The resolved metadata for a DID — minimal subset of the W3C spec.

    Production deployments may extend this with serviceEndpoint /
    verificationMethod arrays. v4.2 carries only what the firewall
    needs: the public key for signature verification, plus the
    advertised tenant + aid."""

    did: str
    pub_key: Ed25519PublicKey
    tenant_id: str
    aid: str

    @property
    def pubkey_fingerprint(self) -> str:
        return fingerprint_pubkey(self.pub_key)


# ─────────────────────────────────────────────────────────────────────
# DID resolver
# ─────────────────────────────────────────────────────────────────────

ResolverFunc = Callable[[str], DIDDocument]


class DIDResolver:
    """Pluggable resolver for W3C DID URIs.

    Methods register themselves via :meth:`register_method`. Resolution
    raises :class:`UnknownDIDMethodError` if the method has no resolver,
    or any backend-specific error (e.g. HTTP 404 for ``did:web``)."""

    def __init__(self) -> None:
        self._methods: dict[str, ResolverFunc] = {}

    def register_method(self, method: str, resolver: ResolverFunc) -> None:
        self._methods[method] = resolver

    def resolve(self, did: str) -> DIDDocument:
        if not did.startswith("did:"):
            raise ValueError(f"not a DID URI: {did}")
        parts = did.split(":")
        if len(parts) < 3:
            raise ValueError(f"malformed DID URI: {did}")
        method = parts[1]
        resolver = self._methods.get(method)
        if resolver is None:
            raise UnknownDIDMethodError(f"no resolver for did:{method}")
        return resolver(did)


def make_aegis_resolver(
    *, public_key_lookup: Callable[[str, str], Ed25519PublicKey],
) -> ResolverFunc:
    """Build a resolver for ``did:aegis:<tenant>:<aid>`` URIs.

    ``public_key_lookup(tenant_id, aid)`` returns the Ed25519 pubkey
    for that agent. Production: read from a tenant DB; tests: in-memory
    dict closure.
    """

    def _resolve(did: str) -> DIDDocument:
        parts = did.split(":")
        if len(parts) != 4 or parts[0] != "did" or parts[1] != AEGIS_DID_METHOD:
            raise ValueError(f"malformed did:aegis URI: {did}")
        tenant_id, aid = parts[2], parts[3]
        try:
            pk = public_key_lookup(tenant_id, aid)
        except KeyError as e:
            raise ValueError(f"no public key for {did}: {e}") from e
        return DIDDocument(did=did, pub_key=pk, tenant_id=tenant_id, aid=aid)

    return _resolve


def make_did_key_resolver() -> ResolverFunc:
    """``did:key:<multibase>`` — public key embedded in the DID itself.

    v4.2 supports the simple ``z`` (base58btc) prefix carrying a raw
    Ed25519 public key. Real W3C did:key uses a multicodec prefix
    (``0xed01`` for Ed25519); we accept both forms for forward compat.
    """

    def _resolve(did: str) -> DIDDocument:
        parts = did.split(":")
        if len(parts) != 3 or parts[0] != "did" or parts[1] != "key":
            raise ValueError(f"malformed did:key URI: {did}")
        body = parts[2]
        if not body.startswith("z"):
            raise ValueError("did:key must start with z (base58btc)")
        # Decode base58btc → raw 32-byte Ed25519 key. We use a
        # minimal alphabet decoder to avoid a pip dependency.
        raw = _b58decode(body[1:])
        # Strip multicodec prefix if present (0xed01 = Ed25519)
        if len(raw) == 34 and raw[0] == 0xed and raw[1] == 0x01:
            raw = raw[2:]
        if len(raw) != 32:
            raise ValueError(
                f"expected 32-byte Ed25519 public key, got {len(raw)}"
            )
        pk = Ed25519PublicKey.from_public_bytes(raw)
        return DIDDocument(did=did, pub_key=pk, tenant_id="", aid="")

    return _resolve


def make_did_web_stub() -> ResolverFunc:
    """``did:web:<host>:<path>`` — STUB. Production resolver fetches
    ``https://<host>/.well-known/did.json`` and parses the DID document.
    v4.2 only declares the contract; resolution always raises so
    deployments don't accidentally trust unverified web identities."""

    def _resolve(did: str) -> DIDDocument:
        raise UnknownDIDMethodError(
            "did:web resolver is a stub; configure a real one in production"
        )

    return _resolve


# Minimal base58btc decoder (Bitcoin alphabet) — no pip dependency.
_B58_ALPHA = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INV: dict[int, int] = {c: i for i, c in enumerate(_B58_ALPHA)}


def _b58decode(s: str) -> bytes:
    """Tiny base58btc decoder. Production uses a proper library; we
    inline a minimal implementation for v4.2 demo of did:key support."""
    n = 0
    for byte in s.encode("ascii"):
        if byte not in _B58_INV:
            raise ValueError(f"invalid base58 char: {chr(byte)}")
        n = n * 58 + _B58_INV[byte]
    # Account for leading '1's = leading zero bytes
    leading = 0
    for char in s:
        if char == "1":
            leading += 1
        else:
            break
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    return b"\x00" * leading + raw


# ─────────────────────────────────────────────────────────────────────
# Identity verifier
# ─────────────────────────────────────────────────────────────────────


class IdentityVerifier:
    """Verify :class:`IdentityProof` signatures against the issuer
    public key resolved via :class:`DIDResolver`.

    Two modes:
    1. **DID-rooted** — the proof carries a DID, resolver gives us the
       pubkey, signature is verified.
    2. **Local pubkey** — caller supplies the issuer pubkey directly
       (e.g. the audit Ed25519 key for in-org identities).
    """

    def __init__(
        self,
        *,
        resolver: DIDResolver | None = None,
        local_issuer: Ed25519PublicKey | None = None,
    ) -> None:
        self._resolver = resolver
        self._local_issuer = local_issuer

    def verify(self, proof: IdentityProof) -> bool:
        """Returns True iff the proof's signature verifies under the
        issuer pubkey resolved from the proof's identity."""
        try:
            self._verify_or_raise(proof)
            return True
        except (UnverifiedIdentityError, InvalidSignature, ValueError):
            return False

    def _verify_or_raise(self, proof: IdentityProof) -> None:
        import json

        # Determine which pubkey to use.
        if proof.identity.did and self._resolver is not None:
            doc = self._resolver.resolve(proof.identity.did)
            if doc.pubkey_fingerprint != proof.issuer_pubkey_fingerprint:
                raise UnverifiedIdentityError(
                    "proof fingerprint does not match resolved DID document"
                )
            pubkey = doc.pub_key
        elif self._local_issuer is not None:
            if (
                fingerprint_pubkey(self._local_issuer)
                != proof.issuer_pubkey_fingerprint
            ):
                raise UnverifiedIdentityError(
                    "proof fingerprint does not match local issuer key"
                )
            pubkey = self._local_issuer
        else:
            raise UnverifiedIdentityError(
                "no resolver or local issuer configured"
            )

        body = json.dumps(
            proof.identity.to_canonical_dict(),
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        try:
            pubkey.verify(proof.signature, body)
        except InvalidSignature as e:
            raise UnverifiedIdentityError("Ed25519 verify failed") from e

    def verify_chain(self, chain: DelegationChain) -> tuple[bool, str | None]:
        """Verify (a) every proof in the chain, then (b) chain structure."""
        for i, proof in enumerate(chain.proofs):
            if not self.verify(proof):
                return False, f"proof index {i} failed signature verify"
        ok, err = chain.is_valid()
        if not ok:
            return False, err
        return True, None


__all__ = [
    "AEGIS_DID_METHOD",
    "DIDDocument",
    "DIDResolver",
    "IdentityVerifier",
    "ResolverFunc",
    "UnknownDIDMethodError",
    "UnverifiedIdentityError",
    "make_aegis_resolver",
    "make_did_key_resolver",
    "make_did_web_stub",
]
