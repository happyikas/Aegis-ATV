"""Ed25519 keypair management + ATV signing (PLAN 6.6)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    """Return the keypair at ``path``, generating one if it doesn't exist.

    The public key is written next to the private key with a ``.pub`` suffix.
    Both files are PEM-encoded; private is PKCS8-NoEncryption (MVP only,
    rotate to KMS-sealed in production).
    """

    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise TypeError(f"key at {path} is not Ed25519: {type(loaded).__name__}")
        return loaded

    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path = path.with_suffix(".pub")
    pub_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    loaded = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(loaded, Ed25519PublicKey):
        raise TypeError(f"key at {path} is not Ed25519 public: {type(loaded).__name__}")
    return loaded


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_atv(
    atv_bytes: bytes,
    header: dict[str, Any],
    prev_hash: str,
    key: Ed25519PrivateKey,
) -> dict[str, Any]:
    """Produce a signed Merkle-chained record for one ATV evaluation."""
    payload = {
        "atv_sha3_256": hashlib.sha3_256(atv_bytes).hexdigest(),
        "header": header,
        "prev_hash": prev_hash,
        "signed_at_ns": time.time_ns(),
    }
    msg = _canonical_json(payload)
    sig = key.sign(msg)
    return {
        "payload": payload,
        "signature": sig.hex(),
        "algorithm": "Ed25519",
    }


def verify(record: dict[str, Any], pub_key: Ed25519PublicKey) -> bool:
    """Verify the signature on a record against ``pub_key``."""
    try:
        msg = _canonical_json(record["payload"])
        pub_key.verify(bytes.fromhex(record["signature"]), msg)
        return True
    except Exception:
        return False
