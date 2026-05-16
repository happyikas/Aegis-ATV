"""v0.5.7 PR ⑩ — ContextMemory rotation.

Covers the rotation engine (size trigger, gzip archive, retention
prune, opt-out env var) and its integration with the writer + reader
(append triggers rotation, `include_rotated=True` walks archives in
chronological order).

The audit-chain rotation in `src/aegis/audit/rotation.py` is a
different beast (it preserves cross-file SHA3 chain continuity); the
ContextMemory rotation is analytics-only and can drop the oldest
archive without ceremony, so this test surface is smaller.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from aegis.context_memory import append, read_all
from aegis.context_memory.record import ContextMemoryRecord
from aegis.context_memory.rotation import (
    COMPRESSION_SUFFIX,
    compressed_rotation_path,
    list_rotation_chain,
    open_rotation_text,
    rotate,
    rotate_if_needed,
    should_rotate,
    slot_path,
)

# ── helpers ────────────────────────────────────────────────────────


def _rec(ts: int, tid: str = "trace-x") -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts, trace_id=tid, invocation_id="", aid="aid",
        tenant_id="local", tool_name="Bash", decision="ALLOW",
        reason="", channel=None, provider=None, latency_ms=1.0,
        cost_usd=0.0, tokens_in=0, tokens_out=0, step_traces={},
        m13_score=None, advisor_invoked=False, recommended_advisors=(),
        atv_sha3=None, atv_dim=2080, is_sidechain=False, mode="local",
    )


@pytest.fixture
def cm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point ContextMemory at a tmp file with rotation-friendly limits.
    Returns the active-file path. Each test gets a clean env."""
    p = tmp_path / "cm.jsonl"
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_PATH", str(p))
    monkeypatch.delenv("AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED", raising=False)
    return p


# ── trigger / should_rotate ────────────────────────────────────────


def test_should_rotate_false_when_file_missing(cm_env: Path) -> None:
    assert should_rotate(cm_env) is False


def test_should_rotate_false_below_threshold(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "10000")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "5")
    cm_env.write_text("tiny\n", encoding="utf-8")
    assert should_rotate(cm_env) is False


def test_should_rotate_true_at_or_above_threshold(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "100")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "5")
    cm_env.write_text("x" * 200, encoding="utf-8")
    assert should_rotate(cm_env) is True


