"""Tests for the v0.5.15 ContextMemory knowledge layer.

Organised by module:

* ``TestSchema``         — KnowledgeEntry round-trip, entry_id helpers
* ``TestStore``          — atomic write + defensive load
* ``TestBuilder``        — per-kind derivation from synthetic records
* ``TestRenderer``       — markdown structure (lead summary, infobox,
                           sections, related, footer)
* ``TestRetrieve``       — by-id lookup, agent fanout, kind/tag filters
* ``TestIntegration``    — build → save → load → render round-trip
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aegis.context_memory.record import ContextMemoryRecord
from aegis.knowledge import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    Section,
    build_knowledge,
    get_entries_for_agent,
    get_entry,
    index_metadata,
    load_entry,
    load_index,
    make_entry_id,
    render_advisor_context,
    render_entry_markdown,
    save_entry,
    save_index,
    search_by_kind_or_tag,
    split_entry_id,
)

# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


def _mk_record(
    *,
    decision: str = "ALLOW",
    reason: str = "",
    tool: str = "Bash",
    aid: str = "agent-A",
    trace_id: str = "trace-001",
    ts_ns: int | None = None,
    cost_usd: float = 0.001,
    latency_ms: float = 100.0,
    step_traces: dict[str, str] | None = None,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns if ts_ns is not None else time.time_ns(),
        trace_id=trace_id,
        invocation_id="inv-001",
        aid=aid,
        tenant_id="t",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        tokens_in=100,
        tokens_out=50,
        step_traces=step_traces or {},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="sidecar",
    )


# ──────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────


class TestSchema:
    def test_entry_id_round_trip(self) -> None:
        for kind in EntryKind:
            slug = "foo-bar.baz"
            entry_id = make_entry_id(kind, slug)
            parsed_kind, parsed_slug = split_entry_id(entry_id)
            assert parsed_kind is kind
            assert parsed_slug == slug

    def test_split_rejects_malformed(self) -> None:
        with pytest.raises(ValueError):
            split_entry_id("no-slash")
        with pytest.raises(ValueError):
            split_entry_id("unknown_kind/foo")
        with pytest.raises(ValueError):
            split_entry_id("agent/")

    def test_entry_serialization_round_trip(self) -> None:
        e = KnowledgeEntry(
            entry_id="agent/foo",
            kind=EntryKind.AGENT,
            title="Agent foo",
            summary="A coding agent.",
            infobox=InfoBox(fields={"n_calls": 42, "total_cost_usd": 0.123}),
            sections=(Section(heading="Activity", body="lots"),),
            related=("tool/Bash",),
            tags=("high-volume",),
            ts_first_ns=1000,
            ts_last_ns=2000,
            n_observations=42,
            confidence=0.84,
        )
        payload = e.to_dict()
        json.dumps(payload)  # must be JSON-serialisable
        roundtripped = KnowledgeEntry.from_dict(payload)
        assert roundtripped.entry_id == e.entry_id
        assert roundtripped.kind == e.kind
        assert roundtripped.title == e.title
        assert roundtripped.summary == e.summary
        assert dict(roundtripped.infobox.fields) == dict(e.infobox.fields)
        assert len(roundtripped.sections) == 1
        assert roundtripped.sections[0].heading == "Activity"
        assert roundtripped.related == ("tool/Bash",)
        assert roundtripped.tags == ("high-volume",)
        assert roundtripped.n_observations == 42
        assert roundtripped.confidence == pytest.approx(0.84)

    def test_from_dict_defensive_against_garbage(self) -> None:
        """Malformed payload fields → safe defaults, no raise."""
        e = KnowledgeEntry.from_dict({
            "entry_id": "agent/x",
            "kind": "unknown-kind",
            "sections": "not a list",
            "related": "not a list",
            "infobox": "not a dict",
            "n_observations": "not an int",
        })
        assert e.kind == EntryKind.AGENT  # fallback
        assert e.sections == ()
        assert e.related == ()
        assert e.infobox.fields == {}
        assert e.n_observations == 0


# ──────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────


class TestStore:
    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        e = KnowledgeEntry(
            entry_id="agent/x",
            kind=EntryKind.AGENT,
            title="X", summary="x",
        )
        root = tmp_path / "nested"
        path = save_entry(e, root=root)
        assert path.exists()
        assert path.parent == root

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_entry("agent/absent", root=tmp_path) is None

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        e = KnowledgeEntry(
            entry_id="tool/Bash",
            kind=EntryKind.TOOL,
            title="Tool Bash",
            summary="3,859 invocations",
            infobox=InfoBox(fields={"n_calls": 3859}),
            tags=("high-volume", "unstable"),
            n_observations=3859,
        )
        save_entry(e, root=tmp_path)
        roundtripped = load_entry("tool/Bash", root=tmp_path)
        assert roundtripped is not None
        assert roundtripped.title == "Tool Bash"
        assert roundtripped.tags == ("high-volume", "unstable")
        assert roundtripped.infobox.fields["n_calls"] == 3859

    def test_index_round_trip(self, tmp_path: Path) -> None:
        entries = [
            KnowledgeEntry(
                entry_id="agent/x", kind=EntryKind.AGENT,
                title="X", summary="x",
                n_observations=100,
            ),
            KnowledgeEntry(
                entry_id="tool/Bash", kind=EntryKind.TOOL,
                title="Tool Bash", summary="bash",
                n_observations=200,
            ),
        ]
        save_index(
            entries, root=tmp_path,
            built_at_ns=1_000_000_000, built_from_records=500,
        )
        rows = load_index(root=tmp_path)
        ids = {r.entry_id for r in rows}
        assert ids == {"agent/x", "tool/Bash"}
        meta = index_metadata(root=tmp_path)
        assert meta["built_at_ns"] == 1_000_000_000
        assert meta["built_from_records"] == 500

    def test_load_index_skips_malformed(self, tmp_path: Path) -> None:
        from aegis.knowledge.store import index_path
        index_path(tmp_path).write_text(
            json.dumps({"entries": ["not a dict", {"kind": "agent",
                                                  "entry_id": "agent/ok"}]}),
            encoding="utf-8",
        )
        rows = load_index(root=tmp_path)
        assert len(rows) == 1
        assert rows[0].entry_id == "agent/ok"


# ──────────────────────────────────────────────────────────────────
# Builder
# ──────────────────────────────────────────────────────────────────


class TestBuilder:
    def test_empty_records_yields_no_entries(self) -> None:
        assert build_knowledge([]) == []

    def test_one_agent_one_tool(self) -> None:
        records = [
            _mk_record(trace_id=f"t{i}") for i in range(100)
        ]
        entries = build_knowledge(records)
        kinds = {e.kind for e in entries}
        # At least AGENT + TOOL entries; no patterns because no
        # REQUIRE_APPROVAL records.
        assert EntryKind.AGENT in kinds
        assert EntryKind.TOOL in kinds
        agent = next(e for e in entries if e.kind == EntryKind.AGENT)
        assert agent.n_observations == 100
        assert agent.infobox.fields["n_calls"] == 100

    def test_pattern_entries_emerge_from_require_approval(self) -> None:
        records = []
        for i in range(20):
            records.append(_mk_record(
                trace_id=f"approval-{i}",
                decision="REQUIRE_APPROVAL",
                reason="same Bash call repeated 3 times this session",
            ))
        entries = build_knowledge(records)
        patterns = [e for e in entries if e.kind == EntryKind.PATTERN]
        assert len(patterns) >= 1
        p = patterns[0]
        assert "loop:Bash" in p.entry_id
        assert p.n_observations == 20

    def test_block_records_drive_unstable_tag(self) -> None:
        records = [
            _mk_record(
                trace_id=f"blk-{i}",
                decision="BLOCK",
                reason=f"rule:foo {i}",
            )
            for i in range(20)
        ]
        records.extend(_mk_record(trace_id=f"ok-{i}") for i in range(20))
        entries = build_knowledge(records)
        tool = next(e for e in entries if e.kind == EntryKind.TOOL)
        assert "unstable" in tool.tags

    def test_confidence_scales_with_observations(self) -> None:
        small = build_knowledge([_mk_record(trace_id=f"t{i}") for i in range(5)])
        large = build_knowledge([_mk_record(trace_id=f"t{i}") for i in range(100)])
        small_agent = next(e for e in small if e.kind == EntryKind.AGENT)
        large_agent = next(e for e in large if e.kind == EntryKind.AGENT)
        assert small_agent.confidence < 0.5
        assert large_agent.confidence == 1.0  # saturated

    def test_cross_references_present(self) -> None:
        records = [
            _mk_record(trace_id=f"t{i}", tool="Bash") for i in range(50)
        ]
        records.extend(
            _mk_record(trace_id=f"e{i}", tool="Edit") for i in range(20)
        )
        records.append(_mk_record(
            trace_id="appr-1",
            decision="REQUIRE_APPROVAL",
            reason="same Bash call repeated 3 times this session",
        ))
        entries = build_knowledge(records)
        agent = next(e for e in entries if e.kind == EntryKind.AGENT)
        # Agent should cross-ref its top tools.
        assert any(r.startswith("tool/Bash") for r in agent.related)
        # And the pattern entry it hit.
        assert any(r.startswith("pattern/") for r in agent.related)


# ──────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_renders_title_and_summary(self) -> None:
        e = KnowledgeEntry(
            entry_id="agent/x",
            kind=EntryKind.AGENT,
            title="Agent X",
            summary="A test agent.",
        )
        md = render_entry_markdown(e)
        assert md.startswith("# Agent X")
        assert "**Summary**: A test agent." in md

    def test_renders_infobox_as_table(self) -> None:
        e = KnowledgeEntry(
            entry_id="tool/Bash",
            kind=EntryKind.TOOL,
            title="Tool Bash",
            summary="test",
            infobox=InfoBox(fields={"n_calls": 1234, "rate": 0.95}),
        )
        md = render_entry_markdown(e)
        assert "## Quick facts" in md
        assert "| Field | Value |" in md
        # Thousands separator on the integer.
        assert "1,234" in md

    def test_renders_sections_in_order(self) -> None:
        e = KnowledgeEntry(
            entry_id="agent/x",
            kind=EntryKind.AGENT,
            title="X",
            summary="x",
            sections=(
                Section(heading="First", body="alpha"),
                Section(heading="Second", body="beta"),
            ),
        )
        md = render_entry_markdown(e)
        first_pos = md.index("## First")
        second_pos = md.index("## Second")
        assert first_pos < second_pos
        assert "alpha" in md
        assert "beta" in md

    def test_skips_empty_section_bodies(self) -> None:
        e = KnowledgeEntry(
            entry_id="agent/x",
            kind=EntryKind.AGENT,
            title="X",
            summary="x",
            sections=(Section(heading="Empty", body=""),),
        )
        md = render_entry_markdown(e)
        assert "## Empty" not in md

    def test_footer_includes_confidence_and_observations(self) -> None:
        e = KnowledgeEntry(
            entry_id="agent/x",
            kind=EntryKind.AGENT,
            title="X", summary="x",
            n_observations=42,
            confidence=0.84,
        )
        md = render_entry_markdown(e)
        assert "42 observations" in md
        assert "84%" in md  # confidence rendered as percent

    def test_advisor_context_separates_entries(self) -> None:
        entries = [
            KnowledgeEntry(entry_id="agent/x", kind=EntryKind.AGENT,
                           title="X", summary="x"),
            KnowledgeEntry(entry_id="tool/Bash", kind=EntryKind.TOOL,
                           title="Bash", summary="bash"),
        ]
        md = render_advisor_context(entries, intro="test intro")
        assert "test intro" in md
        # Each entry should be separated by a horizontal rule.
        assert md.count("---") >= 2  # rules between entries + footer rules

    def test_advisor_context_empty_returns_placeholder(self) -> None:
        md = render_advisor_context([])
        assert "knowledge base" in md.lower() or "knowledge build" in md


# ──────────────────────────────────────────────────────────────────
# Retrieve
# ──────────────────────────────────────────────────────────────────


class TestRetrieve:
    def _populate(self, tmp_path: Path) -> None:
        entries = [
            KnowledgeEntry(
                entry_id="agent/foo",
                kind=EntryKind.AGENT,
                title="Agent foo",
                summary="foo",
                related=("tool/Bash", "pattern/Bash:loop:Bash"),
                n_observations=100,
                tags=("high-volume",),
            ),
            KnowledgeEntry(
                entry_id="tool/Bash",
                kind=EntryKind.TOOL,
                title="Tool Bash",
                summary="bash",
                n_observations=200,
                tags=("high-volume", "unstable"),
            ),
            KnowledgeEntry(
                entry_id="pattern/Bash:loop:Bash",
                kind=EntryKind.PATTERN,
                title="Pattern loop:Bash",
                summary="loop",
                n_observations=50,
                tags=("frequent",),
            ),
        ]
        for e in entries:
            save_entry(e, root=tmp_path)
        save_index(entries, root=tmp_path, built_at_ns=1, built_from_records=1)

    def test_get_entry_by_id(self, tmp_path: Path) -> None:
        self._populate(tmp_path)
        entry = get_entry("agent/foo", root=tmp_path)
        assert entry is not None
        assert entry.title == "Agent foo"

    def test_get_entry_missing(self, tmp_path: Path) -> None:
        self._populate(tmp_path)
        assert get_entry("agent/absent", root=tmp_path) is None

    def test_get_entries_for_agent_includes_cross_refs(
        self, tmp_path: Path,
    ) -> None:
        self._populate(tmp_path)
        entries = get_entries_for_agent("foo", root=tmp_path)
        # Should include agent + the two cross-refs.
        ids = [e.entry_id for e in entries]
        assert "agent/foo" in ids
        assert "tool/Bash" in ids
        assert "pattern/Bash:loop:Bash" in ids
        # Agent should be first.
        assert ids[0] == "agent/foo"

    def test_get_entries_for_agent_missing_aid(
        self, tmp_path: Path,
    ) -> None:
        self._populate(tmp_path)
        assert get_entries_for_agent("absent", root=tmp_path) == []

    def test_search_by_kind(self, tmp_path: Path) -> None:
        self._populate(tmp_path)
        rows = search_by_kind_or_tag(kind=EntryKind.TOOL, root=tmp_path)
        assert len(rows) == 1
        assert rows[0].entry_id == "tool/Bash"

    def test_search_by_tag(self, tmp_path: Path) -> None:
        self._populate(tmp_path)
        rows = search_by_kind_or_tag(tag="unstable", root=tmp_path)
        assert len(rows) == 1
        assert rows[0].entry_id == "tool/Bash"

    def test_search_kind_and_tag_intersect(
        self, tmp_path: Path,
    ) -> None:
        self._populate(tmp_path)
        rows = search_by_kind_or_tag(
            kind=EntryKind.PATTERN, tag="high-volume", root=tmp_path,
        )
        assert rows == []  # no pattern has high-volume tag


# ──────────────────────────────────────────────────────────────────
# Integration — full build → save → load → render
# ──────────────────────────────────────────────────────────────────


class TestIntegration:
    def test_build_save_load_render_round_trip(
        self, tmp_path: Path,
    ) -> None:
        records = []
        for i in range(60):
            records.append(_mk_record(
                trace_id=f"ok-{i}", tool="Bash", aid="agent-int",
            ))
        for i in range(20):
            records.append(_mk_record(
                trace_id=f"appr-{i}",
                aid="agent-int",
                decision="REQUIRE_APPROVAL",
                reason="same Bash call repeated 3 times this session",
            ))

        entries = build_knowledge(records)
        for e in entries:
            save_entry(e, root=tmp_path)
        save_index(entries, root=tmp_path, built_at_ns=1, built_from_records=80)

        # Reload everything through the public API.
        agent_entries = get_entries_for_agent("agent-int", root=tmp_path)
        assert len(agent_entries) >= 1
        primary = agent_entries[0]
        assert primary.entry_id == "agent/agent-int"
        # Render should mention the loop pattern.
        md = render_advisor_context(agent_entries, intro="adviser context")
        assert "Agent agent-int" in md
        assert "adviser context" in md
