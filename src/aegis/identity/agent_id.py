"""Core identity dataclasses + Ed25519-signed proofs.

Design
------
``AgentIdentity`` is the structured envelope. The same identity may be
referenced multiple times (by aid + tenant) without re-signing. An
``IdentityProof`` is the cryptographic artefact that proves the
identity at a point in time — signed by the issuer's Ed25519 key
(typically the same audit-chain key, or a tenant-scoped key in
multi-tenant deployments).

A ``DelegationChain`` carries the chain of agents that delegated
authority. ``agent_A → agent_B → agent_C`` means C is acting on B's
behalf, which is acting on A's behalf. The firewall enforces that
the union of capabilities along the chain is ≤ A's original set.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


@dataclass(frozen=True)
class AgentIdentity:
    """Structured identity for one agent at one point in time.

    Attributes
    ----------
    tenant_id:
        Multi-tenant scope. Identity uniqueness is per-tenant.
    aid:
        Agent identifier — already the ATV header's ``aid`` field.
    did:
        Optional W3C DID URI (e.g. ``did:aegis:demo:agent-A``,
        ``did:web:api.example.com:agents:42``, ``did:key:z6Mk...``).
        When present, the firewall resolves it via :class:`DIDResolver`
        for cross-org trust.
    capabilities:
        Set of capability claim strings (e.g. ``"read_files"``,
        ``"send_email"``, ``"execute_shell"``). Claims are scoped per
        tenant; the firewall checks tool calls against this set in
        addition to the existing blast-radius / policy gates.
    parent_aid:
        For delegated agents — the aid of the agent that spawned this
        one (typically the orchestrator).
    issued_at_ns:
        Nanosecond timestamp of identity issuance.
    expires_at_ns:
        Optional expiry — None means non-expiring. Recommend short
        TTLs (≤1 hour) for production; identities are cheap to re-sign.
    """

    tenant_id: str
    aid: str
    did: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    parent_aid: str | None = None
    issued_at_ns: int = 0
    expires_at_ns: int | None = None

    def is_expired(self, now_ns: int | None = None) -> bool:
        if self.expires_at_ns is None:
            return False
        n = now_ns if now_ns is not None else time.time_ns()
        return n >= self.expires_at_ns

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities

    def to_canonical_dict(self) -> dict[str, Any]:
        """JSON-serialisable canonical form for signing."""
        return {
            "tenant_id": self.tenant_id,
            "aid": self.aid,
            "did": self.did,
            "capabilities": sorted(self.capabilities),
            "parent_aid": self.parent_aid,
            "issued_at_ns": self.issued_at_ns,
            "expires_at_ns": self.expires_at_ns,
        }


@dataclass(frozen=True)
class IdentityProof:
    """Ed25519-signed proof of an :class:`AgentIdentity`.

    Wire format on the network:

    ``base64(canonical-json(identity)) || "." || base64(ed25519-sig)``

    A verifier with the issuer's public key can re-canonicalise + verify
    the signature in <1 ms.
    """

    identity: AgentIdentity
    signature: bytes
    issuer_pubkey_fingerprint: str  # SHA3-256 of the verifier pubkey bytes

    def to_compact_token(self) -> str:
        """Encode as the wire format above. Suitable for HTTP header."""
        import json
        body = json.dumps(
            self.identity.to_canonical_dict(),
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return (
            base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
            + "."
            + base64.urlsafe_b64encode(self.signature).rstrip(b"=").decode("ascii")
            + "."
            + self.issuer_pubkey_fingerprint
        )

    @classmethod
    def from_compact_token(cls, token: str) -> IdentityProof:
        """Decode a compact token back into an IdentityProof.
        Raises ValueError on malformed input."""
        import json
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError(f"expected 3 parts, got {len(parts)}")
        body_b64, sig_b64, fp = parts
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        data = json.loads(body)
        ident = AgentIdentity(
            tenant_id=str(data["tenant_id"]),
            aid=str(data["aid"]),
            did=data.get("did"),
            capabilities=frozenset(data.get("capabilities", [])),
            parent_aid=data.get("parent_aid"),
            issued_at_ns=int(data.get("issued_at_ns", 0)),
            expires_at_ns=(
                int(data["expires_at_ns"])
                if data.get("expires_at_ns") is not None else None
            ),
        )
        return cls(identity=ident, signature=sig, issuer_pubkey_fingerprint=fp)


@dataclass(frozen=True)
class DelegationChain:
    """Chain of identities — agent C acting on behalf of B acting on
    behalf of A. Element 0 is the top of the chain (original principal),
    element N is the immediate caller.

    The firewall enforces that:
    - Each element's capabilities ⊆ predecessor's capabilities
    - Each element's tenant_id matches its predecessor's
    - Each element's parent_aid matches predecessor's aid

    Use :meth:`is_valid` to check the chain locally; cryptographic
    verification of each proof is :class:`IdentityVerifier`'s job.
    """

    proofs: tuple[IdentityProof, ...]

    def __len__(self) -> int:
        return len(self.proofs)

    @property
    def root(self) -> IdentityProof:
        if not self.proofs:
            raise ValueError("empty delegation chain")
        return self.proofs[0]

    @property
    def caller(self) -> IdentityProof:
        if not self.proofs:
            raise ValueError("empty delegation chain")
        return self.proofs[-1]

    def is_valid(self) -> tuple[bool, str | None]:
        """Local structural validation (no signature check).
        Returns ``(True, None)`` or ``(False, "reason")``."""
        if not self.proofs:
            return False, "empty chain"
        for i, proof in enumerate(self.proofs[1:], start=1):
            prev = self.proofs[i - 1].identity
            cur = proof.identity
            if cur.tenant_id != prev.tenant_id:
                return False, (
                    f"tenant mismatch at index {i}: "
                    f"{cur.tenant_id} != {prev.tenant_id}"
                )
            if cur.parent_aid != prev.aid:
                return False, (
                    f"parent_aid mismatch at index {i}: "
                    f"{cur.parent_aid} != {prev.aid}"
                )
            # Capabilities must be a subset of the predecessor's.
            if not cur.capabilities.issubset(prev.capabilities):
                extra = cur.capabilities - prev.capabilities
                return False, (
                    f"capability escalation at index {i}: "
                    f"new capabilities {sorted(extra)} not in predecessor"
                )
        return True, None


# ─────────────────────────────────────────────────────────────────────
# Issuer / signer helpers
# ─────────────────────────────────────────────────────────────────────


def fingerprint_pubkey(pub_key: Ed25519PublicKey) -> str:
    """SHA3-256 hex of the pubkey raw bytes (32 bytes Ed25519)."""
    from cryptography.hazmat.primitives import serialization

    raw = pub_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha3_256(raw).hexdigest()


def issue(
    identity: AgentIdentity,
    *,
    signing_key: Ed25519PrivateKey,
    pub_key: Ed25519PublicKey | None = None,
) -> IdentityProof:
    """Sign an identity, returning a verifiable proof."""
    import json

    body = json.dumps(
        identity.to_canonical_dict(), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    sig = signing_key.sign(body)
    pk = pub_key or signing_key.public_key()
    fp = fingerprint_pubkey(pk)
    return IdentityProof(
        identity=identity, signature=sig, issuer_pubkey_fingerprint=fp,
    )
