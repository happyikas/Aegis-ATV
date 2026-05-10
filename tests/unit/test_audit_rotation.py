"""Unit tests for audit-log rotation + cross-file chain verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.audit.local_chain import GENESIS_HASH, append, verify_chain
from aegis.audit.rotation import (
    list_rotation_chain,
    max_bytes,
    max_rotations,
    maybe_rotate,
    rotate,
    rotation_path,
    should_rotate,
    total_size,
)


# ─────────────────────────────────────────────────────────────────────
# Configuration knobs
# ─────────────────────────────────────────────────────────────────────
class TestConfig:
    def test_default_max_bytes_50mb(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AEGIS_AUDIT_MAX_BYTES", raising=False)
        assert max_bytes() == 50 * 1024 * 1024

    def test_max_bytes_env_override(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "1024")
        assert max_bytes() == 1024

    def test_invalid_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "not-a-number")
        assert max_bytes() == 50 * 1024 * 1024

    def test_zero_disables_rotation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "0")
        f = tmp_path / "audit.jsonl"
        f.write_text("x" * 10_000_000)  # 10 MB
        assert should_rotate(f) is False
        assert maybe_rotate(f) == 0

    def test_max_rotations_default_10(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AEGIS_AUDIT_MAX_ROTATIONS", raising=False)
        assert max_rotations() == 10


# ─────────────────────────────────────────────────────────────────────
# rotation.rotate / list_rotation_chain
# ─────────────────────────────────────────────────────────────────────
class TestRotate:
    def test_no_op_when_file_missing(
        self, tmp_path: Path,
    ) -> None:
        assert rotate(tmp_path / "ghost.jsonl") == 0

    def test_simple_rotate_renames_to_dot_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Post-compression: rotation produces audit.jsonl.1.gz (not the
        legacy plain .1). The active file is gone. Reading goes through
        ``slot_path()`` which transparently picks up the .gz."""
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
        from aegis.audit.rotation import open_rotation_text, slot_path
        f = tmp_path / "audit.jsonl"
        f.write_text("first\n")
        n = rotate(f)
        assert n == 1
        slot1 = slot_path(f, 1)
        assert slot1 is not None
        assert slot1.name == "audit.jsonl.1.gz"
        # Decompresses cleanly back to the original line.
        assert "".join(open_rotation_text(slot1)).strip() == "first"
        assert not f.exists()

    def test_multiple_rotations_shift_correctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
        from aegis.audit.rotation import open_rotation_text, slot_path
        f = tmp_path / "audit.jsonl"
        # Three rotations: each writes a fresh file then rotates.
        for i in range(3):
            f.write_text(f"batch-{i}\n")
            rotate(f)
        # State: .1=batch-2, .2=batch-1, .3=batch-0 — each compressed.
        for n, expected in [(1, "batch-2"), (2, "batch-1"), (3, "batch-0")]:
            p = slot_path(f, n)
            assert p is not None
            assert p.suffix == ".gz"
            assert "".join(open_rotation_text(p)).strip() == expected

    def test_oldest_evicted_at_capacity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "2")
        from aegis.audit.rotation import open_rotation_text, slot_path
        f = tmp_path / "audit.jsonl"
        for i in range(3):
            f.write_text(f"batch-{i}\n")
            rotate(f)
        # Only slots 1 and 2 retained; slot 3 (batch-0) evicted.
        s1 = slot_path(f, 1)
        s2 = slot_path(f, 2)
        s3 = slot_path(f, 3)
        assert s1 is not None and "".join(open_rotation_text(s1)).strip() == "batch-2"
        assert s2 is not None and "".join(open_rotation_text(s2)).strip() == "batch-1"
        assert s3 is None    # neither .3 nor .3.gz

    def test_list_rotation_chain_oldest_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
        f = tmp_path / "audit.jsonl"
        for i in range(3):
            f.write_text(f"r{i}\n")
            rotate(f)
        f.write_text("active\n")
        chain = list_rotation_chain(f)
        # Names: .3.gz, .2.gz, .1.gz, audit.jsonl
        assert [p.name for p in chain] == [
            "audit.jsonl.3.gz", "audit.jsonl.2.gz", "audit.jsonl.1.gz",
            "audit.jsonl",
        ]

    def test_should_rotate_threshold_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "1024")
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
        f = tmp_path / "audit.jsonl"
        f.write_text("x" * 500)
        assert should_rotate(f) is False
        f.write_text("x" * 2000)
        assert should_rotate(f) is True


