"""Unit tests for src/aegis/identity/* + step308 (v4.2, Claim 56)."""

from __future__ import annotations

import json
import time
from typing import Any

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from aegis.firewall import step308_identity
from aegis.firewall.core import FirewallContext
from aegis.identity import (
    AgentIdentity,
    DelegationChain,
    DIDResolver,
    IdentityProof,
    IdentityVerifier,
    MCPAegisMiddleware,
    UnknownDIDMethodError,
    UnverifiedIdentityError,
)
from aegis.identity.agent_id import fingerprint_pubkey, issue
from aegis.identity.did import (
    AEGIS_DID_METHOD,
    DIDDocument,
    make_aegis_resolver,
    make_did_key_resolver,
    make_did_web_stub,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

# ─────────────────────────────────────────────────────────────────────
# AgentIdentity / IdentityProof
# ─────────────────────────────────────────────────────────────────────


def test_identity_dataclass_fields() -> None:
    ident = AgentIdentity(tenant_id="t", aid="a", capabilities=frozenset({"x"}))
    assert ident.tenant_id == "t"
    assert ident.has_capability("x")
    assert not ident.has_capability("y")


def test_identity_is_expired() -> None:
    past = AgentIdentity(tenant_id="t", aid="a", expires_at_ns=1)
    future = AgentIdentity(tenant_id="t", aid="a", expires_at_ns=time.time_ns() + 10**9)
    none = AgentIdentity(tenant_id="t", aid="a", expires_at_ns=None)
    assert past.is_expired() is True
    assert future.is_expired() is False
    assert none.is_expired() is False


def test_proof_signs_and_verifies() -> None:
    sk = Ed25519PrivateKey.generate()
    ident = AgentIdentity(
        tenant_id="t", aid="a", capabilities=frozenset({"read_file"}),
    )
    proof = issue(ident, signing_key=sk)
    verifier = IdentityVerifier(local_issuer=sk.public_key())
    assert verifier.verify(proof) is True


def test_proof_verification_fails_with_wrong_key() -> None:
    sk1 = Ed25519PrivateKey.generate()
    sk2 = Ed25519PrivateKey.generate()
    ident = AgentIdentity(tenant_id="t", aid="a")
    proof = issue(ident, signing_key=sk1)
    # Verifier using a DIFFERENT key
    verifier = IdentityVerifier(local_issuer=sk2.public_key())
    assert verifier.verify(proof) is False


def test_compact_token_round_trip() -> None:
    sk = Ed25519PrivateKey.generate()
    ident = AgentIdentity(
        tenant_id="t1", aid="agent-X",
        capabilities=frozenset({"a", "b"}),
        parent_aid="orchestrator",
        issued_at_ns=12345,
        expires_at_ns=99999,
    )
    proof = issue(ident, signing_key=sk)
    token = proof.to_compact_token()
    decoded = IdentityProof.from_compact_token(token)
    assert decoded.identity == ident
    # Signature byte-identical
    assert decoded.signature == proof.signature
    assert decoded.issuer_pubkey_fingerprint == proof.issuer_pubkey_fingerprint


def test_compact_token_malformed_raises() -> None:
    with pytest.raises(ValueError):
        IdentityProof.from_compact_token("not.enough")
    with pytest.raises(ValueError):
        IdentityProof.from_compact_token("a.b.c.d")


# ─────────────────────────────────────────────────────────────────────
# DelegationChain
# ─────────────────────────────────────────────────────────────────────


def _build_chain(sk: Ed25519PrivateKey) -> DelegationChain:
    """A → B → C with strictly-decreasing capabilities."""
    a = AgentIdentity(
        tenant_id="t", aid="agent-A",
        capabilities=frozenset({"read", "write", "shell"}),
    )
    b = AgentIdentity(
        tenant_id="t", aid="agent-B",
        capabilities=frozenset({"read", "write"}),
        parent_aid="agent-A",
    )
    c = AgentIdentity(
        tenant_id="t", aid="agent-C",
        capabilities=frozenset({"read"}),
        parent_aid="agent-B",
    )
    return DelegationChain(proofs=tuple(
        issue(i, signing_key=sk) for i in (a, b, c)
    ))


def test_delegation_chain_root_and_caller() -> None:
    sk = Ed25519PrivateKey.generate()
    chain = _build_chain(sk)
    assert chain.root.identity.aid == "agent-A"
    assert chain.caller.identity.aid == "agent-C"
    assert len(chain) == 3


def test_delegation_chain_capability_subset_valid() -> None:
    sk = Ed25519PrivateKey.generate()
    chain = _build_chain(sk)
    ok, err = chain.is_valid()
    assert ok is True
    assert err is None


def test_delegation_chain_capability_escalation_invalid() -> None:
    sk = Ed25519PrivateKey.generate()
    a = AgentIdentity(tenant_id="t", aid="A", capabilities=frozenset({"read"}))
    b = AgentIdentity(
        tenant_id="t", aid="B",
        capabilities=frozenset({"read", "WRITE"}),  # ← escalation
        parent_aid="A",
    )
    chain = DelegationChain(proofs=(issue(a, signing_key=sk), issue(b, signing_key=sk)))
    ok, err = chain.is_valid()
    assert ok is False
    assert "escalation" in (err or "").lower() or "WRITE" in (err or "")


def test_delegation_chain_tenant_mismatch_invalid() -> None:
    sk = Ed25519PrivateKey.generate()
    a = AgentIdentity(tenant_id="t1", aid="A")
    b = AgentIdentity(tenant_id="t2", aid="B", parent_aid="A")
    chain = DelegationChain(proofs=(issue(a, signing_key=sk), issue(b, signing_key=sk)))
    ok, err = chain.is_valid()
    assert ok is False
    assert "tenant" in (err or "").lower()


def test_delegation_chain_parent_mismatch_invalid() -> None:
    sk = Ed25519PrivateKey.generate()
    a = AgentIdentity(tenant_id="t", aid="A")
    b = AgentIdentity(tenant_id="t", aid="B", parent_aid="someone-else")
    chain = DelegationChain(proofs=(issue(a, signing_key=sk), issue(b, signing_key=sk)))
    ok, err = chain.is_valid()
    assert ok is False
    assert "parent" in (err or "").lower()


def test_verifier_chain_signature_check() -> None:
    sk = Ed25519PrivateKey.generate()
    chain = _build_chain(sk)
    verifier = IdentityVerifier(local_issuer=sk.public_key())
    ok, err = verifier.verify_chain(chain)
    assert ok is True
    assert err is None


# ─────────────────────────────────────────────────────────────────────
# DID resolution
# ─────────────────────────────────────────────────────────────────────


def test_did_resolver_unknown_method_raises() -> None:
    resolver = DIDResolver()
    with pytest.raises(UnknownDIDMethodError):
        resolver.resolve("did:foo:bar")


def test_did_resolver_malformed_uri() -> None:
    resolver = DIDResolver()
    with pytest.raises(ValueError):
        resolver.resolve("not-a-did")
    with pytest.raises(ValueError):
        resolver.resolve("did:")


def test_aegis_did_resolver() -> None:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    table: dict[tuple[str, str], Ed25519PublicKey] = {("t1", "agent-X"): pk}

    def lookup(tenant: str, aid: str) -> Ed25519PublicKey:
        return table[(tenant, aid)]

    resolver = DIDResolver()
    resolver.register_method(AEGIS_DID_METHOD, make_aegis_resolver(public_key_lookup=lookup))
    doc = resolver.resolve("did:aegis:t1:agent-X")
    assert doc.tenant_id == "t1"
    assert doc.aid == "agent-X"
    assert doc.pubkey_fingerprint == fingerprint_pubkey(pk)


def test_aegis_did_resolver_missing_aid_raises() -> None:
    def lookup(tenant: str, aid: str) -> Ed25519PublicKey:
        raise KeyError(f"{tenant}:{aid}")

    resolver = DIDResolver()
    resolver.register_method(AEGIS_DID_METHOD, make_aegis_resolver(public_key_lookup=lookup))
    with pytest.raises(ValueError):
        resolver.resolve("did:aegis:t:missing")


def test_did_web_stub_always_raises() -> None:
    resolver = DIDResolver()
    resolver.register_method("web", make_did_web_stub())
    with pytest.raises(UnknownDIDMethodError):
        resolver.resolve("did:web:example.com:agent-A")


def test_did_key_resolver_round_trip() -> None:
    """Encode a public key as did:key, resolve, recover the key."""
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    from cryptography.hazmat.primitives import serialization

    raw = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # Build the did:key URI manually using base58btc
    from aegis.identity.did import _B58_ALPHA

    n = int.from_bytes(raw, "big")
    encoded = b""
    while n > 0:
        n, rem = divmod(n, 58)
        encoded = bytes([_B58_ALPHA[rem]]) + encoded
    did = "did:key:z" + encoded.decode("ascii")

    resolver = DIDResolver()
    resolver.register_method("key", make_did_key_resolver())
    doc = resolver.resolve(did)
    assert isinstance(doc, DIDDocument)
    assert doc.pubkey_fingerprint == fingerprint_pubkey(pk)


def test_verifier_with_did_resolver() -> None:
    """End-to-end: identity carries DID, verifier resolves + verifies."""
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()

    def lookup(tenant: str, aid: str) -> Ed25519PublicKey:
        return pk

    resolver = DIDResolver()
    resolver.register_method(AEGIS_DID_METHOD, make_aegis_resolver(public_key_lookup=lookup))
    verifier = IdentityVerifier(resolver=resolver)

    ident = AgentIdentity(
        tenant_id="t", aid="agent-X", did="did:aegis:t:agent-X",
    )
    proof = issue(ident, signing_key=sk)
    assert verifier.verify(proof) is True


def test_verifier_with_did_fingerprint_mismatch() -> None:
    """If the proof's claimed fingerprint doesn't match what the
    resolver returns, verification fails."""
    sk_real = Ed25519PrivateKey.generate()
    sk_imposter = Ed25519PrivateKey.generate()

    def lookup(tenant: str, aid: str) -> Ed25519PublicKey:
        return sk_real.public_key()

    resolver = DIDResolver()
    resolver.register_method(AEGIS_DID_METHOD, make_aegis_resolver(public_key_lookup=lookup))
    verifier = IdentityVerifier(resolver=resolver)

    ident = AgentIdentity(tenant_id="t", aid="agent-X", did="did:aegis:t:agent-X")
    # Sign with the imposter key — fingerprint won't match resolver's pubkey
    proof = issue(ident, signing_key=sk_imposter)
    assert verifier.verify(proof) is False


# ─────────────────────────────────────────────────────────────────────
# MCP middleware
# ─────────────────────────────────────────────────────────────────────


def test_mcp_middleware_issue_proof() -> None:
    sk = Ed25519PrivateKey.generate()
    verifier = IdentityVerifier(local_issuer=sk.public_key())
    middleware = MCPAegisMiddleware(
        identity_verifier=verifier, signing_key=sk,
    )
    ident = AgentIdentity(tenant_id="t", aid="a")
    proof = middleware.issue_proof(ident)
    assert verifier.verify(proof) is True


def test_mcp_middleware_verify_inbound_round_trip() -> None:
    sk = Ed25519PrivateKey.generate()
    verifier = IdentityVerifier(local_issuer=sk.public_key())
    middleware = MCPAegisMiddleware(
        identity_verifier=verifier, signing_key=sk,
    )
    ident = AgentIdentity(tenant_id="t", aid="a")
    token = middleware.issue_proof(ident).to_compact_token()
    recovered = middleware.verify_inbound(token)
    assert recovered.aid == "a"


def test_mcp_middleware_rejects_bad_token() -> None:
    sk = Ed25519PrivateKey.generate()
    sk2 = Ed25519PrivateKey.generate()
    verifier = IdentityVerifier(local_issuer=sk.public_key())
    middleware = MCPAegisMiddleware(
        identity_verifier=verifier, signing_key=sk2,
    )
    ident = AgentIdentity(tenant_id="t", aid="a")
    # Sign with a key that does NOT match the verifier's local_issuer
    bad_token = middleware.issue_proof(ident).to_compact_token()
    with pytest.raises(UnverifiedIdentityError):
        middleware.verify_inbound(bad_token)


def test_mcp_middleware_evaluate_via_handler() -> None:
    """Middleware's evaluate() short-circuits via a test handler."""
    sk = Ed25519PrivateKey.generate()
    verifier = IdentityVerifier(local_issuer=sk.public_key())

    captured: dict[str, Any] = {}

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return {"decision": "ALLOW", "reason": "test"}

    middleware = MCPAegisMiddleware(
        identity_verifier=verifier, signing_key=sk,
        evaluate_handler=handler,
    )
    ident = AgentIdentity(tenant_id="t", aid="a")
    ctx = middleware.build_context(
        tool_name="read_file", tool_args={"file_path": "/x"},
        identity=ident,
    )
    verdict = middleware.evaluate(ctx)
    assert verdict["decision"] == "ALLOW"
    assert captured["payload"]["tool_name"] == "read_file"
    assert captured["payload"]["header"]["aid"] == "a"


# ─────────────────────────────────────────────────────────────────────
# Step 308 (firewall integration)
# ─────────────────────────────────────────────────────────────────────


def _atv_input(tenant: str, aid: str, *, tool: str = "read_file",
               proof_token: str | None = None) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id=tenant, aid=aid, timestamp_ns=0,
        ),
        tool_name=tool,
        tool_args_json=json.dumps({"file_path": "/x"}),
        cost_estimate=CostEfficiencyMetrics(),
        agent_identity_proof_token=proof_token,
    )


