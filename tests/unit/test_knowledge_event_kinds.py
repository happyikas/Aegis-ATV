"""Tests for v0.5.20 — SESSION / INCIDENT / WORKFLOW entry kinds.

Three event-level entries derived per-aid from the timeline:

* SESSION  — gap-segmented contiguous activity.
* INCIDENT — one BLOCK + setup/recovery window.
* WORKFLOW — recurring tool n-gram (bigram by default).

Tests cover: builder produces the expected entries, the entries
have non-trivial infobox + cross-refs, and edge cases (no BLOCK,
short session, no recurring sequence) don't generate spurious
entries.
"""

from __future__ import annotations

from aegis.context_memory.record import ContextMemoryRecord
from aegis.knowledge import EntryKind, build_knowledge
from aegis.knowledge.builder import (
    INCIDENT_BEFORE,
    SESSION_GAP_SECONDS,
    SESSION_MIN_CALLS,
    _mine_workflows,
    _render_incident_entry,
    _render_session_entry,
    _segment_sessions,
)

# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


def _rec(
    *,
    aid: str = "agent-A",
    tool: str = "Bash",
    decision: str = "ALLOW",
    reason: str = "",
    trace_id: str = "t",
    ts_ns: int = 0,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns,
        trace_id=trace_id,
        invocation_id="inv",
        aid=aid,
        tenant_id="t",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=10.0,
        cost_usd=0.001,
        tokens_in=100,
        tokens_out=50,
        step_traces={},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="sidecar",
    )


# ──────────────────────────────────────────────────────────────────
# Session segmentation
# ──────────────────────────────────────────────────────────────────


class TestSessionSegmentation:
    def test_empty_timeline(self) -> None:
        assert _segment_sessions([]) == []

    def test_single_session_no_gap(self) -> None:
        t = 1_000_000_000_000
        timeline = [
            _rec(trace_id=f"t{i}", ts_ns=t + i * 1_000_000_000)
            for i in range(5)
        ]
        sessions = _segment_sessions(timeline)
        assert len(sessions) == 1
        assert len(sessions[0]) == 5

    def test_splits_on_large_gap(self) -> None:
        t = 1_000_000_000_000
        gap_ns = (SESSION_GAP_SECONDS + 60) * 1_000_000_000
        timeline = [
            _rec(trace_id="a", ts_ns=t),
            _rec(trace_id="b", ts_ns=t + 1_000_000_000),
            _rec(trace_id="c", ts_ns=t + 1_000_000_000 + gap_ns),
            _rec(trace_id="d", ts_ns=t + 2_000_000_000 + gap_ns),
        ]
        sessions = _segment_sessions(timeline)
        assert len(sessions) == 2
        assert len(sessions[0]) == 2
        assert len(sessions[1]) == 2


# ──────────────────────────────────────────────────────────────────
# Session entry rendering
# ──────────────────────────────────────────────────────────────────


class TestSessionEntry:
    def test_short_uneventful_session_skipped(self) -> None:
        timeline = [
            _rec(trace_id=f"t{i}", ts_ns=i * 1_000_000_000)
            for i in range(SESSION_MIN_CALLS - 1)
        ]
        entry = _render_session_entry("agent-A", timeline)
        assert entry is None

    def test_short_session_with_block_kept(self) -> None:
        timeline = [
            _rec(trace_id="a", ts_ns=1_000_000_000),
            _rec(trace_id="b", decision="BLOCK",
                 reason="rule:foo", ts_ns=2_000_000_000),
        ]
        entry = _render_session_entry("agent-A", timeline)
        assert entry is not None
        assert entry.kind == EntryKind.SESSION
        assert "had-block" in entry.tags

    def test_long_session_has_top_tools_section(self) -> None:
        timeline = [
            _rec(
                tool=("Bash" if i % 3 == 0 else "Edit" if i % 3 == 1 else "Read"),
                trace_id=f"t{i}",
                ts_ns=i * 1_000_000_000,
            )
            for i in range(10)
        ]
        entry = _render_session_entry("agent-A", timeline)
        assert entry is not None
        assert entry.kind == EntryKind.SESSION
        assert any("Top tools" in s.heading for s in entry.sections)
        assert "agent/agent-A" in entry.related


# ──────────────────────────────────────────────────────────────────
# Incident entry rendering
# ──────────────────────────────────────────────────────────────────