# ─────────────────────────────────────────────────────────────────────
# Cross-file chain via local_chain.append
# ─────────────────────────────────────────────────────────────────────
class TestCrossFileChain:
    def test_chain_continues_after_rotation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """First record after a rotation must inherit prev_hash from the
        last record of the just-rotated file. Without this, every
        rotation injects a GENESIS_HASH break."""
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "300")
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
        f = tmp_path / "audit.jsonl"
        # Each record is ~100 bytes so we'll get ~3-4 records per file
        # before rotation.
        for i in range(20):
            append(f, {"i": i, "msg": f"hello {i}"})

        # The active file's first record must NOT have prev_hash =
        # GENESIS_HASH (rotation should have inherited).
        active_lines = f.read_text().strip().splitlines()
        if active_lines:
            first_active = json.loads(active_lines[0])
            assert first_active["prev_hash"] != GENESIS_HASH

    def test_verify_chain_walks_all_rotations(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # Threshold + retention sized so all 40 records survive: each
        # rec is ~120 bytes, threshold 5 KB → ~40 rec/file; 10 rotations
        # → 400 record cap. All 40 retained.
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "5000")
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "10")
        f = tmp_path / "audit.jsonl"
        for i in range(40):
            append(f, {"i": i, "msg": f"hello {i}"})

        ok, broken, total = verify_chain(f)
        assert ok is True
        assert broken == -1
        assert total == 40

    def test_verify_chain_anchors_at_first_record_after_eviction(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """When retention has evicted earliest files, verify_chain must
        treat the oldest retained record's prev_hash as a trust anchor
        (not require GENESIS_HASH match)."""
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "300")
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "2")
        f = tmp_path / "audit.jsonl"
        for i in range(30):
            append(f, {"i": i, "msg": f"hello {i}"})
        # We've rotated more than 2 times → oldest retained file's
        # first record's prev_hash != GENESIS_HASH (would be the
        # last hash of the now-evicted file).
        ok, broken, total = verify_chain(f)
        assert ok is True, f"verify failed at record {broken}/{total}"

    def test_verify_chain_detects_tamper_in_rotated_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """A mutated record in a rotated (gzipped) file must be caught
        by the cross-file walker — not just by walking the active file.
        We tamper the .2.gz directly: decompress, mutate one record's
        msg, recompress. The signature in this_hash no longer matches
        the recomputed payload."""
        import gzip

        from aegis.audit.rotation import slot_path
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "300")
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
        f = tmp_path / "audit.jsonl"
        for i in range(40):
            append(f, {"i": i, "msg": f"hello {i}"})

        rot2 = slot_path(f, 2)
        assert rot2 is not None
        assert rot2.suffix == ".gz"

        with gzip.open(rot2, "rt", encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        rec = json.loads(lines[0])
        rec["msg"] = "TAMPERED"
        lines[0] = json.dumps(rec)
        with gzip.open(rot2, "wt", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        ok, broken, _ = verify_chain(f)
        assert ok is False
        # Broken record is somewhere in the rotation chain.
        assert broken >= 0


# ─────────────────────────────────────────────────────────────────────
# total_size
# ─────────────────────────────────────────────────────────────────────
def test_total_size_sums_active_and_rotations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Post-compression: rotated slots are gzipped, so total bytes is
    smaller than the plain-text sum. The contract is that ``total_size``
    sums whatever's actually on disk — including the .gz files. We
    don't assert an exact total because gzip's exact output is
    implementation-specific; we assert ``active`` + ``rotated``
    contributions are both non-zero, and the active file's size shows
    up untouched."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    from aegis.audit.rotation import slot_path
    f = tmp_path / "audit.jsonl"
    f.write_text("a" * 100)
    rotate(f)
    f.write_text("b" * 200)
    rotate(f)
    f.write_text("c" * 300)

    s1 = slot_path(f, 1)
    s2 = slot_path(f, 2)
    assert s1 is not None and s1.suffix == ".gz"
    assert s2 is not None and s2.suffix == ".gz"
    # Active file is plain → exactly 300 bytes contributed.
    # Each rotated file is gzipped → some non-zero bytes.
    rotated_bytes = s1.stat().st_size + s2.stat().st_size
    assert total_size(f) == 300 + rotated_bytes
    assert rotated_bytes > 0


# ─────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────
class TestEdgeCases:
    def test_rotation_path_naming(self, tmp_path: Path) -> None:
        base = tmp_path / "audit.jsonl"
        assert rotation_path(base, 1).name == "audit.jsonl.1"
        assert rotation_path(base, 10).name == "audit.jsonl.10"

    def test_rotate_no_op_when_max_rotations_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "0")
        f = tmp_path / "audit.jsonl"
        f.write_text("data\n")
        assert rotate(f) == 0
        assert f.exists()

    def test_maybe_rotate_no_op_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "10000")
        monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
        f = tmp_path / "audit.jsonl"
        f.write_text("small\n")
        assert maybe_rotate(f) == 0
        assert f.exists()

    def test_append_does_not_break_when_rotation_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Setting MAX_BYTES=0 is the v0-compat opt-out path —
        append must still work and produce a valid chain."""
        monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "0")
        f = tmp_path / "audit.jsonl"
        for i in range(5):
            append(f, {"i": i})
        ok, _, total = verify_chain(f)
        assert ok is True
        assert total == 5
        # No rotations should have happened.
        assert not (tmp_path / "audit.jsonl.1").exists()
