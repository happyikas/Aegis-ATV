"""TEE-sealed signing key abstraction (v4.4).

When running inside a TDX / SEV-SNP guest, the audit Ed25519 signing
key should be **sealed** under the TEE's hardware root key — so a
host-OS compromise can't steal it. The seal/unseal operations are
ioctl-based and provider-specific (TDX has none yet upstream; SEV-SNP
uses ``SNP_GET_DERIVED_KEY``; ARM CCA uses CCA-MEASURE).

What v4.4 ships
---------------
* :class:`SealedKeyProvider` — the abstraction. Has two methods:
  ``seal(plaintext) -> bytes`` and ``unseal(ciphertext) -> bytes``.
* :class:`LocalSealedKey` — fallback that uses a local file (no
  hardware sealing). Encryption-at-rest only via filesystem perms.
* :class:`SEVSNPDerivedKey` — uses ``SNP_GET_DERIVED_KEY`` to derive
  a 32-byte AES key from the TCB; we use it to AES-256-GCM wrap the
  Ed25519 key. Stub for now (real ioctl path documented).
* :class:`TDXSealedKey` — placeholder. TDX upstream doesn't yet have
  a stable seal API; expected to land via the Intel TPM-bridge in
  Linux 6.10+. We document the contract.
* :func:`load_or_create_sealed_signing_key` — auto-selects based on
  the live TEE provider and falls back to ``load_or_create_key``.

Security note
-------------
This v4.4 release does **not** automatically migrate existing
``./keys/ed25519.pem`` files to sealed form. Upgrading to TEE sealing
is an explicit operator action: stop the sidecar, run
``aegis tee seal-keys``, restart with ``AEGIS_TEE_SEAL_KEYS=true``.
The CLI tool is a v4.5 milestone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SealedKeyProvider(Protocol):
    """Interface every TEE-sealing backend implements."""

    name: str

    def is_available(self) -> bool: ...

    def seal(self, plaintext: bytes) -> bytes:
        """Return ciphertext that only this TEE instance can unseal."""

    def unseal(self, ciphertext: bytes) -> bytes:
        """Recover plaintext. Raises if the TEE state has changed
        (different MRTD / measurement → key is invalid)."""


# ─────────────────────────────────────────────────────────────────────
# Local fallback (no TEE)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class LocalSealedKey:
    """Pass-through 'sealing' — relies on filesystem permissions only.

    ``seal()`` returns the plaintext as-is; ``unseal()`` returns it
    back. Use only when no TEE is available; sets ``trust_level=fs-only``.
    """

    name: str = "local"

    def is_available(self) -> bool:
        return True

    def seal(self, plaintext: bytes) -> bytes:
        return plaintext

    def unseal(self, ciphertext: bytes) -> bytes:
        return ciphertext


# ─────────────────────────────────────────────────────────────────────
# SEV-SNP derived key wrapping
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SEVSNPDerivedKey:
    """SEV-SNP backend using ``SNP_GET_DERIVED_KEY`` to wrap the audit key.

    The derived key is bound to (TCB version, VMPL, guest field
    select), so a downgrade or different VMPL produces a different
    key — automatically invalidating the ciphertext.

    v4.4 surfaces the contract; the concrete ioctl call is a future
    milestone (depends on AMD ``snpguest`` 0.5+ stabilising the
    ``SNP_GET_DERIVED_KEY`` API).
    """

    name: str = "sev-snp"

    def is_available(self) -> bool:
        from aegis.attest.tee_ioctl import SEV_DEVICE_PATH

        return Path(SEV_DEVICE_PATH).exists()

    def seal(self, plaintext: bytes) -> bytes:  # pragma: no cover — stub
        raise NotImplementedError(
            "SEV-SNP key sealing is a v4.5 milestone. "
            "Use AEGIS_TEE_SEAL_KEYS=false for now."
        )

    def unseal(self, ciphertext: bytes) -> bytes:  # pragma: no cover — stub
        raise NotImplementedError("see seal()")


# ─────────────────────────────────────────────────────────────────────
# TDX seal placeholder
# ─────────────────────────────────────────────────────────────────────


@dataclass
class TDXSealedKey:
    """TDX backend — stub.

    TDX upstream Linux doesn't yet ship a stable seal API. Expected to
    land via the Intel TPM bridge driver in Linux 6.10+. Until then,
    operators on TDX hosts should use the Intel KMS or external
    HSM (AWS KMS / Azure Key Vault) to wrap the audit key.
    """

    name: str = "tdx"

    def is_available(self) -> bool:
        from aegis.attest.tee_ioctl import TDX_DEVICE_PATH

        return Path(TDX_DEVICE_PATH).exists()

    def seal(self, plaintext: bytes) -> bytes:  # pragma: no cover — stub
        raise NotImplementedError(
            "TDX key sealing requires Linux 6.10+ + Intel TPM bridge. "
            "Use AEGIS_TEE_SEAL_KEYS=false on older kernels."
        )

    def unseal(self, ciphertext: bytes) -> bytes:  # pragma: no cover — stub
        raise NotImplementedError("see seal()")


# ─────────────────────────────────────────────────────────────────────
# Auto-select
# ─────────────────────────────────────────────────────────────────────


def detect_sealed_key_provider() -> SealedKeyProvider:
    """Pick the strongest available backend.

    Priority: SEV-SNP > TDX > local. ``AEGIS_TEE_SEAL_KEYS=false``
    forces local even when TEE devices exist (for ops who haven't
    migrated their key files yet)."""
    if os.environ.get("AEGIS_TEE_SEAL_KEYS", "auto").lower() == "false":
        return LocalSealedKey()

    sev = SEVSNPDerivedKey()
    if sev.is_available():
        return sev
    tdx = TDXSealedKey()
    if tdx.is_available():
        return tdx
    return LocalSealedKey()


# ─────────────────────────────────────────────────────────────────────
# Loader — drop-in replacement for aegis.sign.ed25519.load_or_create_key
# ─────────────────────────────────────────────────────────────────────


def load_or_create_sealed_signing_key(
    path: Path,
    *,
    provider: SealedKeyProvider | None = None,
) -> Ed25519PrivateKey:
    """Load (or create) the audit Ed25519 key, optionally wrapping it
    with a TEE-derived key.

    Behaviour:
    * If ``provider.is_available()`` and TEE sealing enabled, the key
      file on disk is the **ciphertext** of the Ed25519 raw bytes.
      We unseal at boot, reconstruct the key, return it.
    * If unavailable / disabled, we fall back to plain
      :func:`aegis.sign.ed25519.load_or_create_key` (existing path).

    The file path encoding is identical so existing deployments keep
    working — the difference is whether the bytes are plaintext.
    """
    from aegis.sign.ed25519 import load_or_create_key

    p = provider if provider is not None else detect_sealed_key_provider()
    if isinstance(p, LocalSealedKey):
        return load_or_create_key(path)

    # TEE provider available — but seal/unseal are stubs in v4.4.
    # Document and fall back to local path with a one-line warning.
    import logging

    logging.getLogger(__name__).warning(
        "TEE seal provider %s available but seal/unseal not yet "
        "implemented. Falling back to local key. Set "
        "AEGIS_TEE_SEAL_KEYS=false to silence this warning.",
        p.name,
    )
    return load_or_create_key(path)


__all__ = [
    "LocalSealedKey",
    "SEVSNPDerivedKey",
    "SealedKeyProvider",
    "TDXSealedKey",
    "detect_sealed_key_provider",
    "load_or_create_sealed_signing_key",
]
