"""Encrypted power-fail-safe ATV journal (patent §13B + Claim 21).

Each journal entry is wrapped in AES-256-GCM (AEAD) before persistence.
The wrapper carries:
    - 12-byte nonce (random per entry)
    - 16-byte authentication tag
    - schema_version + key_version + tenant_id + Agent Identifier
      headers (cleartext metadata for selective decryption)
    - the SHA3-256 commitment of the underlying ATV record
    - the encrypted payload itself (nonce || ciphertext || tag)

Each line in the on-disk journal is a single base64-encoded record;
truncated / torn writes fail the auth-tag check during replay and are
silently skipped (¶[0102G]).

T2 implementation: a single 256-bit data key loaded from
``AEGIS_JOURNAL_DATA_KEY_PATH`` (auto-generated on first run). T3
seals the key under the hardware TEE; the on-disk format is
forward-compatible.

Selective disclosure (¶[0102F]) is supported by exposing
``decrypt_record(line, key) -> dict`` so an authorized verifier can
decrypt one entry without seeing the rest.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LEN = 32          # 256-bit AES-GCM
NONCE_LEN = 12
DATA_KEY_VERSION = 1


def load_or_create_data_key(path: Path) -> bytes:
    """Read or generate the 256-bit data encryption key (T2)."""
    if path.exists():
        b = path.read_bytes()
        if len(b) != KEY_LEN:
            raise ValueError(
                f"data key at {path} has length {len(b)}, expected {KEY_LEN}"
            )
        return b
    key = secrets.token_bytes(KEY_LEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    with contextlib.suppress(OSError):
        path.chmod(0o600)  # filesystem may not support chmod
    return key


class EncryptedJournal:
    """Append-only AEAD-wrapped JSONL store with torn-write recovery."""

    def __init__(self, path: Path, data_key: bytes) -> None:
        if len(data_key) != KEY_LEN:
            raise ValueError(f"data_key must be {KEY_LEN} bytes, got {len(data_key)}")
        self.path = path
        self._aead = AESGCM(data_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Encrypt + append one record. Returns the wrapper metadata."""
        nonce = secrets.token_bytes(NONCE_LEN)
        plaintext = json.dumps(record, separators=(",", ":")).encode("utf-8")
        commitment = hashlib.sha3_256(plaintext).hexdigest()
        # Cleartext header travels with the record so a verifier can route +
        # locate the key without first decrypting the body.
        cleartext_header = {
            "schema_version": "EJOURN-v1",
            "key_version": DATA_KEY_VERSION,
            "tenant_id": record.get("payload", {}).get("header", {}).get("tenant_id"),
            "aid": record.get("payload", {}).get("header", {}).get("aid"),
            "atv_commitment": commitment,
            "ts_ns": time.time_ns(),
        }
        # The cleartext header is also AAD so the auth tag binds it.
        aad = json.dumps(cleartext_header, sort_keys=True, separators=(",", ":")).encode()
        ciphertext = self._aead.encrypt(nonce, plaintext, aad)
        wrapper = {
            **cleartext_header,
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
        line = json.dumps(wrapper, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return wrapper

    def decrypt_record(self, wrapper: dict[str, Any]) -> dict[str, Any]:
        """Verify auth tag + decrypt one record. Raises on tamper / wrong key."""
        nonce = base64.b64decode(wrapper["nonce"])
        ciphertext = base64.b64decode(wrapper["ciphertext"])
        aad_fields = {
            k: wrapper[k] for k in
            ("schema_version", "key_version", "tenant_id", "aid",
             "atv_commitment", "ts_ns")
        }
        aad = json.dumps(aad_fields, sort_keys=True, separators=(",", ":")).encode()
        plaintext = self._aead.decrypt(nonce, ciphertext, aad)
        rec: dict[str, Any] = json.loads(plaintext)
        # Cross-check commitment.
        if hashlib.sha3_256(plaintext).hexdigest() != wrapper["atv_commitment"]:
            raise ValueError("commitment mismatch — record body altered")
        return rec

    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Yield decrypted records in append order, skipping torn / tampered
        lines (logged via the returned dict's ``_decrypt_error`` key)."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    wrapper = json.loads(raw)
                except json.JSONDecodeError:
                    yield {"_decrypt_error": "json_decode", "raw_prefix": raw[:80]}
                    continue
                try:
                    rec = self.decrypt_record(wrapper)
                except Exception as e:  # noqa: BLE001 — fail-soft on tamper
                    yield {
                        "_decrypt_error": type(e).__name__,
                        "atv_commitment": wrapper.get("atv_commitment"),
                        "aid": wrapper.get("aid"),
                    }
                    continue
                yield rec

    def list_wrappers(self) -> list[dict[str, Any]]:
        """Return the cleartext metadata for every line (no decryption).
        Useful for indexing / forensic queries that don't need the body."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    with contextlib.suppress(json.JSONDecodeError):
                        out.append(json.loads(raw))  # skip torn / partial
        return out
