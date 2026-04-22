"""Unit tests for encrypted journal + replay engine (M15)."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from aegis.audit.encrypted_journal import (
    KEY_LEN,
    EncryptedJournal,
    load_or_create_data_key,
)
from aegis.audit.replay import replay


def _journal(tmp_path: Path, key: bytes | None = None) -> EncryptedJournal:
    return EncryptedJournal(
        path=tmp_path / "j.jsonl",
        data_key=key or secrets.token_bytes(KEY_LEN),
    )


def _record(aid: str, prev: str = "GENESIS", commitment: str | None = None) -> dict:
    return {
        "atv_id": "atv-1",
        "decision": "ALLOW",
        "this_hash": commitment or ("a" * 64),
        "signature": "x" * 128,
        "payload": {
            "atv_sha3_256": "f" * 64,
            "header": {
                "aid": aid, "tenant_id": "demo-tenant",
                "trace_id": "t", "span_id": "s",
            },
            "prev_hash": prev,
            "tool_name": "read_file",
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Data key
# ─────────────────────────────────────────────────────────────────────
class TestDataKey:
    def test_creates_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "k.bin"
        assert not p.exists()
        k = load_or_create_data_key(p)
        assert len(k) == KEY_LEN
        assert p.exists()

    def test_reuses_when_present(self, tmp_path: Path) -> None:
        p = tmp_path / "k.bin"
        a = load_or_create_data_key(p)
        b = load_or_create_data_key(p)
        assert a == b

    def test_rejects_wrong_length(self, tmp_path: Path) -> None:
        p = tmp_path / "k.bin"
        p.write_bytes(b"too short")
        with pytest.raises(ValueError):
            load_or_create_data_key(p)


# ─────────────────────────────────────────────────────────────────────
# Append + decrypt round-trip
# ─────────────────────────────────────────────────────────────────────
class TestAppendDecrypt:
    def test_round_trip(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        wrapper = j.append(_record("a"))
        # Decrypt the same wrapper.
        decrypted = j.decrypt_record(wrapper)
        assert decrypted["payload"]["header"]["aid"] == "a"

    def test_iter_records_yields_decrypted(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        for i in range(5):
            j.append(_record("a", commitment=str(i) * 64))
        recs = list(j.iter_records())
        assert len(recs) == 5
        assert all("_decrypt_error" not in r for r in recs)

    def test_wrong_key_fails_decrypt(self, tmp_path: Path) -> None:
        k1 = secrets.token_bytes(KEY_LEN)
        k2 = secrets.token_bytes(KEY_LEN)
        j1 = EncryptedJournal(path=tmp_path / "j.jsonl", data_key=k1)
        j2 = EncryptedJournal(path=tmp_path / "j.jsonl", data_key=k2)
        j1.append(_record("a"))
        recs = list(j2.iter_records())
        assert len(recs) == 1
        # Auth-tag check fails → InvalidTag → recorded as _decrypt_error.
        assert "_decrypt_error" in recs[0]

    def test_torn_line_skipped(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.append(_record("a"))
        # Append a torn / non-JSON line.
        with (tmp_path / "j.jsonl").open("a", encoding="utf-8") as f:
            f.write("not-valid-json\n")
        j.append(_record("b"))
        recs = list(j.iter_records())
        # Two valid records + one tamper marker.
        assert len(recs) == 3
        valid = [r for r in recs if "_decrypt_error" not in r]
        assert len(valid) == 2

    def test_tampered_ciphertext_caught(self, tmp_path: Path) -> None:
        import json
        j = _journal(tmp_path)
        j.append(_record("a"))
        # Read + tamper the line.
        path = tmp_path / "j.jsonl"
        wrapper = json.loads(path.read_text().strip())
        # Flip a bit in the ciphertext base64.
        ct = bytearray(wrapper["ciphertext"], "utf-8")
        ct[0] = (ct[0] ^ 1)
        wrapper["ciphertext"] = ct.decode("utf-8", errors="ignore")
        path.write_text(json.dumps(wrapper) + "\n")
        recs = list(j.iter_records())
        assert len(recs) == 1
        assert "_decrypt_error" in recs[0]


# ─────────────────────────────────────────────────────────────────────
# Replay
# ─────────────────────────────────────────────────────────────────────
class TestReplay:
    def test_clean_chain_passes(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        prev = "GENESIS"
        for i in range(4):
            this_hash = f"hash-{i:02d}-" + "0" * 50
            j.append(_record("a", prev=prev, commitment=this_hash))
            prev = this_hash
        rep = replay(j)
        assert rep.decrypted_count == 4
        assert rep.tampered_count == 0
        assert rep.per_aid_chain_valid["a"] is True
        assert rep.per_aid_head["a"].startswith("hash-03-")

    def test_chain_break_detected(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.append(_record("a", prev="GENESIS", commitment="h1" + "0" * 62))
        # Skip a link — prev_hash should be h1, but we set "WRONG".
        j.append(_record("a", prev="WRONG", commitment="h2" + "0" * 62))
        rep = replay(j)
        assert rep.per_aid_chain_valid["a"] is False
        assert any("chain_break" in t.get("_decrypt_error", "") for t in rep.tampered_records)

    def test_per_aid_isolation(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.append(_record("a", prev="GENESIS", commitment="A1" + "0" * 62))
        j.append(_record("b", prev="GENESIS", commitment="B1" + "0" * 62))
        rep = replay(j)
        assert rep.per_aid_chain_valid["a"] is True
        assert rep.per_aid_chain_valid["b"] is True
        assert rep.aids_seen == {"a", "b"}
