"""MCP (Model Context Protocol) integration middleware.

MCP — Anthropic-led standard for tool calling — defines a JSON-RPC
protocol over stdio / HTTP / WebSocket. This module ships a reference
adapter that sits between an MCP **client** (the agent) and an MCP
**server** (the tool provider) and routes every tool call through
Aegis ``/evaluate`` before forwarding.

Why not import the official MCP SDK?
------------------------------------
- The MCP SDK is in flux (v0.x). v4.2 keeps the dependency at zero so
  Aegis stays installable in environments that haven't pinned an MCP
  client. The middleware is a pure HTTP + JSON adapter that production
  deployments wire to whatever MCP transport they use.
- We expose the **integration contract** so SDK adapters (Python,
  TypeScript, Go) can plug in trivially.

Usage shape (production)
------------------------
    middleware = MCPAegisMiddleware(
        aegis_url="http://aegis:8080",
        identity_verifier=verifier,
        signing_key=signing_key,
    )
    # In your MCP server, every tool invocation:
    ctx = middleware.build_context(
        tool_name=tool_name,
        tool_args=tool_args,
        identity_proof=request.headers["X-Aegis-Identity"],
        ...
    )
    verdict = middleware.evaluate(ctx)
    if verdict.decision != "ALLOW":
        return mcp_error("blocked by Aegis", details=verdict)
    return tool.execute(tool_args)

Test mode
---------
The middleware holds the verifier + signing key but DOES NOT spawn
HTTP. Tests can call ``build_context()`` + ``evaluate()`` directly —
``evaluate`` is overridable to return a mock verdict for fast tests
without the sidecar running.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aegis.identity.agent_id import (
    AgentIdentity,
    DelegationChain,
    IdentityProof,
    issue,
)
from aegis.identity.did import IdentityVerifier


@dataclass
class MCPCallContext:
    """One MCP tool invocation, ready to post to ``/evaluate``."""

    tool_name: str
    tool_args: dict[str, Any]
    identity: AgentIdentity
    delegation_chain: DelegationChain | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_evaluate_payload(self) -> dict[str, Any]:
        """Build the body for ``POST /evaluate``."""
        # The aegis ATVInput model accepts header.tenant_id + .aid;
        # extra identity context rides in metadata for now.
        return {
            "header": {
                "trace_id": self.metadata.get("trace_id", "0" * 32),
                "span_id": self.metadata.get("span_id", "0" * 16),
                "tenant_id": self.identity.tenant_id,
                "aid": self.identity.aid,
                "timestamp_ns": self.identity.issued_at_ns,
            },
            "tool_name": self.tool_name,
            "tool_args_json": json.dumps(self.tool_args, separators=(",", ":")),
            "agent_state_text": self.metadata.get("state_text", ""),
            # Capability claims travel as a hint — the firewall can
            # cross-check tool against the identity's capability set.
            "capability_manifest": sorted(self.identity.capabilities),
        }


VerdictHandler = Callable[[dict[str, Any]], dict[str, Any]]


class MCPAegisMiddleware:
    """Reference adapter — fronts an MCP server with Aegis evaluation."""

    def __init__(
        self,
        *,
        aegis_url: str = "http://localhost:8080",
        identity_verifier: IdentityVerifier,
        signing_key: Ed25519PrivateKey | None = None,
        timeout_s: float = 5.0,
        evaluate_handler: VerdictHandler | None = None,
    ) -> None:
        self._aegis_url = aegis_url.rstrip("/")
        self._verifier = identity_verifier
        self._signing_key = signing_key
        self._timeout_s = timeout_s
        self._evaluate_handler = evaluate_handler

    # ── Identity helpers ─────────────────────────────────────────────

    def issue_proof(self, identity: AgentIdentity) -> IdentityProof:
        """Sign an identity using the configured signing key."""
        if self._signing_key is None:
            raise RuntimeError("MCPAegisMiddleware: no signing_key configured")
        return issue(identity, signing_key=self._signing_key)

    def verify_inbound(self, proof_token: str) -> AgentIdentity:
        """Verify a proof presented in an MCP request header. Returns
        the identity if valid, raises :class:`UnverifiedIdentityError`
        otherwise."""
        proof = IdentityProof.from_compact_token(proof_token)
        if not self._verifier.verify(proof):
            from aegis.identity.did import UnverifiedIdentityError
            raise UnverifiedIdentityError(
                f"identity proof for {proof.identity.aid} failed verification"
            )
        return proof.identity

    # ── Context building + dispatch ──────────────────────────────────

    def build_context(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        identity: AgentIdentity,
        delegation_chain: DelegationChain | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        state_text: str = "",
    ) -> MCPCallContext:
        return MCPCallContext(
            tool_name=tool_name,
            tool_args=tool_args,
            identity=identity,
            delegation_chain=delegation_chain,
            metadata={
                "trace_id": trace_id or "0" * 32,
                "span_id": span_id or "0" * 16,
                "state_text": state_text,
            },
        )

    def evaluate(self, ctx: MCPCallContext) -> dict[str, Any]:
        """POST the context to ``/evaluate`` and return the verdict.

        For unit tests pass ``evaluate_handler`` to short-circuit the
        HTTP call — useful when no sidecar is running.
        """
        payload = ctx.to_evaluate_payload()
        if self._evaluate_handler is not None:
            return self._evaluate_handler(payload)
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self._aegis_url}/evaluate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return data


__all__ = ["MCPAegisMiddleware", "MCPCallContext"]