@pytest.fixture(autouse=True)
def _reset_step308_verifier() -> None:
    step308_identity.reset_verifier_for_tests()
    yield
    step308_identity.reset_verifier_for_tests()


def test_step308_passes_when_no_proof_and_not_required(monkeypatch) -> None:
    monkeypatch.delenv("AEGIS_IDENTITY_REQUIRE", raising=False)
    inp = _atv_input("t", "a")
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict is None


def test_step308_blocks_when_no_proof_and_required(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_IDENTITY_REQUIRE", "true")
    inp = _atv_input("t", "a")
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict == "BLOCK"
    assert "missing" in result.reason


def test_step308_blocks_malformed_token() -> None:
    inp = _atv_input("t", "a", proof_token="not-a-valid.token")
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict == "BLOCK"


def test_step308_full_flow_passes_for_valid_proof(tmp_path, monkeypatch) -> None:
    """End-to-end: write a fresh signing key, sign an identity, verify."""
    key_path = tmp_path / "ed.pem"
    monkeypatch.setattr("aegis.config.settings.aegis_signing_key_path", str(key_path))

    from aegis.sign.ed25519 import load_or_create_key

    sk = load_or_create_key(key_path)
    ident = AgentIdentity(
        tenant_id="t", aid="a",
        capabilities=frozenset({"read_file"}),
    )
    token = issue(ident, signing_key=sk).to_compact_token()
    inp = _atv_input("t", "a", tool="read_file", proof_token=token)
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict is None
    assert "verified_identity" in ctx.extras
    assert ctx.extras["verified_identity"].aid == "a"


def test_step308_blocks_tenant_mismatch(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "ed.pem"
    monkeypatch.setattr("aegis.config.settings.aegis_signing_key_path", str(key_path))
    from aegis.sign.ed25519 import load_or_create_key

    sk = load_or_create_key(key_path)
    ident = AgentIdentity(tenant_id="other-tenant", aid="a")
    token = issue(ident, signing_key=sk).to_compact_token()
    inp = _atv_input("t", "a", proof_token=token)
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict == "BLOCK"
    assert "tenant" in result.reason.lower()


def test_step308_blocks_capability_mismatch(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "ed.pem"
    monkeypatch.setattr("aegis.config.settings.aegis_signing_key_path", str(key_path))
    from aegis.sign.ed25519 import load_or_create_key

    sk = load_or_create_key(key_path)
    # Capability ⊅ tool name → BLOCK
    ident = AgentIdentity(
        tenant_id="t", aid="a",
        capabilities=frozenset({"read_file"}),
    )
    token = issue(ident, signing_key=sk).to_compact_token()
    inp = _atv_input("t", "a", tool="execute_shell", proof_token=token)
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict == "BLOCK"
    assert "capability" in result.reason.lower()


def test_step308_blocks_expired_proof(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "ed.pem"
    monkeypatch.setattr("aegis.config.settings.aegis_signing_key_path", str(key_path))
    from aegis.sign.ed25519 import load_or_create_key

    sk = load_or_create_key(key_path)
    ident = AgentIdentity(tenant_id="t", aid="a", expires_at_ns=1)
    token = issue(ident, signing_key=sk).to_compact_token()
    inp = _atv_input("t", "a", proof_token=token)
    ctx = FirewallContext()
    result = step308_identity.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert result.verdict == "BLOCK"
    assert "expired" in result.reason.lower()