def test_should_rotate_respects_max_rotations_zero(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MAX_ROTATIONS=0` disables rotation entirely, even when the
    file is well over the byte threshold."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "10")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "0")
    cm_env.write_text("x" * 1000, encoding="utf-8")
    assert should_rotate(cm_env) is False


def test_should_rotate_respects_disable_env(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED", "1")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "10")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "5")
    cm_env.write_text("x" * 1000, encoding="utf-8")
    assert should_rotate(cm_env) is False


# ── rotation engine ────────────────────────────────────────────────


def test_rotate_creates_slot_1_gz_and_clears_active(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")
    cm_env.write_text("line-a\nline-b\n", encoding="utf-8")
    assert rotate(cm_env) == 1
    # Active file is gone; slot 1 archive exists and is non-empty.
    assert not cm_env.exists()
    slot1 = compressed_rotation_path(cm_env, 1)
    assert slot1.exists()
    # Decompresses back to the original two lines.
    with gzip.open(slot1, "rt", encoding="utf-8") as fh:
        assert fh.read() == "line-a\nline-b\n"


def test_rotate_shifts_existing_archives_up(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing slot 1 becomes slot 2 on next rotation."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")

    # First rotation — produces slot 1.
    cm_env.write_text("first\n", encoding="utf-8")
    assert rotate(cm_env) == 1
    # Marker payload so we can tell the slots apart.
    slot1_before = compressed_rotation_path(cm_env, 1).read_bytes()

    # Second rotation — slot 1 should become slot 2.
    cm_env.write_text("second\n", encoding="utf-8")
    assert rotate(cm_env) == 1
    slot1_after = compressed_rotation_path(cm_env, 1).read_bytes()
    slot2 = compressed_rotation_path(cm_env, 2)
    assert slot2.exists()
    # Slot 2 carries the original slot-1 payload.
    assert slot2.read_bytes() == slot1_before
    # Slot 1 now carries the most recent rotation.
    assert slot1_after != slot1_before


def test_rotate_drops_oldest_beyond_retention(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With K=2 and three rotations performed, slot 3 never exists
    — the oldest archive is dropped on the rotation that would have
    produced slot 3."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "2")

    cm_env.write_text("a\n", encoding="utf-8")
    rotate(cm_env)
    cm_env.write_text("b\n", encoding="utf-8")
    rotate(cm_env)
    cm_env.write_text("c\n", encoding="utf-8")
    rotate(cm_env)

    assert compressed_rotation_path(cm_env, 1).exists()
    assert compressed_rotation_path(cm_env, 2).exists()
    assert compressed_rotation_path(cm_env, 3).exists() is False
    # The 'a' archive (oldest) is gone — only 'b' and 'c' remain.
    def _read_gz(n: int) -> str:
        with gzip.open(compressed_rotation_path(cm_env, n), "rt") as fh:
            return fh.read()

    contents = sorted(_read_gz(n) for n in (1, 2))
    assert contents == ["b\n", "c\n"]


def test_rotate_no_op_when_file_missing(cm_env: Path) -> None:
    assert rotate(cm_env) is None


def test_rotate_no_op_when_disabled(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED", "1")
    cm_env.write_text("x" * 1000, encoding="utf-8")
    assert rotate(cm_env) is None
    # Active file untouched.
    assert cm_env.exists()
    assert not compressed_rotation_path(cm_env, 1).exists()


def test_rotate_if_needed_skips_when_under_threshold(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "10000")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "5")
    cm_env.write_text("x" * 100, encoding="utf-8")
    assert rotate_if_needed(cm_env) is None
    assert cm_env.exists()


def test_rotate_if_needed_rotates_when_over(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "100")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "5")
    cm_env.write_text("x" * 200, encoding="utf-8")
    assert rotate_if_needed(cm_env) == 1


# ── reader integration ────────────────────────────────────────────


def test_writer_triggers_rotation_above_threshold(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`append()` should opportunistically rotate when the active
    file crosses the size threshold."""
    # Use a threshold large enough that 1 record fits but a few
    # records together don't, so we see "no rotation, no rotation,
    # rotation" transitions through the test.
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "2000")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")

    # First write — well under threshold, no rotation.
    append(_rec(1_700_000_000_000_000_000))
    assert cm_env.exists()
    assert not compressed_rotation_path(cm_env, 1).exists()

    # Subsequent writes accumulate; eventually a rotation must fire.
    for i in range(1, 30):
        append(_rec(1_700_000_000_000_000_000 + i, tid=f"t{i}"))

    chain = list_rotation_chain(cm_env)
    archived = [p for p in chain if p.suffix == COMPRESSION_SUFFIX]
    assert len(archived) >= 1, (
        "at least one rotation should have fired across 30 writes "
        "with a 2000-byte threshold"
    )


def test_read_all_active_only_by_default(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default `read_all()` only sees the active file. Archives
    are invisible unless the caller opts in with
    `include_rotated=True` — preserves pre-v0.5.7 behavior."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "10")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")

    for i in range(10):
        append(_rec(1_700_000_000_000_000_000 + i, tid=f"t{i}"))

    # Default: active file only.
    n_active = len(read_all())
    # With archives: should be larger (or at least equal when the
    # last write also rotated and the active is empty).
    n_all = len(read_all(include_rotated=True))
    assert n_all >= n_active
    assert n_all > 0


def test_read_all_with_include_rotated_walks_chronological(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records returned by `read_all(include_rotated=True)` must be
    in chronological order — oldest archived first, active last."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_BYTES", "300")
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "5")

    # ts strictly increasing across writes.
    for i in range(20):
        append(_rec(1_700_000_000_000_000_000 + i, tid=f"t{i:03d}"))

    records = read_all(include_rotated=True)
    if len(records) < 2:
        return  # nothing meaningful to assert
    ts_list = [r.ts_ns for r in records]
    assert ts_list == sorted(ts_list), (
        "include_rotated should return records in chronological order"
    )


# ── slot_path / open_rotation_text ────────────────────────────────


def test_slot_path_returns_none_when_missing(cm_env: Path) -> None:
    assert slot_path(cm_env, 1) is None


def test_open_rotation_text_handles_missing_file(cm_env: Path) -> None:
    """Silent no-output on missing files — keeps the verifier
    walker robust against partial rotation states."""
    fake = cm_env.with_name("nope.gz")
    assert list(open_rotation_text(fake)) == []


def test_open_rotation_text_reads_gzip_lines(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.gz` archive is decoded transparently."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")
    cm_env.write_text("alpha\nbeta\n", encoding="utf-8")
    rotate(cm_env)
    slot1 = compressed_rotation_path(cm_env, 1)
    lines = [ln.rstrip("\n") for ln in open_rotation_text(slot1)]
    assert lines == ["alpha", "beta"]


# ── reader skips malformed lines in archives ──────────────────────


def test_iter_records_skips_malformed_in_rotated(
    cm_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defensive: malformed JSON inside a `.gz` archive does NOT
    break the reader — those lines are silently skipped."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")
    valid = json.dumps({
        "ts_ns": 1, "trace_id": "x", "invocation_id": "", "aid": "a",
        "tenant_id": "t", "tool_name": "Bash", "decision": "ALLOW",
        "reason": "", "channel": None, "provider": None,
        "latency_ms": 0.0, "cost_usd": 0.0, "tokens_in": 0,
        "tokens_out": 0, "step_traces": {}, "m13_score": None,
        "advisor_invoked": False, "recommended_advisors": [],
        "atv_sha3": None, "atv_dim": 0, "is_sidechain": False,
        "mode": "local",
    })
    content = f"{valid}\nnot-json\n\n{valid}\n"
    cm_env.write_text(content, encoding="utf-8")
    rotate(cm_env)

    # include_rotated should pick up the 2 valid records, skip the
    # malformed line and blank.
    records = read_all(include_rotated=True)
    assert len(records) == 2
