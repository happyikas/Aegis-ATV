"""Tests for v0.7.0 — Claude Code transcript ↔ Aegis audit correlation.

Three layers:

1. Transcript parsing — Task tool_use detection, subagent_type +
   description extraction, tool_use_id → tool_result matching.
2. Audit correlation — verdict tally inside spawn windows + session
   total.
3. Tree rendering + defensive contracts (missing files, malformed
   records).
"""

from __future__ import annotations

import json
from pathlib import Path

from aegis.integrations.claude_code_subagent import (
    build_subagent_graph,
    correlate_audit,
    parse_task_spawns,
    render_tree,
)

# ──────────────────────────────────────────────────────────────────
# Fixtures (inline, so the test file is self-contained)
# ──────────────────────────────────────────────────────────────────


SESSION_A = "sess-A-1234"
SPAWN_UUID_A = "abcd1234"
PARENT_UUID_A = "parent-A"
TOOL_USE_ID_A = "toolu_A_001"


def _mk_assistant_task(
    uuid_: str, parent_uuid: str, session: str,
    tool_use_id: str, subagent_type: str, description: str,
    ts_iso: str,
) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid_,
        "parentUuid": parent_uuid,
        "sessionId": session,
        "timestamp": ts_iso,
        "isSidechain": False,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Task",
                    "input": {
                        "subagent_type": subagent_type,
                        "description": description,
                        "prompt": "go do the thing",
                    },
                }
            ]
        },
    }


def _mk_user_result(tool_use_id: str, ts_iso: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts_iso,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "done",
                }
            ]
        },
    }


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _audit_rec(
    aid: str, ts_ns: int, decision: str, tool: str = "Bash",
) -> dict:
    return {
        "aid": aid,
        "ts_ns": ts_ns,
        "decision": decision,
        "tool": tool,
        "trace_id": f"trace-{ts_ns}",
        "this_hash": f"h-{ts_ns}",
        "prev_hash": "p-prev",
    }


