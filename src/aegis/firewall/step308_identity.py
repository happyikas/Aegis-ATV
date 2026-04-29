"""Step 308 — Agent identity verification (v4.2, Claim 56).

Inserted between step 305 (safe-allowlist) and step 309 (instruction
drift). Reads an :class:`IdentityProof` from the ATV input — when
present — verifies it under the configured identity issuer key,
checks expiry + capability fit against the requested tool, and
attaches the verified identity to the firewall context for later
steps to consume.

Disabled by default for backward compat: when no identity proof is
provided, the step is a pass-through. Production deployments require
a proof by setting ``AEGIS_IDENTITY_REQUIRE=true``.
"""

from __future__ import annotations

import json
import os
from typing import Any

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

_REQUIRE_ENV = "AEGIS_IDENTITY_REQUIRE"


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    require = os.environ.get(_REQUIRE_ENV, "false").lower() in ("true", "1", "yes")
    raw_token = inp.agent_identity_proof_token

    if raw_token is None:
        if require:
            return StepResult(
                verdict="BLOCK",
                reason="agent identity proof required but missing",
                trace="step308: no proof + require=true → BLOCK",
            )
        return StepResult(
            verdict=None, reason="no proof provided",
            trace="step308: skipped (no proof, require=false)",
        )

    # Verify the proof using the lazily-built singleton verifier.
    try:
        verifier = _get_singleton_verifier()
    except Exception as e:  # noqa: BLE001
        # Verifier setup failed — soft-fail to avoid blocking the
        # whole pipeline on a config issue. Log via trace.
        return StepResult(
            verdict=None, reason="verifier unavailable",
            trace=f"step308: verifier setup failed ({type(e).__name__})",
        )

    from aegis.identity.agent_id import IdentityProof
    try:
        proof = IdentityProof.from_compact_token(raw_token)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return StepResult(
            verdict="BLOCK",
            reason=f"malformed identity proof: {e}",
            trace="step308: malformed token → BLOCK",
        )

    if proof.identity.is_expired():
        return StepResult(
            verdict="BLOCK", reason="identity proof expired",
            trace="step308: expired → BLOCK",
        )

    if not verifier.verify(proof):
        return StepResult(
            verdict="BLOCK", reason="identity signature failed verification",
            trace="step308: signature failed → BLOCK",
        )

    # Cross-check identity tenant/aid against the ATV header.
    if proof.identity.tenant_id != inp.header.tenant_id:
        return StepResult(
            verdict="BLOCK",
            reason=(
                f"identity tenant mismatch: "
                f"{proof.identity.tenant_id} != {inp.header.tenant_id}"
            ),
            trace="step308: tenant mismatch → BLOCK",
        )
    if proof.identity.aid != inp.header.aid:
        return StepResult(
            verdict="BLOCK",
            reason=f"identity aid mismatch: {proof.identity.aid} != {inp.header.aid}",
            trace="step308: aid mismatch → BLOCK",
        )

    # Capability check: the requested tool must be in the identity's
    # capability set if the set is non-empty (empty = "no claims =
    # default policy decides").
    caps = proof.identity.capabilities
    if caps and inp.tool_name not in caps:
        return StepResult(
            verdict="BLOCK",
            reason=(
                f"tool '{inp.tool_name}' not in identity capability set "
                f"{sorted(caps)}"
            ),
            trace="step308: capability mismatch → BLOCK",
        )

    # Stash the verified identity for later steps + audit annotation.
    ctx.extras["verified_identity"] = proof.identity
    return StepResult(
        verdict=None,
        reason="identity verified",
        trace=(
            f"step308: identity verified "
            f"(aid={proof.identity.aid}, "
            f"caps={len(caps)}, "
            f"expired=False)"
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Lazy verifier singleton
# ─────────────────────────────────────────────────────────────────────
_VERIFIER: Any = None


def _get_singleton_verifier() -> Any:
    """Build the IdentityVerifier once per process. Re-uses the audit
    Ed25519 key as the local issuer (the simplest single-org setup)."""
    global _VERIFIER
    if _VERIFIER is not None:
        return _VERIFIER

    from pathlib import Path

    from aegis.config import settings
    from aegis.identity.did import IdentityVerifier
    from aegis.sign.ed25519 import load_or_create_key

    # Reuse the telemetry signing key as the identity issuer for now.
    # Production: separate ed25519_identity.pem so identity rotation is
    # decoupled from audit-chain rotation.
    sk = load_or_create_key(Path(settings.aegis_signing_key_path))
    pk = sk.public_key()
    _VERIFIER = IdentityVerifier(local_issuer=pk)
    return _VERIFIER


def reset_verifier_for_tests() -> None:
    """Test helper — drop the cached verifier so a different key path
    can be picked up by the next call."""
    global _VERIFIER
    _VERIFIER = None
