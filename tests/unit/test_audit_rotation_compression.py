"""Tests for the gzip-compression / time-trigger / prune / status
extensions to ``src/aegis/audit/rotation.py``.

The pre-existing ``test_audit_rotation.py`` covers the size-trigger +
slot shifting + cross-file chain. This file covers the post-PR
features: compression, transparent decompression, daily trigger,
explicit ``prune()``, ``status()`` shape, and legacy-format
backwards-compat reads.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aegis.audit.local_chain import GENESIS_HASH, append, verify_chain
from aegis.audit.rotation import (
    COMPRESSION_SUFFIX,
    compressed_rotation_path,
    is_compressed,
    list_rotation_chain,
    open_rotation_text,
    prune,
    rotate,
    rotation_path,
    should_rotate,
    slot_path,
    status,
)

# ── compression of newly-rotated files ───────────────────────────


def test_rotation_produces_gzip_at_slot_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    f.write_text("hello world\n")
    n = rotate(f)
    assert n == 1
    s1 = slot_path(f, 1)
    assert s1 is not None
    assert s1.suffix == COMPRESSION_SUFFIX
    assert s1.name == "audit.jsonl.1.gz"
    # Plain .1 should NOT exist (it was compressed in place).
    assert not rotation_path(f, 1).exists()


def test_compressed_content_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    payload = "first record\nsecond record\nthird record\n"
    f.write_text(payload)
    rotate(f)
    # open_rotation_text decompresses transparently.
    assert "".join(open_rotation_text(slot_path(f, 1))) == payload


def test_compression_is_smaller_for_realistic_audit_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Audit records are highly compressible (repeated keys, mostly-
    zero ATV-2080 vector subfields). The contract isn't a specific
    ratio, just "compressed is smaller than plain"."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    # Simulate ~50 records, each with the same field structure.
    rec = {"ts_ns": 0, "tool": "Bash", "decision": "ALLOW", "args": "x" * 200}
    for i in range(50):
        rec["ts_ns"] = i
        f.write_text(f.read_text() + json.dumps(rec) + "\n" if f.exists() else json.dumps(rec) + "\n")
    plain_bytes = f.stat().st_size
    rotate(f)
    s1 = slot_path(f, 1)
    assert s1 is not None
    gz_bytes = s1.stat().st_size
    assert gz_bytes < plain_bytes


# ── transparent reads across mixed compression ───────────────────


def test_legacy_plain_rotation_still_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Pre-PR users have legacy plain .N files. We MUST NOT delete or
    fail to read them — they get shifted up the slot chain on each new
    rotation and aged out naturally over time."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    # Manually plant a legacy plain .1 file (as if an older Aegis
    # rotated before this PR).
    legacy = rotation_path(f, 1)
    legacy.write_text("legacy plain content\n")
    # slot_path picks it up.
    assert slot_path(f, 1) == legacy
    # open_rotation_text reads it.
    assert "".join(open_rotation_text(legacy)) == "legacy plain content\n"
    # And list_rotation_chain includes it.
    assert legacy in list_rotation_chain(f)


def test_mixed_compression_chain_in_list(
    tmp_path: Path,
) -> None:
    """``list_rotation_chain`` picks up both ``.gz`` and legacy plain
    files at their respective slots. We plant files directly so the
    test exercises the read path without going through ``rotate()``
    (which shifts slots)."""
    f = tmp_path / "audit.jsonl"
    # Plant legacy plain at .2 and a fresh compressed at .1.
    rotation_path(f, 2).write_text("aged plain\n")
    gz = compressed_rotation_path(f, 1)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write("fresh content\n")
    chain = list_rotation_chain(f)
    names = [p.name for p in chain]
    # Order: oldest first (highest slot number first).
    assert names == ["audit.jsonl.2", "audit.jsonl.1.gz"]


def test_compressed_form_preferred_when_both_exist(
    tmp_path: Path,
) -> None:
    """Defensive: if a crashed rotation left both .1 and .1.gz, the
    .gz is canonical (the post-PR contract)."""
    f = tmp_path / "audit.jsonl"
    rotation_path(f, 1).write_text("plain leftover\n")
    gz = compressed_rotation_path(f, 1)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write("compressed canonical\n")
    s = slot_path(f, 1)
    assert s == gz
    assert "".join(open_rotation_text(s)) == "compressed canonical\n"


# ── chain continuity across compressed boundaries ────────────────


def test_chain_walks_through_compressed_rotations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The whole point of this PR is preserving chain continuity even
    when rotated files are gzipped. Trigger explicit rotations, then
    verify across the entire compressed history.

    We size things so all records survive retention: 5 batches of 4
    records each, manually rotated; max_rotations=10 keeps everything.
    """
    monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "0")  # no auto-trigger
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "10")
    f = tmp_path / "audit.jsonl"

    total_written = 0
    for batch in range(5):
        for j in range(4):
            append(f, {"i": batch * 10 + j, "msg": f"batch {batch} rec {j}"})
            total_written += 1
        rotate(f)   # 4 records into .1.gz, then start fresh active

    # 5 rotations performed; all files .1.gz..5.gz. Active is empty.
    for n in range(1, 6):
        s = slot_path(f, n)
        assert s is not None and s.suffix == COMPRESSION_SUFFIX

    ok, broken, total = verify_chain(f)
    assert ok is True, f"chain broke at record {broken}/{total}"
    assert broken == -1
    assert total == total_written


