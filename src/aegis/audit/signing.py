"""Optional Ed25519 signing for the local audit chain.

Plugin / local mode (Solo Free) ships with SHA3-chained audit but no
cryptographic signing — the chain proves "this log has not been mutated
post-write" but a sufficiently motivated attacker with file-write access
could re-compute the entire chain forward from the tampered record.

This module adds an OPT-IN signing layer:

* If a private key is available at
  ``~/.aegis/keys/audit.ed25519`` (or the path specified by
  ``AEGIS_AUDIT_SIGNING_KEY``), every appended record is signed.
* The signature covers ``this_hash`` (the chain pointer), so signing
  inherits the integrity properties of the chain — one signature per
  record verifies that record AND transitively all earlier records,
  via the prev_hash linkage.
* Records without a ``signature`` field are accepted by
  :func:`verify_chain` for backwards compatibility — turning signing
  on only adds new constraints, never breaks existing audits.

Why opt-in
----------

Solo Free contract says we don't force key management on
single-developer users. But operators who want
non-repudiation (e.g., for compliance, or for shared audit logs across
machines) can run ``aegis audit-key init`` to generate a keypair, and
all subsequent records gain a ``signature`` field that any holder of
the public key can verify with :func:`verify_record_signature`.

Threat model
------------

Without signing: SHA3 chain detects tampering of EXISTING records but
cannot detect REWRITES (attacker recomputes entire chain from any
tampered point forward; chain still verifies as internally consistent
but the original anchor at GENESIS is lost). With signing: each
record carries an Ed25519 signature over ``this_hash``. Recomputing
the chain requires the private key, which never leaves the host.

Files on disk
-------------

* ``~/.aegis/keys/audit.ed25519``      — 32-byte raw private key
* ``~/.aegis/keys/audit.ed25519.pub``  — 32-byte raw public key
* (Optional) override paths via ``AEGIS_AUDIT_SIGNING_KEY`` and
  ``AEGIS_AUDIT_PUBKEY``.

The keypair is created with :func:`init_signing_key` (idempotent —
won't overwrite without ``--force``). Loss of the private key means
no further signed records, but existing signatures remain verifiable
with the retained public key.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# ──────────────────────────────────────────────────────────────────────
# Default key paths
# ──────────────────────────────────────────────────────────────────────


def _default_keys_dir() -> Path:
    return Path.home() / ".aegis" / "keys"


def default_private_key_path() -> Path:
    """Path of the audit signing key.

    Honours ``AEGIS_AUDIT_SIGNING_KEY`` env override, falling back to
    ``~/.aegis/keys/audit.ed25519``."""
    override = os.environ.get("AEGIS_AUDIT_SIGNING_KEY", "").strip()
    if override:
        return Path(override).expanduser()
    return _default_keys_dir() / "audit.ed25519"


def default_public_key_path() -> Path:
    """Path of the corresponding public key.

    Defaults to ``<private>.pub`` next to the private key. Honours the
    ``AEGIS_AUDIT_PUBKEY`` override for verifier-only deployments."""
    override = os.environ.get("AEGIS_AUDIT_PUBKEY", "").strip()
    if override:
        return Path(override).expanduser()
    priv = default_private_key_path()
    return priv.with_name(priv.name + ".pub")


# ──────────────────────────────────────────────────────────────────────
# Key generation + load
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyPair:
    """In-memory Ed25519 keypair. ``fingerprint`` is the SHA3-256
    prefix of the public key bytes — short, stable, suitable for the
    ``pubkey_fingerprint`` field in audit records."""

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    fingerprint: str            # 16-char SHA3 hex

    @property
    def private_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    @property
    def public_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


def _fingerprint(public_bytes: bytes, length: int = 16) -> str:
    return hashlib.sha3_256(public_bytes).hexdigest()[:length]


def init_signing_key(
    *,
    private_path: Path | None = None,
    public_path: Path | None = None,
    force: bool = False,
) -> KeyPair:
    """Generate (or read) a Ed25519 keypair for audit signing.

    * ``force=False`` (default) — refuses to overwrite an existing
      private key file. Returns the existing keypair if both files
      already exist; otherwise creates them.
    * ``force=True`` — generates a fresh keypair, overwriting both
      files. Use this only when you accept losing all signing-history
      on the previous key.

    Files are written with mode ``0o600`` (private) / ``0o644`` (public).
    """
    private_path = private_path or default_private_key_path()
    public_path = public_path or default_public_key_path()

    if not force and private_path.is_file() and public_path.is_file():
        return load_keypair(
            private_path=private_path, public_path=public_path,
        )

    private_path.parent.mkdir(parents=True, exist_ok=True)

    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()

    from cryptography.hazmat.primitives import serialization

    priv_bytes = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    private_path.write_bytes(priv_bytes)
    private_path.chmod(0o600)
    public_path.write_bytes(pub_bytes)
    public_path.chmod(0o644)

    return KeyPair(
        private_key=sk,
        public_key=pk,
        fingerprint=_fingerprint(pub_bytes),
    )


def load_keypair(
    *,
    private_path: Path | None = None,
    public_path: Path | None = None,
) -> KeyPair:
    """Load both halves of the keypair from disk.

    Raises :class:`FileNotFoundError` if either is missing — callers
    that want "key may or may not exist" should prefer
    :func:`load_private_key_or_none`.
    """
    private_path = private_path or default_private_key_path()
    public_path = public_path or default_public_key_path()
    if not private_path.is_file():
        raise FileNotFoundError(
            f"audit signing key not found at {private_path}"
        )
    if not public_path.is_file():
        raise FileNotFoundError(
            f"audit signing pubkey not found at {public_path}"
        )

    sk = Ed25519PrivateKey.from_private_bytes(private_path.read_bytes())
    pub_bytes = public_path.read_bytes()
    pk = Ed25519PublicKey.from_public_bytes(pub_bytes)
    return KeyPair(
        private_key=sk, public_key=pk,
        fingerprint=_fingerprint(pub_bytes),
    )


def load_private_key_or_none(
    private_path: Path | None = None,
) -> KeyPair | None:
    """Best-effort load — returns ``None`` (instead of raising) when
    no signing key is configured. This is the entry point used by
    :func:`aegis.audit.local_chain.append` so missing-key is the
    silent default for Solo Free users."""
    private_path = private_path or default_private_key_path()
    if not private_path.is_file():
        return None
    public_path = private_path.with_name(private_path.name + ".pub")
    if not public_path.is_file():
        return None
    try:
        return load_keypair(
            private_path=private_path, public_path=public_path,
        )
    except (FileNotFoundError, ValueError):
        return None


def load_public_key_or_none(
    public_path: Path | None = None,
) -> tuple[Ed25519PublicKey, str] | None:
    """Verifier-only load. Returns (key, fingerprint) or None."""
    public_path = public_path or default_public_key_path()
    if not public_path.is_file():
        return None
    try:
        pub_bytes = public_path.read_bytes()
        return Ed25519PublicKey.from_public_bytes(pub_bytes), _fingerprint(pub_bytes)
    except (OSError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────
# Sign + verify
# ──────────────────────────────────────────────────────────────────────


def sign_hash(this_hash: str, keypair: KeyPair) -> str:
    """Sign a SHA3-chain ``this_hash`` value. Returns hex signature.

    The signature covers the hex digest as UTF-8 bytes — same encoding
    that's stored in the JSONL record. This keeps the verification
    code byte-trivial: it doesn't need to know the JSON canonicalisation
    rules used to build the hash itself, only how to recompute the
    same hex string.
    """
    return keypair.private_key.sign(this_hash.encode("utf-8")).hex()


def verify_hash_signature(
    this_hash: str,
    signature_hex: str,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify ``signature_hex`` is a valid Ed25519 signature over
    ``this_hash`` under ``public_key``. Returns ``True``/``False`` —
    never raises on bad input (returns False instead)."""
    if not signature_hex or not this_hash:
        return False
    try:
        sig_bytes = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    try:
        public_key.verify(sig_bytes, this_hash.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 — InvalidSignature, ValueError, etc.
        return False


def verify_record_signature(
    record: dict[str, object],
    public_key: Ed25519PublicKey,
) -> bool:
    """Convenience: extract ``signature`` and ``this_hash`` from a
    record and verify. Returns ``False`` for records with no signature
    (caller should typically allow them through, but they don't pass
    *signature* verification — only chain verification)."""
    sig = record.get("signature")
    this_hash = record.get("this_hash")
    if not isinstance(sig, str) or not isinstance(this_hash, str):
        return False
    return verify_hash_signature(this_hash, sig, public_key)
