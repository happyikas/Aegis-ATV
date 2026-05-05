"""Stop-hook session retrospective tests.

Covers:
* analyze_session against synthesized (transcript, audit) pairs
* All 6 efficiency-slot KPIs computed correctly
* All 3 inefficiency ratios derived from PR #45 PostToolUse data
* graceful behaviour when transcript or audit is missing
* End-to-end through session_end.py — Stop record lands in audit
* aegis verify-audit walks the new Stop record without breaking
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from aegis.cost.retrospective import (
    SessionRetrospective,
    analyze_session,
    format_brief,
    to_audit_record,
)

# ─────────────────────────────────────────────────────────────────────
# Synth helpers — Claude Code transcript shape (PR #36 nested)
# ─────────────────────────────────────────────────────────────────────


def _line(d: dict[str, Any]) -> str:
    return json.dumps(d) + "\n"


def _user(text: str) -> str:
    return _line({"type": "user", "content": text})


def _assistant(
    *,
    in_tokens: int = 0,
    out_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    text: str = "ok",
    tool_uses: list[dict[str, Any]] | None = None,
) -> str:
    """Synthesize one assistant turn with usage + optional tool_use."""
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for tu in tool_uses or []:
        content.append({"type": "tool_use", **tu})
    rec = {
        "type": "assistant",
        "message": {
            "role": "assistant", "model": "claude-sonnet-4-6",
            "content": content,
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }
    return _line(rec)


def _write_audit(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _post_record(
    *, ts_ns: int, aid: str, tool: str = "Bash",
    status: str = "success", is_error: bool = False,
    backtrack: bool = False, redundant_of: str | None = None,
) -> dict[str, Any]:
    """Synthesize one PostToolUse audit record carrying the
    PR #45 post_analysis block."""
    pa: dict[str, Any] = {
        "classification": {"is_error": is_error, "size_bytes": 100,
                            "line_count": 2},
        "args_hash": "deadbeef" + "0" * 8,
    }
    if backtrack:
        pa["backtrack"] = {
            "reverted_trace_id": "tr-prior",
            "file_path": "/x.py",
            "matched_string_hash": "abc",
        }
    if redundant_of is not None:
        pa["redundant_of"] = redundant_of
    return {
        "ts_ns": ts_ns, "tool": tool, "aid": aid,
        "hook": "PostToolUse", "status": status,
        "trace_id": f"trace-{ts_ns}",
        "explain": {"post_analysis": pa},
    }


def _pre_record(
    *, ts_ns: int, aid: str, tool: str = "Bash",
    decision: str = "ALLOW",
) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns, "tool": tool, "aid": aid,
        "decision": decision, "trace_id": f"pre-{ts_ns}",
        "explain": {"step_traces": {}},
    }


# ─────────────────────────────────────────────────────────────────────
# 1. Token aggregation + cache_hit_rate
# ─────────────────────────────────────────────────────────────────────