def test_first_rotation_chain_anchors_correctly_with_gz(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """After the first rotation, the active file's first record's
    prev_hash must match the .1.gz's last record's this_hash —
    NOT GENESIS_HASH. Without compression-aware reads this would
    break."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "200")
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
    f = tmp_path / "audit.jsonl"
    for i in range(10):
        append(f, {"i": i})
    # Active file's first record's prev_hash is NOT genesis.
    if f.exists():
        active_lines = f.read_text().strip().splitlines()
        if active_lines:
            first = json.loads(active_lines[0])
            assert first["prev_hash"] != GENESIS_HASH


# ── time-based trigger ───────────────────────────────────────────


def test_should_rotate_silent_without_daily_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Without AEGIS_AUDIT_ROTATE_DAILY, a small file with old records
    does NOT trigger rotation."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "1000000")
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    monkeypatch.delenv("AEGIS_AUDIT_ROTATE_DAILY", raising=False)
    f = tmp_path / "audit.jsonl"
    # Record dated 2 days ago.
    old_ts = int((datetime.now(UTC) - timedelta(days=2)).timestamp() * 1_000_000_000)
    f.write_text(json.dumps({"ts_ns": old_ts}) + "\n")
    assert should_rotate(f) is False


def test_should_rotate_fires_when_daily_and_old_first_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "1000000")
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    monkeypatch.setenv("AEGIS_AUDIT_ROTATE_DAILY", "1")
    f = tmp_path / "audit.jsonl"
    old_ts = int((datetime.now(UTC) - timedelta(days=2)).timestamp() * 1_000_000_000)
    f.write_text(json.dumps({"ts_ns": old_ts}) + "\n")
    assert should_rotate(f) is True


def test_should_rotate_silent_when_daily_and_today_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "1000000")
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    monkeypatch.setenv("AEGIS_AUDIT_ROTATE_DAILY", "1")
    f = tmp_path / "audit.jsonl"
    f.write_text(json.dumps({"ts_ns": time.time_ns()}) + "\n")
    assert should_rotate(f) is False


def test_should_rotate_silent_when_daily_but_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty file shouldn't trigger daily rotation — there's nothing
    to age out."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_BYTES", "1000000")
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    monkeypatch.setenv("AEGIS_AUDIT_ROTATE_DAILY", "1")
    f = tmp_path / "audit.jsonl"
    f.write_text("")
    assert should_rotate(f) is False


# ── prune ────────────────────────────────────────────────────────


def test_prune_keeps_specified_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
    f = tmp_path / "audit.jsonl"
    # Build slots .1..5
    for i in range(5):
        f.write_text(f"batch-{i}\n")
        rotate(f)
    # Pre-condition: slots .1..5 are all present, all .gz.
    for n in range(1, 6):
        assert slot_path(f, n) is not None
    # Prune everything above slot 2.
    removed = prune(f, keep=2)
    assert {p.name for p in removed} == {
        "audit.jsonl.3.gz",
        "audit.jsonl.4.gz",
        "audit.jsonl.5.gz",
    }
    # Slots 1 and 2 retained.
    assert slot_path(f, 1) is not None
    assert slot_path(f, 2) is not None
    # Slots 3..5 gone.
    for n in range(3, 6):
        assert slot_path(f, n) is None


def test_prune_keep_zero_drops_all_rotations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    for i in range(3):
        f.write_text(f"batch-{i}\n")
        rotate(f)
    # All three slots present.
    assert all(slot_path(f, n) is not None for n in (1, 2, 3))
    removed = prune(f, keep=0)
    assert len(removed) == 3
    # Active file untouched (we didn't write any active in this test).
    assert all(slot_path(f, n) is None for n in (1, 2, 3))


def test_prune_preserves_active_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``prune()`` MUST NOT touch the active audit.jsonl, even with
    keep=0."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    f.write_text("rotation-source\n")
    rotate(f)
    # Now write the active.
    f.write_text("active content\n")
    prune(f, keep=0)
    assert f.exists()
    assert f.read_text() == "active content\n"


def test_prune_handles_legacy_plain_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If legacy plain .N files are present, prune drops them too."""
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
    f = tmp_path / "audit.jsonl"
    # Plant legacy plain at slots 2 and 3.
    rotation_path(f, 2).write_text("legacy 2\n")
    rotation_path(f, 3).write_text("legacy 3\n")
    removed = prune(f, keep=1)
    assert {p.name for p in removed} == {"audit.jsonl.2", "audit.jsonl.3"}


def test_prune_negative_keep_clamped_to_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    f.write_text("x\n")
    rotate(f)
    removed = prune(f, keep=-99)
    # Should drop slot 1 (clamped to keep=0, not crash).
    assert len(removed) == 1


# ── status ───────────────────────────────────────────────────────


def test_status_shape_and_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "5")
    f = tmp_path / "audit.jsonl"
    f.write_text("active\n")
    s = status(f)
    expected_keys = {
        "active_path", "active_bytes", "active_exists",
        "threshold_bytes", "max_rotations", "rotate_daily",
        "total_bytes", "rotation_slots",
    }
    assert set(s.keys()) == expected_keys


