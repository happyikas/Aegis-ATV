"""Tests for ``aegis.atv.temporal`` — multi-turn ATV → narrative (PR-θ).

The module turns the agent's recent execution into a multi-turn
"video" narrative the sLLM can read. We verify:

* trajectory loads correctly from transcript + audit
* per-turn pairing (FIFO by tool name) works without trace_id
* aggregate signals (backtrack, redundant, errors, failures) are
  counted across the window
* narrative format is stable + readable
* serializer integration: when ``temporal=`` is supplied to
  ``atv_to_prompt``, the action_history "hash-only" gap is no
  longer flagged because the narrative replaces it
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aegis.atv.builder import build_atv
from aegis.atv.serializer import atv_to_prompt
from aegis.atv.temporal import (
    DEFAULT_WINDOW_SIZE,
    ATVSnapshot,
    TemporalContext,
    load_recent_history,
    serialize_temporal,
    temporal_context_to_dict,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _write_transcript(
    path: Path, turns: list[tuple[str, str, int, int, int, int]],
) -> None:
    """Write an assistant-only transcript with the given turn data:
    (tool_name, args_json, input_tokens, output_tokens,
     cache_read, cache_creation).
    """
    with path.open("w") as fh:
        for tool, args, in_t, out_t, cr, cc in turns:
            rec = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "name": tool,
                        "id": f"tu_{tool}",
                        "input": json.loads(args) if args else {},
                    }],
                    "usage": {
                        "input_tokens": in_t,
                        "output_tokens": out_t,
                        "cache_read_input_tokens": cr,
                        "cache_creation_input_tokens": cc,
                    },
                },
            }
            fh.write(json.dumps(rec) + "\n")


def _write_audit(
    path: Path,
    *,
    session_id: str,
    decisions: list[tuple[str, str]],   # (tool, decision)
    posts: list[tuple[str, str, dict[str, Any]]],
                                        # (tool, status, post_analysis)
) -> None:
    with path.open("w") as fh:
        ts = 1_000_000_000
        for tool, decision in decisions:
            fh.write(json.dumps({
                "ts_ns": ts, "aid": session_id, "tool": tool,
                "decision": decision, "reason": "test",
            }) + "\n")
            ts += 1
        for tool, status, pa in posts:
            fh.write(json.dumps({
                "ts_ns": ts, "aid": session_id, "tool": tool,
                "hook": "PostToolUse", "status": status,
                "explain": {"post_analysis": pa},
            }) + "\n")
            ts += 1


# ──────────────────────────────────────────────────────────────────────
# load_recent_history — basic
# ──────────────────────────────────────────────────────────────────────


class TestLoadHistory:
    def test_empty_paths_yields_empty_context(
        self, tmp_path: Path,
    ) -> None:
        ctx = load_recent_history(
            transcript_path=tmp_path / "missing.jsonl",
            audit_path=tmp_path / "missing-audit.jsonl",
            session_id="x",
        )
        assert isinstance(ctx, TemporalContext)
        assert len(ctx.history) == 0
        assert ctx.window_size == DEFAULT_WINDOW_SIZE

    def test_transcript_only_yields_unknown_decisions(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", '{"file_path": "x"}', 100, 50, 0, 0),
            ("Edit", '{}', 200, 100, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts,
            audit_path=None,
            session_id="x",
        )
        assert len(ctx.history) == 2
        for s in ctx.history:
            assert s.decision == "unknown"
            assert s.outcome == "unknown"
            assert s.backtrack is False

    def test_window_size_truncates_to_last_n(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 0, 0),
            ("Read", "{}", 100, 50, 0, 0),
            ("Read", "{}", 100, 50, 0, 0),
            ("Read", "{}", 100, 50, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x", window_size=2,
        )
        assert len(ctx.history) == 2
        # turn_index_rel = -1, 0
        rels = [s.turn_index_rel for s in ctx.history]
        assert rels == [-1, 0]


# ──────────────────────────────────────────────────────────────────────
# Audit pairing
# ──────────────────────────────────────────────────────────────────────


class TestPairing:
    def test_audit_signals_paired_to_transcript_turns(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        au = tmp_path / "audit.jsonl"
        _write_transcript(ts, [
            ("Edit", '{"file_path": "a"}', 100, 50, 0, 0),
            ("Edit", '{"file_path": "a"}', 100, 50, 0, 0),
            ("Bash", '{"command": "pytest"}', 200, 100, 0, 0),
        ])
        _write_audit(
            au, session_id="x",
            decisions=[
                ("Edit", "ALLOW"),
                ("Edit", "ALLOW"),
                ("Bash", "ALLOW"),
            ],
            posts=[
                ("Edit", "success", {
                    "classification": {"is_error": False},
                }),
                ("Edit", "success", {
                    "backtrack": {"file_path": "a"},
                    "classification": {"is_error": False},
                }),
                ("Bash", "failure", {
                    "classification": {"is_error": True},
                }),
            ],
        )

        ctx = load_recent_history(
            transcript_path=ts, audit_path=au,
            session_id="x", window_size=5,
        )
        assert len(ctx.history) == 3

        # turn -1 (the second Edit) was the backtrack
        assert ctx.history[1].backtrack is True
        assert ctx.history[0].backtrack is False
        # last turn (Bash) was an error + failure
        assert ctx.history[-1].is_error is True
        assert ctx.history[-1].outcome == "failure"
        # All decisions ALLOW
        for s in ctx.history:
            assert s.decision == "ALLOW"

    def test_pairing_fifo_by_tool_name(self, tmp_path: Path) -> None:
        # Three Read calls in transcript, three Read PostToolUse
        # in audit. They should pair in order.
        ts = tmp_path / "transcript.jsonl"
        au = tmp_path / "audit.jsonl"
        _write_transcript(ts, [
            ("Read", '{"file_path": "1"}', 100, 50, 0, 0),
            ("Read", '{"file_path": "2"}', 100, 50, 0, 0),
            ("Read", '{"file_path": "3"}', 100, 50, 0, 0),
        ])
        _write_audit(
            au, session_id="x",
            decisions=[("Read", "ALLOW")] * 3,
            posts=[
                ("Read", "success", {}),
                ("Read", "failure", {}),
                ("Read", "success", {}),
            ],
        )
        ctx = load_recent_history(
            transcript_path=ts, audit_path=au,
            session_id="x", window_size=3,
        )
        outcomes = [s.outcome for s in ctx.history]
        assert outcomes == ["success", "failure", "success"]


# ──────────────────────────────────────────────────────────────────────
# Trajectory metrics
# ──────────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_cumulative_token_trajectory_monotonic(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 0, 0),
            ("Read", "{}", 200, 100, 0, 0),
            ("Read", "{}", 300, 150, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        # Each cumulative >= previous.
        for i in range(1, len(ctx.cumulative_token_trajectory)):
            assert (
                ctx.cumulative_token_trajectory[i]
                >= ctx.cumulative_token_trajectory[i - 1]
            )

    def test_cache_hit_rate_drop_detected(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        # Turn -1: cache hit 90% (cache_read=900, total_in=1000)
        # Turn  0: cache hit 10% (cache_read=100, total_in=1000)
        # Drop = 80 pp
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 900, 0),
            ("Read", "{}", 900, 50, 100, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        assert ctx.cache_hit_rate_max_drop_pp >= 70.0

    def test_aggregate_counts_match_signals(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        au = tmp_path / "audit.jsonl"
        _write_transcript(ts, [
            ("Edit", "{}", 100, 50, 0, 0),
            ("Edit", "{}", 100, 50, 0, 0),
            ("Bash", "{}", 100, 50, 0, 0),
        ])
        _write_audit(
            au, session_id="x",
            decisions=[("Edit", "ALLOW")] * 2 + [("Bash", "ALLOW")],
            posts=[
                ("Edit", "success", {"backtrack": {"file_path": "x"}}),
                ("Edit", "success", {"backtrack": {"file_path": "x"}}),
                ("Bash", "failure", {
                    "classification": {"is_error": True},
                    "redundant_of": "earlier-trace",
                }),
            ],
        )
        ctx = load_recent_history(
            transcript_path=ts, audit_path=au,
            session_id="x",
        )
        assert ctx.n_backtracks == 2
        assert ctx.n_redundant == 1
        assert ctx.n_errors == 1
        assert ctx.n_failures == 1


# ──────────────────────────────────────────────────────────────────────
# Narrative renderer
# ──────────────────────────────────────────────────────────────────────


class TestNarrative:
    def test_empty_window_renders_safely(self) -> None:
        ctx = TemporalContext(
            history=(),
            window_size=5,
            cumulative_token_trajectory=(),
            cache_hit_rate_trajectory=(),
            n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
            cache_hit_rate_max_drop_pp=0.0,
            token_velocity_per_turn=0.0,
            is_progress_stalled=False,
            distinct_tools_in_window=(),
        )
        text = serialize_temporal(ctx)
        assert "TEMPORAL TRAJECTORY" in text
        assert "empty" in text.lower()

    def test_per_turn_lines_use_relative_indices(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 0, 0),
            ("Read", "{}", 100, 50, 0, 0),
            ("Read", "{}", 100, 50, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        text = serialize_temporal(ctx)
        # turn_index_rel = -2, -1, 0 → renders as " -2 ", " -1 ", "  0"
        assert "-2" in text
        assert "-1" in text
        assert "  0  " in text

    def test_backtrack_signal_in_narrative(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        au = tmp_path / "audit.jsonl"
        _write_transcript(ts, [
            ("Edit", "{}", 100, 50, 0, 0),
            ("Edit", "{}", 100, 50, 0, 0),
        ])
        _write_audit(
            au, session_id="x",
            decisions=[("Edit", "ALLOW")] * 2,
            posts=[
                ("Edit", "success", {}),
                ("Edit", "success", {"backtrack": {"file_path": "x"}}),
            ],
        )
        ctx = load_recent_history(
            transcript_path=ts, audit_path=au,
            session_id="x",
        )
        text = serialize_temporal(ctx)
        assert "BACKTRACK" in text

    def test_inefficiency_summary_only_when_signals_present(
        self,
    ) -> None:
        clean = TemporalContext(
            history=(
                ATVSnapshot(
                    turn_index_rel=0, ts_ns=0,
                    tool_name="Read", args_excerpt="",
                    decision="ALLOW", outcome="success",
                ),
            ),
            window_size=1,
            cumulative_token_trajectory=(0,),
            cache_hit_rate_trajectory=(0.0,),
            n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
            cache_hit_rate_max_drop_pp=0.0,
            token_velocity_per_turn=0.0,
            is_progress_stalled=False,
            distinct_tools_in_window=("Read",),
        )
        text = serialize_temporal(clean)
        assert "inefficiency_in_window" not in text


# ──────────────────────────────────────────────────────────────────────
# Serializer integration — atv_to_prompt(temporal=...)
# ──────────────────────────────────────────────────────────────────────


def _mk_input() -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="alice", aid="agent-007", timestamp_ns=1,
        ),
        tool_name="Bash", tool_args_json='{"command": "ls"}',
        plan_text="hello",
        cost_estimate=CostEfficiencyMetrics(),
        novelty={"composite_novelty": 0.0},
    )


class TestSerializerIntegration:
    def test_temporal_section_appears_when_supplied(
        self, tmp_path: Path,
    ) -> None:
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 0, 0),
            ("Edit", "{}", 100, 50, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv, mode="strict", temporal=ctx)
        assert "TEMPORAL TRAJECTORY" in out.text
        # Bands_present records the trajectory window.
        assert "temporal_trajectory" in out.bands_present

    def test_no_temporal_section_when_omitted(self) -> None:
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv, mode="strict")
        assert "TEMPORAL TRAJECTORY" not in out.text

    def test_temporal_supplies_action_history_gap(
        self, tmp_path: Path,
    ) -> None:
        # When temporal is supplied, the strict-mode "action_history
        # hash-only" gap should NOT be flagged — the narrative
        # replaces what action_history fails to carry.
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        atv = build_atv(_mk_input())
        with_temporal = atv_to_prompt(atv, mode="strict", temporal=ctx)
        without = atv_to_prompt(atv, mode="strict")
        # With temporal, the action_history gap message is gone.
        assert not any(
            "action_history" in g and "hash" in g
            for g in with_temporal.gaps
        )
        # Without it, the gap is still there.
        assert any(
            "action_history" in g and "hash" in g
            for g in without.gaps
        )


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


class TestSerialisation:
    def test_to_dict_round_trip(self, tmp_path: Path) -> None:
        ts = tmp_path / "transcript.jsonl"
        _write_transcript(ts, [
            ("Read", "{}", 100, 50, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        d = temporal_context_to_dict(ctx)
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded["window_size"] == ctx.window_size
        assert len(decoded["history"]) == 1


# ──────────────────────────────────────────────────────────────────────
# Privacy
# ──────────────────────────────────────────────────────────────────────


class TestPrivacy:
    def test_args_excerpt_capped(self, tmp_path: Path) -> None:
        from aegis.atv.temporal import ARGS_EXCERPT_MAX_CHARS

        # Synthesise a turn whose tool_use input stringifies to a
        # very long string. The narrative must cap the excerpt.
        ts = tmp_path / "transcript.jsonl"
        long_args = json.dumps({"command": "X" * 500})
        _write_transcript(ts, [
            ("Bash", long_args, 100, 50, 0, 0),
        ])
        ctx = load_recent_history(
            transcript_path=ts, audit_path=None,
            session_id="x",
        )
        snap = ctx.history[0]
        assert len(snap.args_excerpt) <= ARGS_EXCERPT_MAX_CHARS

    def test_window_size_validation(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="window_size"):
            load_recent_history(
                transcript_path=None, audit_path=None,
                session_id="x", window_size=0,
            )
