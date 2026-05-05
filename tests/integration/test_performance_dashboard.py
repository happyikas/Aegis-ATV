"""Tests for ``aegis.performance.dashboard`` — audit-chain
aggregator that powers `aegis status --performance`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aegis.performance.dashboard import (
    PerformanceSummary,
    build_performance_summary,
    summary_to_dict,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers — synthetic audit records
# ──────────────────────────────────────────────────────────────────────


def _stop_record(
    *,
    aid: str,
    ts_ns: int,
    cumulative_billed_dollars: float = 0.10,
    cache_hit_rate: float = 0.50,
    input_tokens_total: float = 1000.0,
    output_tokens_total: float = 500.0,
    cache_read_tokens_total: float = 1000.0,
    cache_creation_tokens_total: float = 100.0,
    n_tool_success: int = 8,
    n_tool_failure: int = 1,
    n_backtracks: int = 0,
    n_redundant: int = 0,
    n_is_error: int = 0,
) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns,
        "aid": aid,
        "tool": "(stop)",
        "hook": "Stop",
        "explain": {
            "session_retrospective": {
                "aid": aid,
                "cumulative_billed_dollars": cumulative_billed_dollars,
                "cache_hit_rate": cache_hit_rate,
                "input_tokens_total": input_tokens_total,
                "output_tokens_total": output_tokens_total,
                "cache_read_tokens_total": cache_read_tokens_total,
                "cache_creation_tokens_total": cache_creation_tokens_total,
                "n_tool_success": n_tool_success,
                "n_tool_failure": n_tool_failure,
                "n_backtracks": n_backtracks,
                "n_redundant": n_redundant,
                "n_is_error": n_is_error,
            }
        },
    }


def _post_record(
    *,
    aid: str,
    tool: str,
    backtrack: bool = False,
    redundant: bool = False,
    is_error: bool = False,
    ts_ns: int = 0,
) -> dict[str, Any]:
    pa: dict[str, Any] = {
        "classification": {"is_error": is_error},
    }
    if backtrack:
        pa["backtrack"] = {"reverted_trace_id": "x", "file_path": "/foo.py",
                           "matched_string_hash": "abcd"}
    if redundant:
        pa["redundant_of"] = "earlier-trace-id"
    return {
        "ts_ns": ts_ns,
        "aid": aid,
        "tool": tool,
        "hook": "PostToolUse",
        "explain": {"post_analysis": pa},
    }


def _precompact_record(*, aid: str, ts_ns: int = 0) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns,
        "aid": aid,
        "hook": "PreCompact",
        "explain": {"compaction": {"aid": aid, "n_turns_before": 30}},
    }


def _user_retry_record(
    *, aid: str, is_retry: bool = True, ts_ns: int = 0,
) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns,
        "aid": aid,
        "hook": "UserPromptSubmit",
        "explain": {
            "user_retry": {
                "prompt_hash": "deadbeefcafebabe",
                "prompt_size_bytes": 50,
                "is_retry": is_retry,
            }
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ──────────────────────────────────────────────────────────────────────
# build_performance_summary
# ──────────────────────────────────────────────────────────────────────


class TestEmptyAndMissing:
    def test_missing_audit_returns_empty(self, tmp_path: Path) -> None:
        out = build_performance_summary(tmp_path / "absent.jsonl")
        assert isinstance(out, PerformanceSummary)
        assert out.n_records_walked == 0
        assert out.n_sessions == 0
        assert out.cumulative_billed_dollars == 0.0

    def test_empty_audit(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        out = build_performance_summary(path)
        assert out.n_records_walked == 0
        assert out.n_sessions == 0


class TestStopAggregation:
    def test_single_session(self, tmp_path: Path) -> None:
        path = tmp_path / "one.jsonl"
        _write_jsonl(path, [_stop_record(aid="a1", ts_ns=1_000_000_000)])
        out = build_performance_summary(path)
        assert out.n_sessions == 1
        assert out.cumulative_billed_dollars == 0.10
        assert out.total_input_tokens == 1000.0
        assert out.total_cache_read_tokens == 1000.0
        # weighted hit rate: 1000 / (1000 + 1000 + 100)
        assert out.weighted_cache_hit_rate == 1000.0 / 2100.0

    def test_multiple_sessions_sum_correctly(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "many.jsonl"
        _write_jsonl(path, [
            _stop_record(
                aid="a1", ts_ns=1_000_000_000,
                cumulative_billed_dollars=0.10, cache_hit_rate=0.50,
                input_tokens_total=1000, cache_read_tokens_total=1000,
                cache_creation_tokens_total=100,
            ),
            _stop_record(
                aid="a2", ts_ns=2_000_000_000,
                cumulative_billed_dollars=0.20, cache_hit_rate=0.80,
                input_tokens_total=500, cache_read_tokens_total=4000,
                cache_creation_tokens_total=200,
            ),
        ])
        out = build_performance_summary(path)
        assert out.n_sessions == 2
        assert out.cumulative_billed_dollars == pytest.approx(0.30)
        assert out.avg_session_billed_dollars == pytest.approx(0.15)
        # Avg session hit rate is arithmetic mean
        assert out.avg_session_cache_hit_rate == pytest.approx(0.65)
        # Weighted: (1000+4000) / (1500 + 5000 + 300) = 5000 / 6800
        assert out.weighted_cache_hit_rate == pytest.approx(5000 / 6800)

    def test_session_window(self, tmp_path: Path) -> None:
        path = tmp_path / "win.jsonl"
        _write_jsonl(path, [
            _stop_record(aid="a1", ts_ns=1_000_000_000_000_000_000),
            _stop_record(aid="a2", ts_ns=1_500_000_000_000_000_000),
            _stop_record(aid="a3", ts_ns=2_000_000_000_000_000_000),
        ])
        out = build_performance_summary(path)
        assert out.earliest_session_ts_ns == 1_000_000_000_000_000_000
        assert out.latest_session_ts_ns == 2_000_000_000_000_000_000

    def test_inefficiency_totals_propagate(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "ineff.jsonl"
        _write_jsonl(path, [
            _stop_record(
                aid="a1", ts_ns=1_000_000_000,
                n_backtracks=2, n_redundant=3, n_is_error=1,
            ),
            _stop_record(
                aid="a2", ts_ns=2_000_000_000,
                n_backtracks=0, n_redundant=0, n_is_error=0,
            ),
        ])
        out = build_performance_summary(path)
        assert out.n_backtracks == 2
        assert out.n_redundant == 3
        assert out.n_tool_errors == 1
        assert out.sessions_with_inefficiency_signals == 1


class TestSidecarHooks:
    def test_compaction_count(self, tmp_path: Path) -> None:
        path = tmp_path / "comp.jsonl"
        _write_jsonl(path, [
            _stop_record(aid="a1", ts_ns=1),
            _precompact_record(aid="a1"),
            _precompact_record(aid="a1"),
            _precompact_record(aid="a2"),
        ])
        out = build_performance_summary(path)
        assert out.n_compactions == 3

    def test_user_retry_count_only_flags_actual_retries(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "retry.jsonl"
        _write_jsonl(path, [
            _stop_record(aid="a1", ts_ns=1),
            _user_retry_record(aid="a1", is_retry=True),
            _user_retry_record(aid="a1", is_retry=False),
            _user_retry_record(aid="a2", is_retry=True),
        ])
        out = build_performance_summary(path)
        assert out.n_user_retries == 2  # only is_retry=True


class TestPerToolBreakdown:
    def test_top_inefficient_tools_sorted(self, tmp_path: Path) -> None:
        path = tmp_path / "tools.jsonl"
        _write_jsonl(path, [
            _stop_record(aid="a1", ts_ns=1),
            # Edit: 3 backtracks
            _post_record(aid="a1", tool="Edit", backtrack=True),
            _post_record(aid="a1", tool="Edit", backtrack=True),
            _post_record(aid="a1", tool="Edit", backtrack=True),
            # Bash: 1 redundant + 2 errors
            _post_record(aid="a1", tool="Bash", redundant=True),
            _post_record(aid="a1", tool="Bash", is_error=True),
            _post_record(aid="a1", tool="Bash", is_error=True),
            # Read: clean
            _post_record(aid="a1", tool="Read"),
            _post_record(aid="a1", tool="Read"),
        ])
        out = build_performance_summary(path)
        assert len(out.top_inefficient_tools) == 2  # Edit + Bash; Read clean
        # Edit (3 signals) and Bash (3 signals) — order by signal count
        # then any tiebreak. Both have same total here.
        names = {t.tool for t in out.top_inefficient_tools}
        assert names == {"Edit", "Bash"}

    def test_clean_tools_excluded(self, tmp_path: Path) -> None:
        path = tmp_path / "clean.jsonl"
        _write_jsonl(path, [
            _stop_record(aid="a1", ts_ns=1),
            _post_record(aid="a1", tool="Read"),
            _post_record(aid="a1", tool="Read"),
        ])
        out = build_performance_summary(path)
        # Read had no signals → not in top list.
        assert out.top_inefficient_tools == []


class TestSerialisation:
    def test_to_dict_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "ser.jsonl"
        _write_jsonl(path, [
            _stop_record(aid="a1", ts_ns=1),
            _stop_record(aid="a2", ts_ns=2),
            _post_record(aid="a1", tool="Bash", redundant=True),
        ])
        out = build_performance_summary(path)
        d = summary_to_dict(out)
        # Must JSON-serialise.
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded["n_sessions"] == 2
        assert "cumulative_billed_dollars" in decoded
        assert "top_inefficient_tools" in decoded


class TestSurvivesGarbage:
    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        good = json.dumps(_stop_record(aid="a1", ts_ns=1))
        path.write_text(
            "\n"
            "not-json\n"
            f"{good}\n"
            "{partial: bad}\n"
            "\n"
        )
        out = build_performance_summary(path)
        # Only the well-formed record counted.
        assert out.n_sessions == 1