class TestTokenAggregation:
    def test_high_cache_hit_session(self, tmp_path: Path) -> None:
        """90 % cache_read → cache_hit_rate ≈ 0.90."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("task")
            + _assistant(in_tokens=100, out_tokens=50, cache_read=900,
                         cache_creation=0)
            + _user("x" * 200),
            encoding="utf-8",
        )
        retro = analyze_session(
            transcript_path=transcript,
            audit_path=None,
            session_id="s",
            model_for_cost="claude-sonnet-4-6",
        )
        # cache_hit_rate = 900 / (100 + 900 + 0) = 0.90
        assert retro.cache_hit_rate == pytest.approx(0.90, abs=0.01)
        assert retro.cache_read_tokens_total == 900
        assert retro.input_tokens_total == 100

    def test_low_cache_hit_session(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("task")
            + _assistant(in_tokens=900, out_tokens=100, cache_read=100,
                         cache_creation=0)
            + _user("x" * 200),
            encoding="utf-8",
        )
        retro = analyze_session(
            transcript_path=transcript, audit_path=None,
            session_id="s", model_for_cost="claude-sonnet-4-6",
        )
        # 100 / (900 + 100) = 0.10
        assert retro.cache_hit_rate == pytest.approx(0.10, abs=0.01)


# ─────────────────────────────────────────────────────────────────────
# 2. reasoning:action ratio
# ─────────────────────────────────────────────────────────────────────


class TestReasoningRatio:
    def test_heavy_thinking_high_ratio(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("plan carefully")
            + _assistant(in_tokens=100, out_tokens=200,
                         reasoning_tokens=2000)   # 10× more reasoning than output
            + _user("x" * 200),
            encoding="utf-8",
        )
        retro = analyze_session(
            transcript_path=transcript, audit_path=None,
            session_id="s",
        )
        assert retro.reasoning_to_action_ratio == pytest.approx(10.0, abs=0.01)

    def test_no_reasoning_zero_ratio(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("just do it")
            + _assistant(in_tokens=100, out_tokens=200, reasoning_tokens=0)
            + _user("x" * 200),
            encoding="utf-8",
        )
        retro = analyze_session(
            transcript_path=transcript, audit_path=None,
            session_id="s",
        )
        assert retro.reasoning_to_action_ratio == 0.0


# ─────────────────────────────────────────────────────────────────────
# 3. Inefficiency ratios from audit walk
# ─────────────────────────────────────────────────────────────────────


class TestInefficiencyRatios:
    def test_backtrack_ratio(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        # 4 Edit calls, 1 backtrack → 0.25
        _write_audit(audit, [
            _post_record(ts_ns=1, aid="s", tool="Edit"),
            _post_record(ts_ns=2, aid="s", tool="Edit"),
            _post_record(ts_ns=3, aid="s", tool="Edit", backtrack=True),
            _post_record(ts_ns=4, aid="s", tool="Edit"),
            _post_record(ts_ns=5, aid="s", tool="Bash"),  # not counted
        ])
        retro = analyze_session(
            transcript_path=None, audit_path=audit, session_id="s",
        )
        assert retro.n_edit_calls == 4
        assert retro.n_backtracks == 1
        assert retro.backtrack_ratio == 0.25

    def test_redundancy_and_error_rate(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pre_record(ts_ns=1, aid="s"),
            _pre_record(ts_ns=2, aid="s"),
            _pre_record(ts_ns=3, aid="s"),
            _post_record(ts_ns=11, aid="s", redundant_of="tr-x"),
            _post_record(ts_ns=12, aid="s", is_error=True),
            _post_record(ts_ns=13, aid="s"),
        ])
        retro = analyze_session(
            transcript_path=None, audit_path=audit, session_id="s",
        )
        assert retro.n_pretool_records == 3
        assert retro.n_posttool_records == 3
        assert retro.n_redundant == 1
        assert retro.n_is_error == 1
        # redundancy_ratio = 1/3 over PreToolUse records.
        assert retro.redundancy_ratio == pytest.approx(1.0 / 3.0)
        # error_rate = 1 / 3 over PostToolUse records.
        assert retro.error_rate == pytest.approx(1.0 / 3.0)

    def test_session_aid_filter(self, tmp_path: Path) -> None:
        """Records from OTHER sessions must not contaminate this
        session's retrospective."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _post_record(ts_ns=1, aid="my-session", tool="Edit", backtrack=True),
            _post_record(ts_ns=2, aid="other-session", tool="Edit", backtrack=True),
            _post_record(ts_ns=3, aid="my-session", tool="Edit"),
        ])
        retro = analyze_session(
            transcript_path=None, audit_path=audit, session_id="my-session",
        )
        assert retro.n_edit_calls == 2
        assert retro.n_backtracks == 1


# ─────────────────────────────────────────────────────────────────────
# 4. Context utilization
# ─────────────────────────────────────────────────────────────────────