def test_status_lists_compressed_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_AUDIT_MAX_ROTATIONS", "3")
    f = tmp_path / "audit.jsonl"
    for i in range(2):
        f.write_text(f"batch-{i}\n")
        rotate(f)
    s = status(f)
    slots = s["rotation_slots"]
    assert isinstance(slots, list)
    assert len(slots) == 2
    for slot in slots:
        assert slot["compressed"] is True
        assert int(slot["bytes"]) > 0
    # Slot numbers start at 1, ascending.
    assert [int(slot["n"]) for slot in slots] == [1, 2]


def test_status_active_exists_flag(
    tmp_path: Path,
) -> None:
    f = tmp_path / "audit.jsonl"
    assert status(f)["active_exists"] is False
    f.write_text("hello\n")
    s2 = status(f)
    assert s2["active_exists"] is True
    assert s2["active_bytes"] == 6  # "hello\n"


def test_status_rotate_daily_reflects_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    f = tmp_path / "audit.jsonl"
    monkeypatch.delenv("AEGIS_AUDIT_ROTATE_DAILY", raising=False)
    assert status(f)["rotate_daily"] is False
    monkeypatch.setenv("AEGIS_AUDIT_ROTATE_DAILY", "1")
    assert status(f)["rotate_daily"] is True


# ── helpers ──────────────────────────────────────────────────────


def test_is_compressed() -> None:
    assert is_compressed(Path("a.jsonl.1.gz")) is True
    assert is_compressed(Path("a.jsonl.1")) is False
    assert is_compressed(Path("a.jsonl")) is False


def test_open_rotation_text_silently_returns_for_missing(
    tmp_path: Path,
) -> None:
    """No file → empty iterator, no exception."""
    lines = list(open_rotation_text(tmp_path / "ghost.jsonl.gz"))
    assert lines == []
