"""Tests for Ed25519 signing + key management."""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.sign.ed25519 import (
    load_or_create_key,
    load_public_key,
    sign_atv,
    verify,
)


def _sample_record(key, prev: str = "GENESIS") -> dict[str, object]:
    return sign_atv(b"hello-atv", {"aid": "a", "tenant_id": "t"}, prev, key)


def test_load_or_create_generates_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "ed.pem"
    assert not p.exists()
    key = load_or_create_key(p)
    assert p.exists()
    assert (tmp_path / "ed.pub").exists()
    # second call returns the same key (private bytes equal)
    key2 = load_or_create_key(p)
    from cryptography.hazmat.primitives import serialization

    fmt = serialization.PrivateFormat.PKCS8
    enc = serialization.NoEncryption()
    raw = serialization.Encoding.Raw
    a = key.private_bytes(serialization.Encoding.PEM, fmt, enc)
    b = key2.private_bytes(serialization.Encoding.PEM, fmt, enc)
    assert a == b
    # public key file is loadable
    _ = load_public_key(tmp_path / "ed.pub")
    _ = raw  # silence unused


def test_sign_and_verify_roundtrip(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    rec = _sample_record(key)
    pub = load_public_key(tmp_path / "k.pub")
    assert verify(rec, pub) is True


def test_tampered_payload_fails_verify(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    rec = _sample_record(key)
    rec["payload"]["header"]["aid"] = "evil"  # type: ignore[index]
    pub = load_public_key(tmp_path / "k.pub")
    assert verify(rec, pub) is False


def test_tampered_signature_fails_verify(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "k.pem")
    rec = _sample_record(key)
    sig = bytes.fromhex(rec["signature"])  # type: ignore[arg-type]
    flipped = bytes([sig[0] ^ 0x01]) + sig[1:]
    rec["signature"] = flipped.hex()
    pub = load_public_key(tmp_path / "k.pub")
    assert verify(rec, pub) is False


def test_signature_independent_of_dict_key_order(tmp_path: Path) -> None:
    """Canonicalization (sort_keys) means any key permutation verifies fine."""
    key = load_or_create_key(tmp_path / "k.pem")
    rec = _sample_record(key)
    # rebuild payload dict with reversed key order
    pl = rec["payload"]
    rec["payload"] = dict(reversed(list(pl.items())))  # type: ignore[arg-type]
    pub = load_public_key(tmp_path / "k.pub")
    assert verify(rec, pub) is True


def test_wrong_key_fails(tmp_path: Path) -> None:
    key1 = load_or_create_key(tmp_path / "k1.pem")
    _ = load_or_create_key(tmp_path / "k2.pem")
    rec = _sample_record(key1)
    other_pub = load_public_key(tmp_path / "k2.pub")
    assert verify(rec, other_pub) is False


def test_existing_non_ed25519_key_raises(tmp_path: Path) -> None:
    # write a plausible-looking PEM that isn't Ed25519
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "bad.pem"
    p.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(TypeError):
        load_or_create_key(p)
