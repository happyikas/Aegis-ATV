"""Tests for the optional Ed25519 audit-signing layer (v4.4)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aegis.audit.local_chain import append, verify_chain
from aegis.audit.signing import (
    default_private_key_path,
    default_public_key_path,
    init_signing_key,
    load_keypair,
    load_private_key_or_none,
    load_public_key_or_none,
    sign_hash,
    verify_hash_signature,
    verify_record_signature,
)


@pytest.fixture
def signing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test's signing key + audit log under tmp_path,
    so the user's real ~/.aegis is never touched."""
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    priv = keys_dir / "audit.ed25519"
    pub = keys_dir / "audit.ed25519.pub"
    monkeypatch.setenv("AEGIS_AUDIT_SIGNING_KEY", str(priv))
    monkeypatch.setenv("AEGIS_AUDIT_PUBKEY", str(pub))
    monkeypatch.setenv("AEGIS_LOCAL_AUDIT", str(tmp_path / "audit.jsonl"))
    return tmp_path


# ──────────────────────────────────────────────────────────────────────
# Key generation + load
# ──────────────────────────────────────────────────────────────────────


class TestKeyManagement:
    def test_init_creates_files_with_correct_modes(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        priv = default_private_key_path()
        pub = default_public_key_path()
        assert priv.is_file()
        assert pub.is_file()
        # Private must be 0600.
        assert (priv.stat().st_mode & 0o777) == 0o600
        # Public should be world-readable.
        assert (pub.stat().st_mode & 0o777) == 0o644
        # 32-byte raw keys.
        assert len(priv.read_bytes()) == 32
        assert len(pub.read_bytes()) == 32
        # Fingerprint is 16 hex chars.
        assert len(kp.fingerprint) == 16
        assert all(c in "0123456789abcdef" for c in kp.fingerprint)

    def test_init_idempotent_without_force(
        self, signing_env: Path,
    ) -> None:
        kp1 = init_signing_key()
        kp2 = init_signing_key()  # default force=False, files already exist
        assert kp1.fingerprint == kp2.fingerprint

    def test_init_force_rotates_key(self, signing_env: Path) -> None:
        kp1 = init_signing_key()
        kp2 = init_signing_key(force=True)
        # Different key → different fingerprint.
        assert kp1.fingerprint != kp2.fingerprint

    def test_load_private_key_or_none_returns_none_when_absent(
        self, signing_env: Path,
    ) -> None:
        assert load_private_key_or_none() is None

    def test_load_keypair_round_trip(self, signing_env: Path) -> None:
        kp1 = init_signing_key()
        kp2 = load_keypair()
        assert kp1.fingerprint == kp2.fingerprint
        # Public key bytes match.
        assert kp1.public_bytes == kp2.public_bytes

    def test_load_public_key_or_none_returns_none_when_absent(
        self, signing_env: Path,
    ) -> None:
        assert load_public_key_or_none() is None

    def test_load_public_key_or_none_returns_pair(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        loaded = load_public_key_or_none()
        assert loaded is not None
        _pk, fp = loaded
        assert fp == kp.fingerprint


# ──────────────────────────────────────────────────────────────────────
# Sign + verify primitives
# ──────────────────────────────────────────────────────────────────────


class TestSignVerify:
    def test_sign_and_verify_round_trip(self, signing_env: Path) -> None:
        kp = init_signing_key()
        h = "a" * 64
        sig = sign_hash(h, kp)
        assert len(sig) == 128            # 64 bytes hex-encoded
        assert verify_hash_signature(h, sig, kp.public_key) is True

    def test_verify_rejects_tampered_hash(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        sig = sign_hash("a" * 64, kp)
        # Same key, different hash → bad signature.
        assert (
            verify_hash_signature("b" * 64, sig, kp.public_key) is False
        )

    def test_verify_rejects_tampered_signature(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        h = "a" * 64
        good = sign_hash(h, kp)
        # Flip one byte in the signature.
        tampered = good[:-2] + ("00" if good[-2:] != "00" else "ff")
        assert (
            verify_hash_signature(h, tampered, kp.public_key) is False
        )

    def test_verify_handles_malformed_signature(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        # Non-hex.
        assert (
            verify_hash_signature("a" * 64, "zzz", kp.public_key)
            is False
        )
        # Empty.
        assert verify_hash_signature("a" * 64, "", kp.public_key) is False

    def test_verify_record_signature_helper(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        h = "deadbeef" * 8  # 64 chars
        rec = {
            "this_hash": h,
            "signature": sign_hash(h, kp),
        }
        assert verify_record_signature(rec, kp.public_key) is True
        # Missing fields → False.
        assert verify_record_signature({}, kp.public_key) is False


# ──────────────────────────────────────────────────────────────────────
# Append / verify_chain integration
# ──────────────────────────────────────────────────────────────────────


class TestAppendAndChain:
    def test_append_without_key_no_signature_field(
        self, signing_env: Path,
    ) -> None:
        # Don't init — no key on disk.
        audit_path = Path(os.environ["AEGIS_LOCAL_AUDIT"])
        rec = append(audit_path, {"tool": "Bash", "aid": "x", "ts_ns": 1})
        assert "signature" not in rec
        assert "pubkey_fingerprint" not in rec

    def test_append_with_key_attaches_signature(
        self, signing_env: Path,
    ) -> None:
        kp = init_signing_key()
        audit_path = Path(os.environ["AEGIS_LOCAL_AUDIT"])
        rec = append(audit_path, {"tool": "Bash", "aid": "x", "ts_ns": 1})
        assert isinstance(rec.get("signature"), str)
        assert len(rec["signature"]) == 128
        assert rec.get("pubkey_fingerprint") == kp.fingerprint

    def test_chain_verifies_with_signing_enabled(
        self, signing_env: Path,
    ) -> None:
        init_signing_key()
        audit_path = Path(os.environ["AEGIS_LOCAL_AUDIT"])
        for i in range(5):
            append(audit_path, {"tool": "Bash", "aid": "a", "ts_ns": i})
        ok, broken_at, total = verify_chain(audit_path)
        assert ok is True
        assert broken_at == -1
        assert total == 5

    def test_chain_tolerates_unsigned_legacy_records(
        self, signing_env: Path,
    ) -> None:
        # Append unsigned, then init key, then append signed.
        # All records should still verify.
        audit_path = Path(os.environ["AEGIS_LOCAL_AUDIT"])
        append(audit_path, {"tool": "Read", "aid": "a", "ts_ns": 1})
        append(audit_path, {"tool": "Read", "aid": "a", "ts_ns": 2})

        init_signing_key()
        append(audit_path, {"tool": "Bash", "aid": "a", "ts_ns": 3})

        ok, _, total = verify_chain(audit_path)
        assert ok is True
        assert total == 3

    def test_tampered_signature_breaks_chain(
        self, signing_env: Path,
    ) -> None:
        init_signing_key()
        audit_path = Path(os.environ["AEGIS_LOCAL_AUDIT"])
        append(audit_path, {"tool": "Bash", "aid": "a", "ts_ns": 1})
        append(audit_path, {"tool": "Bash", "aid": "a", "ts_ns": 2})
        # Forge the signature on record #1.
        lines = audit_path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["signature"] = "00" * 64
        lines[1] = json.dumps(rec)
        audit_path.write_text("\n".join(lines) + "\n")

        ok, broken_at, total = verify_chain(audit_path)
        assert ok is False
        assert broken_at == 1

    def test_signature_excluded_from_hash_payload(
        self, signing_env: Path,
    ) -> None:
        # The hash MUST be computed without the signature in the
        # payload (the signature signs the hash, so including it would
        # be circular). Verify by recomputing manually.
        from aegis.audit.local_chain import _hash_record

        kp = init_signing_key()
        audit_path = Path(os.environ["AEGIS_LOCAL_AUDIT"])
        rec = append(audit_path, {"tool": "Bash", "aid": "a", "ts_ns": 1})

        # Recompute with signature stripped — must match this_hash.
        recomputed = _hash_record(rec["prev_hash"], rec)
        assert recomputed == rec["this_hash"]
        # The signature itself is well-formed.
        from aegis.audit.signing import verify_hash_signature
        assert verify_hash_signature(
            rec["this_hash"], rec["signature"], kp.public_key,
        ) is True


# ──────────────────────────────────────────────────────────────────────
# KeyPair dataclass invariants
# ──────────────────────────────────────────────────────────────────────


class TestKeyPairData:
    def test_private_bytes_is_32(self, signing_env: Path) -> None:
        kp = init_signing_key()
        assert len(kp.private_bytes) == 32

    def test_public_bytes_is_32(self, signing_env: Path) -> None:
        kp = init_signing_key()
        assert len(kp.public_bytes) == 32

    def test_keypair_is_frozen(self, signing_env: Path) -> None:
        kp = init_signing_key()
        with pytest.raises((TypeError, AttributeError)):
            kp.fingerprint = "different"  # type: ignore[misc]