def _write_audit(path: Path, recs: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


# ──────────────────────────────────────────────────────────────────
# 1. Transcript parsing
# ──────────────────────────────────────────────────────────────────


class TestParseTaskSpawns:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert parse_task_spawns(tmp_path / "nope.jsonl") == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert parse_task_spawns(p) == []

    def test_single_task_spawn_no_result(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [
            _mk_assistant_task(
                SPAWN_UUID_A, PARENT_UUID_A, SESSION_A,
                TOOL_USE_ID_A, "Explore", "find the leak",
                "2026-05-17T04:00:00Z",
            ),
        ])
        spawns = parse_task_spawns(p)
        assert len(spawns) == 1
        s = spawns[0]
        assert s.subagent_type == "Explore"
        assert s.description == "find the leak"
        assert s.session_id == SESSION_A
        assert s.tool_use_id == TOOL_USE_ID_A
        assert s.spawn_ts_ns > 0
        assert s.result_ts_ns is None

    def test_task_with_matching_result(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [
            _mk_assistant_task(
                SPAWN_UUID_A, PARENT_UUID_A, SESSION_A,
                TOOL_USE_ID_A, "Plan", "design the thing",
                "2026-05-17T04:00:00Z",
            ),
            _mk_user_result(TOOL_USE_ID_A, "2026-05-17T04:02:30Z"),
        ])
        spawns = parse_task_spawns(p)
        assert len(spawns) == 1
        s = spawns[0]
        assert s.result_ts_ns is not None
        assert s.result_ts_ns > s.spawn_ts_ns

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        p.write_text("not json\n" + json.dumps(_mk_assistant_task(
            "u", "p", "s", "id1", "Explore", "d", "2026-05-17T04:00:00Z",
        )) + "\n{also not json\n")
        spawns = parse_task_spawns(p)
        assert len(spawns) == 1

    def test_non_task_tool_use_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        rec = {
            "type": "assistant",
            "uuid": "u", "parentUuid": "p", "sessionId": "s",
            "timestamp": "2026-05-17T04:00:00Z",
            "message": {"content": [{
                "type": "tool_use", "id": "id1", "name": "Bash",
                "input": {"command": "ls"},
            }]},
        }
        _write_transcript(p, [rec])
        assert parse_task_spawns(p) == []

    def test_multiple_spawns_chronological(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        # Write out of order to verify sort.
        _write_transcript(p, [
            _mk_assistant_task(
                "u2", "p", SESSION_A, "id2", "Explore", "later",
                "2026-05-17T04:05:00Z",
            ),
            _mk_assistant_task(
                "u1", "p", SESSION_A, "id1", "Plan", "earlier",
                "2026-05-17T04:00:00Z",
            ),
        ])
        spawns = parse_task_spawns(p)
        assert [s.description for s in spawns] == ["earlier", "later"]


# ──────────────────────────────────────────────────────────────────
# 2. Audit correlation
# ──────────────────────────────────────────────────────────────────


class TestCorrelateAudit:
    def test_no_spawns_no_audit(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        session_mix, enriched = correlate_audit(
            [], audit, session_id=SESSION_A,
        )
        assert session_mix.total == 0
        assert enriched == []

    def test_session_filter(self, tmp_path: Path) -> None:
        """Audit records with the wrong aid are excluded from the
        session total — verifies the aid filter."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _audit_rec(SESSION_A, 1_000_000_000, "ALLOW"),
            _audit_rec("other-session", 2_000_000_000, "ALLOW"),
            _audit_rec(SESSION_A, 3_000_000_000, "BLOCK"),
        ])
        mix, _ = correlate_audit([], audit, session_id=SESSION_A)
        assert mix.allow == 1
        assert mix.block == 1
        assert mix.approval == 0
        assert mix.total == 2

    def test_window_correlation(self, tmp_path: Path) -> None:
        """Verdicts inside [spawn, result] window land in that spawn's
        bucket; outside are session-total-only."""
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _mk_assistant_task(
                "u", "p", SESSION_A, TOOL_USE_ID_A, "Explore", "test",
                "2026-05-17T04:00:00Z",
            ),
            _mk_user_result(TOOL_USE_ID_A, "2026-05-17T04:00:10Z"),
        ])
        spawns = parse_task_spawns(transcript)
        spawn_ts = spawns[0].spawn_ts_ns
        result_ts = spawns[0].result_ts_ns
        assert result_ts is not None

        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _audit_rec(SESSION_A, spawn_ts - 1_000_000_000, "ALLOW"),  # before
            _audit_rec(SESSION_A, spawn_ts + 1_000_000_000, "ALLOW", "Read"),
            _audit_rec(SESSION_A, spawn_ts + 2_000_000_000, "REQUIRE_APPROVAL", "Bash"),
            _audit_rec(SESSION_A, spawn_ts + 3_000_000_000, "BLOCK", "Bash"),
            _audit_rec(SESSION_A, result_ts + 5_000_000_000, "ALLOW"),  # after
        ])
        mix, enriched = correlate_audit(
            spawns, audit, session_id=SESSION_A,
        )
        assert mix.total == 5
        assert len(enriched) == 1
        v = enriched[0].verdicts
        # The three records inside the window — both >=spawn and <=result.
        assert v.allow + v.approval + v.block == 3
        assert v.allow == 1
        assert v.approval == 1
        assert v.block == 1
        assert v.tool_counts == {"Read": 1, "Bash": 2}

    def test_duration_computed_when_result_present(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _mk_assistant_task(
                "u", "p", SESSION_A, TOOL_USE_ID_A, "Explore", "d",
                "2026-05-17T04:00:00Z",
            ),
            _mk_user_result(TOOL_USE_ID_A, "2026-05-17T04:00:02.5Z"),
        ])
        spawns = parse_task_spawns(transcript)
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        _, enriched = correlate_audit(
            spawns, audit, session_id=SESSION_A,
        )
        assert enriched[0].duration_ms is not None
        assert abs(enriched[0].duration_ms - 2500.0) < 50.0


# ──────────────────────────────────────────────────────────────────
# 3. End-to-end builder + rendering
# ──────────────────────────────────────────────────────────────────


class TestBuildAndRender:
    def test_full_pipeline(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _mk_assistant_task(
                "u", "p", SESSION_A, TOOL_USE_ID_A, "Explore", "first",
                "2026-05-17T04:00:00Z",
            ),
            _mk_user_result(TOOL_USE_ID_A, "2026-05-17T04:00:05Z"),
            _mk_assistant_task(
                "u2", "p2", SESSION_A, "toolu_B", "Plan", "second",
                "2026-05-17T04:00:10Z",
            ),
            _mk_user_result("toolu_B", "2026-05-17T04:00:15Z"),
        ])
        audit = tmp_path / "audit.jsonl"
        spawn_ts_first = parse_task_spawns(transcript)[0].spawn_ts_ns
        _write_audit(audit, [
            _audit_rec(SESSION_A, spawn_ts_first + 500_000_000, "BLOCK", "Bash"),
        ])
        graph = build_subagent_graph(transcript, audit_path=audit)
        assert graph.session_id == SESSION_A
        assert len(graph.spawns) == 2
        # First spawn's window contains the BLOCK.
        assert graph.spawns[0].verdicts.block == 1
        # Second has nothing.
        assert graph.spawns[1].verdicts.total == 0

    def test_render_tree_no_spawns(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("")
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        graph = build_subagent_graph(transcript, audit_path=audit)
        out = render_tree(graph)
        assert "no Task spawns" in out

    def test_render_tree_with_spawns(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _mk_assistant_task(
                "u", "p", SESSION_A, TOOL_USE_ID_A, "Explore", "test",
                "2026-05-17T04:00:00Z",
            ),
        ])
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        graph = build_subagent_graph(transcript, audit_path=audit)
        out = render_tree(graph)
        assert "Explore" in out
        assert "test" in out
        assert "ALLOW" in out  # verdict line always rendered

    def test_missing_audit_file_does_not_raise(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _mk_assistant_task(
                "u", "p", SESSION_A, TOOL_USE_ID_A, "Explore", "x",
                "2026-05-17T04:00:00Z",
            ),
        ])
        # audit file simply absent.
        graph = build_subagent_graph(
            transcript, audit_path=tmp_path / "nonexistent.jsonl",
        )
        assert len(graph.spawns) == 1
        assert graph.spawns[0].verdicts.total == 0
