"""Agent identity & cross-agent trust (v4.2, Claim 56).

Multi-agent systems need to know **who is calling, on whose behalf,
with what privileges**. v4.2 introduces a forward-compatible identity
layer that maps cleanly onto:

* **W3C DID-Agent** standard (in progress 2026)
* **Anthropic MCP** server-side authentication (Model Context Protocol)
* The existing M14 per-AID circuit breaker

Three pieces:

1. :class:`AgentIdentity` — structured identity carrying tenant_id,
   aid, optional W3C DID, capability claims, parent agent (for
   delegation chains).

2. :class:`IdentityProof` — Ed25519-signed proof of identity.
   Verifiable against the issuer's public key.

3. :class:`MCPAegisMiddleware` — reference adapter showing how an
   MCP server fronts every tool call through Aegis ``/evaluate``.

Wired into the firewall as ``step308_identity`` — runs before the
existing step310 args check so an unverified identity short-circuits
the rest of the pipeline.
"""

from __future__ import annotations

from aegis.identity.agent_id import (
    AgentIdentity,
    DelegationChain,
    IdentityProof,
)
from aegis.identity.did import (
    AEGIS_DID_METHOD,
    DIDDocument,
    DIDResolver,
    IdentityVerifier,
    UnknownDIDMethodError,
    UnverifiedIdentityError,
)
from aegis.identity.mcp import (
    MCPAegisMiddleware,
    MCPCallContext,
)

__all__ = [
    "AEGIS_DID_METHOD",
    "AgentIdentity",
    "DIDDocument",
    "DIDResolver",
    "DelegationChain",
    "IdentityProof",
    "IdentityVerifier",
    "MCPAegisMiddleware",
    "MCPCallContext",
    "UnknownDIDMethodError",
    "UnverifiedIdentityError",
]