class TestIncidentEntry:
    def test_non_block_returns_none(self) -> None:
        rec = _rec(trace_id="x", ts_ns=1_000)
        entry = _render_incident_entry("agent-A", rec, [rec])
        assert entry is None

    def test_block_produces_incident(self) -> None:
        timeline = [
            _rec(trace_id="r1", tool="Read", ts_ns=1_000),
            _rec(trace_id="r2", tool="Read", ts_ns=2_000),
            _rec(trace_id="r3", tool="Read", ts_ns=3_000),
            _rec(trace_id="blk", tool="Bash",
                 decision="BLOCK", reason="rule:dangerous",
                 ts_ns=4_000),
            _rec(trace_id="rec1", tool="Bash", ts_ns=5_000),
        ]
        entry = _render_incident_entry("agent-A", timeline[3], timeline)
        assert entry is not None
        assert entry.kind == EntryKind.INCIDENT
        assert entry.infobox.fields["recovered"] is True
        assert "recovered" in entry.tags
        assert "block" in entry.tags
        # Cross-refs: agent + tool + pattern.
        assert "agent/agent-A" in entry.related
        assert "tool/Bash" in entry.related
        assert any(r.startswith("pattern/Bash:") for r in entry.related)

    def test_unrecovered_incident_tagged(self) -> None:
        timeline = [
            _rec(trace_id="blk", decision="BLOCK",
                 reason="rule:foo", ts_ns=1_000),
        ]
        entry = _render_incident_entry("agent-A", timeline[0], timeline)
        assert entry is not None
        assert "unresolved" in entry.tags

    def test_setup_window_size_capped(self) -> None:
        timeline = [
            _rec(trace_id=f"s{i}", tool="Read", ts_ns=i * 1_000)
            for i in range(10)
        ]
        timeline.append(_rec(
            trace_id="blk", tool="Bash",
            decision="BLOCK", reason="rule:foo",
            ts_ns=11_000,
        ))
        entry = _render_incident_entry("agent-A", timeline[-1], timeline)
        assert entry is not None
        assert entry.infobox.fields["n_setup_calls"] == INCIDENT_BEFORE


# ──────────────────────────────────────────────────────────────────
# Workflow mining
# ──────────────────────────────────────────────────────────────────


class TestWorkflowMining:
    def test_empty_timeline(self) -> None:
        assert _mine_workflows("agent-A", []) == []

    def test_below_min_occurrences_skipped(self) -> None:
        # Read -> Edit appears twice; default min is 3.
        timeline = [
            _rec(tool="Read", trace_id="a", ts_ns=1_000),
            _rec(tool="Edit", trace_id="b", ts_ns=2_000),
            _rec(tool="Read", trace_id="c", ts_ns=3_000),
            _rec(tool="Edit", trace_id="d", ts_ns=4_000),
        ]
        flows = _mine_workflows("agent-A", timeline)
        assert flows == []

    def test_recurring_bigram_promoted(self) -> None:
        # Read -> Edit appears 5 times → n_occ * 10 = 50 → conf 1.0.
        timeline = []
        for i in range(5):
            timeline.append(_rec(tool="Read",
                                 trace_id=f"r{i}",
                                 ts_ns=(2 * i) * 1_000))
            timeline.append(_rec(tool="Edit",
                                 trace_id=f"e{i}",
                                 ts_ns=(2 * i + 1) * 1_000))
        flows = _mine_workflows("agent-A", timeline)
        names = [f.entry_id for f in flows]
        assert any("Read-Edit" in n for n in names)
        flow = next(f for f in flows if "Read-Edit" in f.entry_id)
        # 5 occurrences × 10 = 50 → confidence saturates.
        assert flow.confidence == 1.0
        assert "recurring-workflow" in flow.tags

    def test_dominant_workflow_tagged(self) -> None:
        timeline = []
        for i in range(12):
            timeline.append(_rec(tool="A", trace_id=f"a{i}",
                                 ts_ns=(2 * i) * 1_000))
            timeline.append(_rec(tool="B", trace_id=f"b{i}",
                                 ts_ns=(2 * i + 1) * 1_000))
        flows = _mine_workflows("agent-A", timeline)
        ab = next(f for f in flows if f.entry_id.endswith("A-B"))
        assert "dominant-workflow" in ab.tags


# ──────────────────────────────────────────────────────────────────
# End-to-end build_knowledge integration
# ──────────────────────────────────────────────────────────────────


class TestBuildKnowledgeWithEventKinds:
    def test_all_six_kinds_produced(self) -> None:
        t = 1_000_000_000_000
        recs: list[ContextMemoryRecord] = []
        # Long sessions of Read->Edit recurring (drives WORKFLOW).
        for i in range(6):
            recs.append(_rec(tool="Read", trace_id=f"r{i}",
                             ts_ns=t + (2 * i) * 1_000_000_000))
            recs.append(_rec(tool="Edit", trace_id=f"e{i}",
                             ts_ns=t + (2 * i + 1) * 1_000_000_000))
        # A REQUIRE_APPROVAL pattern (drives PATTERN).
        recs.append(_rec(
            decision="REQUIRE_APPROVAL",
            reason="same Bash call repeated 3 times this session",
            trace_id="ra",
            ts_ns=t + 100 * 1_000_000_000,
        ))
        # A BLOCK (drives INCIDENT).
        recs.append(_rec(
            decision="BLOCK",
            reason="rule:foo",
            trace_id="blk",
            ts_ns=t + 110 * 1_000_000_000,
        ))
        recs.append(_rec(trace_id="rec", ts_ns=t + 111 * 1_000_000_000))

        entries = build_knowledge(recs)
        kinds = {e.kind for e in entries}
        assert EntryKind.AGENT in kinds
        assert EntryKind.TOOL in kinds
        assert EntryKind.PATTERN in kinds
        assert EntryKind.SESSION in kinds
        assert EntryKind.INCIDENT in kinds
        assert EntryKind.WORKFLOW in kinds