class TestContextUtilization:
    def test_high_context_use(self, tmp_path: Path) -> None:
        # 180k tokens at peak / 200k window = 0.90
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("task")
            + _assistant(in_tokens=10_000, out_tokens=500,
                         cache_read=170_000)  # peak input 180k
            + _user("x" * 200),
            encoding="utf-8",
        )
        retro = analyze_session(
            transcript_path=transcript, audit_path=None,
            session_id="s", model_for_cost="claude-sonnet-4-6",
        )
        # context_utilization = 180k / 200k = 0.90
        assert retro.context_utilization_ratio == pytest.approx(0.90, abs=0.01)

    def test_low_context_use(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("hi")
            + _assistant(in_tokens=100, out_tokens=50)
            + _user("x" * 200),
            encoding="utf-8",
        )
        retro = analyze_session(
            transcript_path=transcript, audit_path=None,
            session_id="s", model_for_cost="claude-sonnet-4-6",
        )
        # 100 / 200_000 = 0.0005
        assert retro.context_utilization_ratio == pytest.approx(0.0005, abs=0.0001)


# ─────────────────────────────────────────────────────────────────────
# 5. Burn rate + duration
# ─────────────────────────────────────────────────────────────────────


class TestBurnRate:
    def test_duration_from_audit_timestamps(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        # Two records 90 seconds apart — use real-ish ts_ns since
        # ts_ns=0 is treated as "missing" (falsy guard in the walker).
        base = int(time.time_ns())
        _write_audit(audit, [
            _post_record(ts_ns=base, aid="s", tool="Bash"),
            _post_record(ts_ns=base + int(1e9 * 90), aid="s", tool="Bash"),
        ])
        retro = analyze_session(
            transcript_path=None, audit_path=audit, session_id="s",
        )
        assert retro.session_duration_seconds == pytest.approx(90.0, abs=0.1)


# ─────────────────────────────────────────────────────────────────────
# 6. Graceful degradation
# ─────────────────────────────────────────────────────────────────────


class TestGraceful:
    def test_missing_transcript_returns_zeros(self, tmp_path: Path) -> None:
        retro = analyze_session(
            transcript_path=tmp_path / "absent.jsonl",
            audit_path=None,
            session_id="s",
        )
        assert retro.input_tokens_total == 0.0
        assert retro.cache_hit_rate == 0.0

    def test_missing_audit_returns_zeros(self, tmp_path: Path) -> None:
        retro = analyze_session(
            transcript_path=None,
            audit_path=tmp_path / "absent.jsonl",
            session_id="s",
        )
        assert retro.n_pretool_records == 0
        assert retro.backtrack_ratio == 0.0

    def test_both_missing(self, tmp_path: Path) -> None:
        retro = analyze_session(
            transcript_path=None, audit_path=None, session_id="s",
        )
        assert retro.cumulative_billed_dollars == 0.0


# ─────────────────────────────────────────────────────────────────────
# 7. to_audit_record + format_brief
# ─────────────────────────────────────────────────────────────────────


class TestSerialization:
    def test_to_audit_record_has_stop_hook_and_explain(self) -> None:
        retro = SessionRetrospective(
            aid="s", session_id="s", model_for_cost="m",
            cache_hit_rate=0.5, n_turns=10,
        )
        rec = to_audit_record(retro)
        assert rec["hook"] == "Stop"
        assert rec["aid"] == "s"
        assert "ts_ns" in rec
        block = rec["explain"]["session_retrospective"]
        assert block["cache_hit_rate"] == 0.5
        assert block["n_turns"] == 10

    def test_format_brief_renders_all_sections(self) -> None:
        retro = SessionRetrospective(
            aid="abcdef", session_id="abcdef", model_for_cost="m",
            n_turns=20, n_user_messages=10, n_assistant_messages=10,
            n_posttool_records=15, n_tool_success=12,
            n_tool_failure=2, n_tool_timeout=1,
            cumulative_billed_dollars=0.1234,
            cache_hit_rate=0.85, reasoning_to_action_ratio=2.3,
            session_duration_seconds=125.5,
        )
        out = format_brief(retro)
        assert "Aegis session retrospective" in out
        assert "duration:" in out
        assert "Efficiency:" in out
        assert "Inefficiency:" in out
        assert "0.85" in out


# ─────────────────────────────────────────────────────────────────────
# 8. End-to-end via session_end.py
# ─────────────────────────────────────────────────────────────────────


def _run_session_end_hook(payload: dict[str, Any]) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import session_end
    out_buf = io.StringIO()
    rc = session_end.handle_session_end(
        io.StringIO(json.dumps(payload)), out_buf,
    )
    return rc, out_buf.getvalue()


@pytest.fixture
def isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import session_end
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(session_end, "LOCAL_AUDIT_PATH", audit)
    return audit


class TestE2EHook:
    def test_stop_hook_appends_retrospective_record(
        self, isolated_audit: Path, tmp_path: Path,
    ) -> None:
        # Synth transcript + audit.
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("debug bug")
            + _assistant(in_tokens=200, out_tokens=100, cache_read=1800)
            + _user("x" * 200),
            encoding="utf-8",
        )
        # Two PostToolUse records belonging to this session.
        _write_audit(isolated_audit, [
            _post_record(ts_ns=int(1e9 * 0), aid="my-session", tool="Bash"),
            _post_record(ts_ns=int(1e9 * 30), aid="my-session", tool="Read"),
        ])

        rc, stdout = _run_session_end_hook({
            "transcript_path": str(transcript),
            "session_id": "my-session",
        })
        assert rc == 0

        # _aegis envelope reports the retrospective summary.
        env = json.loads(stdout)["_aegis"]
        assert env["retrospective"] == "written"
        assert env["n_tool_calls"] == 2

        # The audit chain now ends with a Stop record carrying the
        # session_retrospective block.
        last_line = isolated_audit.read_text().strip().splitlines()[-1]
        last = json.loads(last_line)
        assert last["hook"] == "Stop"
        assert last["aid"] == "my-session"
        block = last["explain"]["session_retrospective"]
        # Cache hit ≈ 0.90, error_rate = 0, n_pretool = 0
        # (we only synthed PostToolUse records).
        assert block["cache_hit_rate"] == pytest.approx(0.90, abs=0.01)
        assert block["n_posttool_records"] == 2
        assert block["n_tool_success"] == 2
        assert block["error_rate"] == 0.0

    def test_stop_record_preserves_audit_chain_integrity(
        self, isolated_audit: Path, tmp_path: Path,
    ) -> None:
        """The new Stop record must chain via SHA3 just like
        Pre/PostToolUse records — `aegis verify-audit` must still pass."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("hi") + _assistant(in_tokens=10, out_tokens=5)
            + _user("x" * 200),
            encoding="utf-8",
        )
        # Pre-seed audit with one Pre + one Post record using local_chain
        # so the chain prev_hash/this_hash gets set up properly.
        from aegis.audit.local_chain import append as chain_append
        from aegis.audit.local_chain import verify_chain
        chain_append(isolated_audit, _pre_record(ts_ns=1, aid="s"))
        chain_append(isolated_audit, _post_record(ts_ns=2, aid="s"))

        _run_session_end_hook({
            "transcript_path": str(transcript),
            "session_id": "s",
        })
        ok, broken_at, total = verify_chain(isolated_audit)
        assert ok is True, f"audit chain broken at {broken_at}"
        assert total == 3   # Pre + Post + Stop

    def test_missing_transcript_does_not_crash(
        self, isolated_audit: Path, tmp_path: Path,
    ) -> None:
        rc, stdout = _run_session_end_hook({
            "transcript_path": str(tmp_path / "absent.jsonl"),
            "session_id": "s",
        })
        assert rc == 0
        env = json.loads(stdout)["_aegis"]
        # Retrospective still written (with zero-fill)
        assert env["retrospective"] == "written"
