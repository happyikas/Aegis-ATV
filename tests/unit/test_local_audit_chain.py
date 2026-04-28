"""Unit tests for src/aegis/audit/local_chain.py (v2.1.5, Day-1 #8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.audit.local_chain import (
    GENESIS_HASH,
    _hash_record,
    _last_hash,
    append,
    verify_chain,
)


def test_genesis_hash_is_64_zeros() -> None:
    assert GENESIS_HASH == "0" * 64


def test_last_hash_of_empty_file_is_genesis(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    assert _last_hash(audit) == GENESIS_HASH


def test_append_first_line_uses_genesis_prev(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    rec = append(audit, {"tool": "Bash", "decision": "ALLOW"})
    assert rec["prev_hash"] == GENESIS_HASH
    assert "this_hash" in rec
    assert len(rec["this_hash"]) == 64


def test_append_chains_consecutive_lines(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    a = append(audit, {"tool": "Read", "decision": "ALLOW"})
    b = append(audit, {"tool": "Bash", "decision": "BLOCK"})
    assert b["prev_hash"] == a["this_hash"]


def test_verify_chain_intact(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    for i in range(5):
        append(audit, {"i": i, "decision": "ALLOW"})
    ok, broken, total = verify_chain(audit)
    assert ok is True
    assert broken == -1
    assert total == 5


def test_verify_chain_detects_mutation_in_middle(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    for i in range(4):
        append(audit, {"i": i, "decision": "ALLOW"})
    # Tamper line index 2 in place — keep its hash fields but swap a value.
    lines = audit.read_text().splitlines()
    rec2 = json.loads(lines[2])
    rec2["decision"] = "BLOCK"  # mutated
    lines[2] = json.dumps(rec2)
    audit.write_text("\n".join(lines) + "\n")

    ok, broken, _ = verify_chain(audit)
    assert ok is False
    # The tampered line itself fails the recompute.
    assert broken == 2


def test_verify_chain_detects_reorder(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    for i in range(4):
        append(audit, {"i": i, "decision": "ALLOW"})
    lines = audit.read_text().splitlines()
    # swap lines 1 and 2
    lines[1], lines[2] = lines[2], lines[1]
    audit.write_text("\n".join(lines) + "\n")

    ok, broken, _ = verify_chain(audit)
    assert ok is False
    # Line 1 (now formerly line 2) has prev_hash pointing at line 2's
    # hash, but expected is line 0's. Break detected at index 1.
    assert broken == 1


def test_verify_chain_handles_blank_lines(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    append(audit, {"i": 0})
    audit.write_text(audit.read_text() + "\n\n")
    append(audit, {"i": 1})
    ok, broken, total = verify_chain(audit)
    assert ok is True
    assert total == 2


def test_verify_chain_detects_malformed(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    append(audit, {"i": 0})
    audit.write_text(audit.read_text() + "not json {{\n")
    ok, broken, _ = verify_chain(audit)
    assert ok is False
    assert broken == 1


def test_verify_chain_missing_file_is_ok(tmp_path: Path) -> None:
    """A non-existent path is trivially-intact (empty chain) — return ok."""
    ok, broken, total = verify_chain(tmp_path / "absent.jsonl")
    assert ok is True
    assert broken == -1
    assert total == 0


def test_hash_record_excludes_self_hash() -> None:
    """The hash recompute must NOT include any pre-existing this_hash."""
    base = {"a": 1, "b": 2}
    h_clean = _hash_record(GENESIS_HASH, base)
    h_with_self = _hash_record(GENESIS_HASH, {**base, "this_hash": "BOGUS"})
    assert h_clean == h_with_self


def test_canonical_json_dict_order_independent(tmp_path: Path) -> None:
    """Same logical record produces the same hash regardless of key order."""
    audit = tmp_path / "audit.jsonl"
    rec_a = append(audit, {"a": 1, "b": 2})
    audit.unlink()
    rec_b = append(audit, {"b": 2, "a": 1})
    assert rec_a["this_hash"] == rec_b["this_hash"]


def test_chain_survives_unicode_and_complex_values(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    append(audit, {"reason": "한글 reason — emoji 🛡️", "tags": ["a", "b"]})
    append(audit, {"reason": "second"})
    ok, _, total = verify_chain(audit)
    assert ok is True
    assert total == 2


def test_append_creates_parent_dir(tmp_path: Path) -> None:
    audit = tmp_path / "deep" / "nested" / "audit.jsonl"
    append(audit, {"i": 0})
    assert audit.exists()


# ---- aegis_cli.cmd_verify_audit integration -----------------------------


def test_cmd_verify_audit_local_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # noqa: I001
    import argparse

    audit = tmp_path / "audit.jsonl"
    append(audit, {"decision": "ALLOW"})
    append(audit, {"decision": "BLOCK"})

    rc = aegis_cli.cmd_verify_audit(argparse.Namespace(audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 records intact" in out


def test_cmd_verify_audit_local_detects_break(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # noqa: I001
    import argparse

    audit = tmp_path / "audit.jsonl"
    append(audit, {"decision": "ALLOW"})
    append(audit, {"decision": "BLOCK"})
    # tamper line 0
    lines = audit.read_text().splitlines()
    rec0 = json.loads(lines[0])
    rec0["decision"] = "TAMPERED"
    lines[0] = json.dumps(rec0)
    audit.write_text("\n".join(lines) + "\n")

    rc = aegis_cli.cmd_verify_audit(argparse.Namespace(audit=str(audit)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "broken at record #0" in out


def test_cmd_verify_audit_no_log_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # noqa: I001
    import argparse

    rc = aegis_cli.cmd_verify_audit(
        argparse.Namespace(audit=str(tmp_path / "absent.jsonl"))
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "no local audit log" in out
    assert "/forensic/replay" in out  # sidecar pointer
