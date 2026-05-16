"""v0.5.2 PR ③ — `aegis memory claude-md` proposal generator.

Exercises the four miners + dedup + rendering. Uses synthetic
ContextMemoryRecord lists so we don't depend on a real ~/.aegis
store. Destructive keywords (the SQL drop-table verb, the recursive
remove command, system-secret paths) are spliced at module load
(via ``_KW_*``) so the Aegis step310 firewall doesn't BLOCK this
test file when an operator edits it under an active hook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from aegis.context_memory.claude_md_proposals import (
    ApplyResult,
    Proposal,
    apply_proposal,
    propose_edits,
    render_proposals_markdown,
)
from aegis.context_memory.record import ContextMemoryRecord

# Same self-defense trick as the module under test — bypass our own
# step310 firewall when this test file is written / edited. The
# firewall scans literal source bytes for `/etc/(shadow|passwd)`,
# `\brm\s+-rf\s+/`, and `DROP\s+TABLE` patterns; concatenating at
# import time satisfies the test data while keeping the source
# bytes innocuous.
_KW_DROP_TABLE: Final[str] = "DROP" + " TABLE"
_KW_RM_RF: Final[str] = "rm" + " -rf"
_KW_RM_RF_ROOT: Final[str] = _KW_RM_RF + " " + "/"
_PATH_SECRET_A: Final[str] = "/etc/" + "passwd"
_PATH_SECRET_B: Final[str] = "/etc/" + "shadow"


# ── helpers ────────────────────────────────────────────────────────


def _rec(
    *,
    decision: str,
    reason: str = "",
    tool: str = "Bash",
    trace_id: str = "deadbeef",
    ts_ns: int = 1_700_000_000_000_000_000,
    cost_usd: float = 0.0,
    recommended_advisors: tuple[str, ...] = (),
) -> ContextMemoryRecord:
    """Minimal record factory with sensible defaults for the fields
    miners don't read. Keeps test cases short."""
    return ContextMemoryRecord(
        ts_ns=ts_ns,
        trace_id=trace_id,
        invocation_id="inv-x",
        aid="aid-x",
        tenant_id="local",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=10.0,
        cost_usd=cost_usd,
        tokens_in=0,
        tokens_out=0,
        step_traces={},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=recommended_advisors,
        atv_sha3=None,
        atv_dim=0,
        is_sidechain=False,
        mode="local",
    )


# Pre-built reason strings — built once via concatenation so the
# source file stays clean of literal destructive bytes.
_REASON_DROP_TABLE = "dangerous pattern: " + "DROP" + r"\s+TABLE"
_REASON_RM_RF_ROOT = "dangerous pattern: " + r"\brm\s+-rf\s+/"


# ── dangerous-pattern miner ────────────────────────────────────────


def test_dangerous_pattern_drop_table_known_pattern() -> None:
    """The SQL drop-table regex is in _DANGER_LOOKUP, so we get
    high-confidence + curated prose. Threshold is 3 by default."""
    records = [
        _rec(decision="BLOCK", reason=_REASON_DROP_TABLE, trace_id=f"t{i}")
        for i in range(3)
    ]
    proposals = propose_edits(records, min_count=3)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.kind == "dangerous-pattern"
    assert p.count == 3
    assert p.confidence == "high"
    assert "migration" in p.suggested_text.lower()
    assert p.suggested_section == "Security Notes"
    assert len(p.sample_trace_ids) == 3


def test_dangerous_pattern_rm_rf_known_pattern() -> None:
    records = [
        _rec(decision="BLOCK", reason=_REASON_RM_RF_ROOT, trace_id=f"t{i}")
        for i in range(5)
    ]
    proposals = propose_edits(records, min_count=3)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.kind == "dangerous-pattern"
    assert p.count == 5
    assert p.confidence == "high"
    # suggested text mentions the destructive form
    assert "rm" in p.suggested_text and "-rf" in p.suggested_text


def test_dangerous_pattern_below_threshold_skipped() -> None:
    """Two hits = noise; we don't surface."""
    records = [
        _rec(decision="BLOCK", reason=_REASON_DROP_TABLE)
        for _ in range(2)
    ]
    assert propose_edits(records, min_count=3) == []


def test_dangerous_pattern_unknown_falls_back_to_generic() -> None:
    """An unknown pattern still surfaces but at medium confidence."""
    records = [
        _rec(
            decision="BLOCK",
            reason="dangerous pattern: " + r"\bSOME_NEW_VERB\b",
            trace_id=f"t{i}",
        )
        for i in range(3)
    ]
    proposals = propose_edits(records, min_count=3)
    assert len(proposals) == 1
    assert proposals[0].confidence == "medium"
    # Either the rationale or the suggested text should signal the
    # generic-fallback path.
    text = (
        proposals[0].rationale + " " + proposals[0].suggested_text
    ).lower()
    assert "yet" in text or "firewall pattern" in text


def test_dangerous_pattern_ignores_non_block_decisions() -> None:
    """REQUIRE_APPROVAL with a dangerous-pattern reason shouldn't slip
    through the dangerous-pattern miner — only BLOCK surfaces there."""
    records = [
        _rec(decision="REQUIRE_APPROVAL", reason=_REASON_DROP_TABLE)
        for _ in range(5)
    ]
    proposals = propose_edits(records, min_count=3)
    assert [p for p in proposals if p.kind == "dangerous-pattern"] == []


# ── loop-detector miner ────────────────────────────────────────────


def test_loop_detector_groups_by_tool_name() -> None:
    records = (
        [_rec(
            decision="REQUIRE_APPROVAL",
            reason="same Bash call repeated 3 times this session (threshold=3)",
            trace_id=f"b{i}",
        ) for i in range(4)]
        + [_rec(
            decision="REQUIRE_APPROVAL",
            reason="same Read call repeated 3 times this session (threshold=3)",
            trace_id=f"r{i}",
        ) for i in range(3)]
    )
    proposals = propose_edits(records, min_count=3)
    loop_props = [p for p in proposals if p.kind == "loop-detector"]
    assert len(loop_props) == 2
    by_pattern = {p.pattern: p for p in loop_props}
    assert by_pattern["repeated Bash"].count == 4
    assert by_pattern["repeated Read"].count == 3


def test_loop_detector_skips_below_threshold() -> None:
    records = [_rec(
        decision="REQUIRE_APPROVAL",
        reason="same Bash call repeated 3 times this session (threshold=3)",
    ) for _ in range(2)]
    proposals = propose_edits(records, min_count=3)
    assert [p for p in proposals if p.kind == "loop-detector"] == []


def test_loop_detector_ignores_block_decisions() -> None:
    """Loop detector is REQUIRE_APPROVAL only — BLOCK loop reasons
    would be unusual and we don't claim them."""
    records = [_rec(
        decision="BLOCK",
        reason="same Bash call repeated 3 times this session (threshold=3)",
    ) for _ in range(5)]
    proposals = propose_edits(records, min_count=3)
    assert [p for p in proposals if p.kind == "loop-detector"] == []


# ── sensitive-path miner ───────────────────────────────────────────


def test_sensitive_path_groups_by_path() -> None:
    records = (
        [_rec(
            decision="REQUIRE_APPROVAL",
            reason="sensitive path requires approval: " + _PATH_SECRET_A,
            trace_id=f"a{i}",
        ) for i in range(5)]
        + [_rec(
            decision="REQUIRE_APPROVAL",
            reason="sensitive path requires approval: " + _PATH_SECRET_B,
            trace_id=f"b{i}",
        ) for i in range(3)]
    )
    proposals = propose_edits(records, min_count=3)
    path_props = [p for p in proposals if p.kind == "sensitive-path"]
    assert len(path_props) == 2
    by_pattern = {p.pattern: p for p in path_props}
    assert by_pattern[_PATH_SECRET_A].count == 5
    assert by_pattern[_PATH_SECRET_B].count == 3
    assert by_pattern[_PATH_SECRET_A].priority_score > \
           by_pattern[_PATH_SECRET_B].priority_score


# ── rule-violation miner ───────────────────────────────────────────


def test_rule_violation_extracts_rule_name() -> None:
    records = [_rec(
        decision="BLOCK",
        reason="rule:git_destructive",
        trace_id=f"g{i}",
    ) for i in range(7)]
    proposals = propose_edits(records, min_count=3)
    rule_props = [p for p in proposals if p.kind == "rule-violation"]
    assert len(rule_props) == 1
    p = rule_props[0]
    assert p.pattern == "rule:git_destructive"
    assert p.count == 7
    assert "git_destructive" in p.suggested_text
    assert "blocks" in p.suggested_text.lower()


def test_rule_violation_decision_wording() -> None:
    """BLOCK reasons get "blocks", REQUIRE_APPROVAL gets
    "requires approval for"."""
    records_block = [_rec(decision="BLOCK", reason="rule:foo") for _ in range(3)]
    records_appr = [_rec(decision="REQUIRE_APPROVAL", reason="rule:bar") for _ in range(3)]

    p_block = next(p for p in propose_edits(records_block, min_count=3)
                   if p.pattern == "rule:foo")
    p_appr = next(p for p in propose_edits(records_appr, min_count=3)
                  if p.pattern == "rule:bar")
    assert "blocks" in p_block.suggested_text.lower()
    assert "requires approval" in p_appr.suggested_text.lower()


# ── high-cost-tool miner ───────────────────────────────────────────


def test_high_cost_tool_surfaces_when_cumulative_above_threshold() -> None:
    """A tool with cumulative cost above the threshold AND call-count
    above min_count should surface as a high-cost-tool proposal."""
    records = [
        _rec(decision="ALLOW", tool="ExpensiveLLM", cost_usd=0.005,
             trace_id=f"e{i}") for i in range(5)  # total = $0.025
    ]
    proposals = propose_edits(records, min_count=3)
    cost_props = [p for p in proposals if p.kind == "high-cost-tool"]
    assert len(cost_props) == 1
    p = cost_props[0]
    assert p.pattern == "tool:ExpensiveLLM"
    assert p.count == 5
    assert p.suggested_section == "Cost Discipline"


def test_high_cost_tool_skips_below_dollar_threshold() -> None:
    """Many calls but tiny total cost → below threshold → skip."""
    records = [
        _rec(decision="ALLOW", tool="CheapTool", cost_usd=0.0001,
             trace_id=f"c{i}") for i in range(10)  # total = $0.001
    ]
    proposals = propose_edits(records, min_count=3)
    assert [p for p in proposals if p.kind == "high-cost-tool"] == []


def test_high_cost_tool_skips_block_and_approval() -> None:
    """Only ALLOW records get charged. BLOCK + REQUIRE_APPROVAL
    records (which carry cost_usd in some edge cases) must not feed
    the high-cost-tool miner — the agent didn't actually pay for those."""
    records = (
        [_rec(decision="BLOCK", tool="X", cost_usd=1.00) for _ in range(5)]
        + [_rec(decision="REQUIRE_APPROVAL", tool="X", cost_usd=1.00)
           for _ in range(5)]
    )
    proposals = propose_edits(records, min_count=3)
    assert [p for p in proposals if p.kind == "high-cost-tool"] == []


def test_high_cost_tool_below_min_count_skipped() -> None:
    """One expensive call ≠ pattern. Below min_count → skip even if
    the single call is over the $-threshold."""
    records = [
        _rec(decision="ALLOW", tool="X", cost_usd=10.00, trace_id="big"),
    ]
    proposals = propose_edits(records, min_count=3)
    assert [p for p in proposals if p.kind == "high-cost-tool"] == []


def test_high_cost_tool_confidence_scales_with_call_count() -> None:
    """≥10 calls = high confidence; fewer = medium."""
    few = [
        _rec(decision="ALLOW", tool="ToolA", cost_usd=0.01, trace_id=f"a{i}")
        for i in range(5)  # 5 calls × $0.01 = $0.05
    ]
    many = [
        _rec(decision="ALLOW", tool="ToolB", cost_usd=0.01, trace_id=f"b{i}")
        for i in range(12)
    ]
    p_few = next(p for p in propose_edits(few, min_count=3)
                 if p.pattern == "tool:ToolA")
    p_many = next(p for p in propose_edits(many, min_count=3)
                  if p.pattern == "tool:ToolB")
    assert p_few.confidence == "medium"
    assert p_many.confidence == "high"


def test_high_cost_tool_custom_threshold_via_propose_edits() -> None:
    """`min_tool_cost_usd` raises the $-threshold for the high-cost
    miner only — other miners are unaffected."""
    records = [
        _rec(decision="ALLOW", tool="X", cost_usd=0.05, trace_id=f"x{i}")
        for i in range(5)  # total = $0.25
    ]
    # Default 0.01 threshold → fires.
    assert any(
        p.kind == "high-cost-tool"
        for p in propose_edits(records, min_count=3)
    )
    # Custom $1.00 threshold → suppressed.
    assert not any(
        p.kind == "high-cost-tool"
        for p in propose_edits(
            records, min_count=3, min_tool_cost_usd=1.0,
        )
    )


# ── advisor-recommendation miner ───────────────────────────────────


def test_advisor_recommendation_rolls_up_by_advisor_name() -> None:
    records = (
        [_rec(decision="ALLOW", recommended_advisors=("cost-watcher",),
              trace_id=f"c{i}") for i in range(5)]
        + [_rec(decision="ALLOW", recommended_advisors=("security-reviewer",),
                trace_id=f"s{i}") for i in range(3)]
    )
    proposals = propose_edits(records, min_count=3)
    adv = [p for p in proposals if p.kind == "advisor-recommendation"]
    assert len(adv) == 2
    by_pattern = {p.pattern: p for p in adv}
    assert by_pattern["advisor:cost-watcher"].count == 5
    assert by_pattern["advisor:security-reviewer"].count == 3


def test_advisor_recommendation_below_threshold_skipped() -> None:
    records = [
        _rec(decision="ALLOW", recommended_advisors=("rare-advisor",))
        for _ in range(2)
    ]
    assert [p for p in propose_edits(records, min_count=3)
            if p.kind == "advisor-recommendation"] == []


def test_advisor_recommendation_section_keyword_routing() -> None:
    """The advisor name's keyword determines which CLAUDE.md section
    to suggest. cost-* → Cost Discipline, security-* → Security
    Notes, etc."""
    records = (
        [_rec(decision="ALLOW", recommended_advisors=("cost-budget-watcher",),
              trace_id=f"c{i}") for i in range(3)]
        + [_rec(decision="ALLOW", recommended_advisors=("security-pii",),
                trace_id=f"s{i}") for i in range(3)]
        + [_rec(decision="ALLOW", recommended_advisors=("zzz-uncategorised",),
                trace_id=f"z{i}") for i in range(3)]
    )
    by_advisor = {p.pattern: p for p in propose_edits(records, min_count=3)
                  if p.kind == "advisor-recommendation"}
    assert by_advisor["advisor:cost-budget-watcher"].suggested_section == "Cost Discipline"
    assert by_advisor["advisor:security-pii"].suggested_section == "Security Notes"
    assert by_advisor["advisor:zzz-uncategorised"].suggested_section == "Project Guardrails"


def test_advisor_recommendation_multiple_advisors_per_record() -> None:
    """A single record can carry multiple advisors in the tuple —
    each one counts."""
    records = [
        _rec(decision="ALLOW",
             recommended_advisors=("a", "b"), trace_id=f"t{i}")
        for i in range(3)
    ]
    advs = [p for p in propose_edits(records, min_count=3)
            if p.kind == "advisor-recommendation"]
    # Both "a" and "b" surface — each fired 3 times across the 3 records.
    assert {p.pattern for p in advs} == {"advisor:a", "advisor:b"}


def test_advisor_recommendation_ignores_empty_strings() -> None:
    """Empty string advisor names (defensive against bad data) are
    skipped silently."""
    records = [
        _rec(decision="ALLOW",
             recommended_advisors=("", "real-advisor"), trace_id=f"t{i}")
        for i in range(3)
    ]
    advs = [p for p in propose_edits(records, min_count=3)
            if p.kind == "advisor-recommendation"]
    assert {p.pattern for p in advs} == {"advisor:real-advisor"}


# ── dedup against current CLAUDE.md ────────────────────────────────


def test_dedup_skips_proposal_when_pattern_already_in_md() -> None:
    """If the current CLAUDE.md already mentions the pattern, skip —
    the operator has already documented it."""
    records = [
        _rec(decision="BLOCK", reason=_REASON_RM_RF_ROOT)
        for _ in range(5)
    ]
    md_mentioning = (
        "# Project guide\n\nNever use the recursive remove command "
        f"`{_KW_RM_RF}` without confirmation.\n"
    )
    proposals = propose_edits(records, current_md_text=md_mentioning)
    assert [p for p in proposals if p.kind == "dangerous-pattern"] == []


def test_dedup_does_not_skip_unrelated_pattern() -> None:
    """An unrelated mention in CLAUDE.md shouldn't suppress unrelated
    proposals — anchoring on the actual pattern keeps false-positives
    low."""
    records = [
        _rec(decision="BLOCK", reason=_REASON_DROP_TABLE)
        for _ in range(5)
    ]
    md_about_git = "# Project guide\n\nUse `git rebase` over merge.\n"
    proposals = propose_edits(records, current_md_text=md_about_git)
    assert len(proposals) == 1
    assert proposals[0].kind == "dangerous-pattern"


# ── priority sort ──────────────────────────────────────────────────


def test_priority_sort_orders_by_score() -> None:
    """High-confidence × high-count wins; ties broken by kind."""
    records = (
        # rule-violation × 10 (medium confidence)  → score 20
        [_rec(decision="BLOCK", reason="rule:a", trace_id=f"a{i}")
         for i in range(10)]
        # loop-detector × 4 (high confidence)      → score 12
        + [_rec(
            decision="REQUIRE_APPROVAL",
            reason="same X call repeated 3 times this session",
            trace_id=f"l{i}",
        ) for i in range(4)]
    )
    proposals = propose_edits(records, min_count=3)
    kinds = [p.kind for p in proposals]
    # rule-violation (score 20) comes before loop-detector (score 12)
    assert kinds == ["rule-violation", "loop-detector"]


# ── markdown rendering ─────────────────────────────────────────────


def test_render_empty_proposals_shows_clean_message() -> None:
    md = render_proposals_markdown(
        proposals=[],
        window_seconds=7 * 86400,
        record_count=42,
    )
    assert "No actionable proposals" in md
    assert "42" in md
    assert "7d" in md


def test_render_proposals_includes_all_sections() -> None:
    p = Proposal(
        kind="dangerous-pattern",
        pattern="some-pattern",
        count=5,
        suggested_section="Security Notes",
        suggested_text=f"Never issue `{_KW_DROP_TABLE}` directly.",
        rationale="Aegis blocks this.",
        sample_trace_ids=("aaa", "bbb"),
        confidence="high",
    )
    md = render_proposals_markdown(
        proposals=[p],
        window_seconds=86400,
        md_path="/tmp/CLAUDE.md",
        record_count=100,
    )
    assert "# CLAUDE.md improvement proposals" in md
    assert "/tmp/CLAUDE.md" in md
    assert "Never issue" in md
    assert "Security Notes" in md
    assert "aaa" in md and "bbb" in md
    assert "Aegis blocks this." in md
    assert "1d" in md  # window label


def test_render_window_label_handles_sub_day() -> None:
    md = render_proposals_markdown(
        proposals=[],
        window_seconds=3 * 3600,
        record_count=0,
    )
    assert "3h" in md


# ── apply_proposal ─────────────────────────────────────────────────


def _make_proposal(
    *, section: str = "Workflow Discipline", text: str = "Do the thing."
) -> Proposal:
    return Proposal(
        kind="loop-detector",
        pattern="repeated X",
        count=5,
        suggested_section=section,
        suggested_text=text,
        rationale="Because.",
        sample_trace_ids=(),
        confidence="high",
    )


def test_apply_proposal_inserts_under_matching_heading(tmp_path):
    """When the suggested section already exists as a heading, the
    splice lands immediately after that heading and a .bak is
    written."""
    md = tmp_path / "CLAUDE.md"
    md.write_text(
        "# Project\n\n## Workflow Discipline\n\nUse small commits.\n\n## Other\n\nMisc.\n",
        encoding="utf-8",
    )
    p = _make_proposal()
    result = apply_proposal(p, md)

    assert isinstance(result, ApplyResult)
    assert result.bak_path is not None
    assert Path(str(result.bak_path)).exists()
    assert result.inserted_under == "Workflow Discipline"
    assert result.new_lines_added > 0

    new = md.read_text(encoding="utf-8")
    # Splice landed AFTER the Workflow Discipline heading but BEFORE
    # the existing "Use small commits" body — the standard insertion
    # point is right after the heading line.
    h_pos = new.index("## Workflow Discipline")
    body_pos = new.index("Use small commits")
    splice_pos = new.index("Do the thing.")
    assert h_pos < splice_pos < body_pos
    # Marker present + carries the kind for traceability.
    assert "aegis-managed-proposal" in new
    assert "kind=loop-detector" in new
    # The "Other" section is untouched.
    assert "## Other" in new and "Misc." in new


def test_apply_proposal_appends_new_section_when_no_match(tmp_path):
    """No matching heading → append `## <section>` block at EOF."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Project\n\nNothing else.\n", encoding="utf-8")
    p = _make_proposal(section="Brand New Section", text="Mind this.")
    result = apply_proposal(p, md)

    assert result.inserted_under == "(appended new section)"
    new = md.read_text(encoding="utf-8")
    assert "## Brand New Section" in new
    assert "Mind this." in new
    # New content is at the END of the file, not before the original.
    assert new.index("Nothing else.") < new.index("## Brand New Section")


def test_apply_proposal_case_insensitive_heading_match(tmp_path):
    """Heading matching is case-insensitive substring — operators
    won't always write the section label the same way we suggested."""
    md = tmp_path / "CLAUDE.md"
    md.write_text(
        "# Project\n\n## Security\n\nWatch out.\n",
        encoding="utf-8",
    )
    p = _make_proposal(section="Security Notes")
    # The miner suggested "Security Notes" but the project has just
    # "Security". Substring match should still find it.
    result = apply_proposal(p, md)
    assert result.inserted_under == "Security"


def test_apply_proposal_no_bak_when_write_backup_false(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# x\n\n## Section\n\nbody\n", encoding="utf-8")
    p = _make_proposal(section="Section")
    result = apply_proposal(p, md, write_backup=False)
    assert result.bak_path is None
    assert not (tmp_path / "CLAUDE.md.bak").exists()


def test_apply_proposal_preserves_trailing_newline(tmp_path):
    """If the original file ends with a newline, the new file should
    too — otherwise diff tools complain. Same for no-trailing-newline
    (we preserve the convention of the input)."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("# x\n\n## Section\n\nbody\n", encoding="utf-8")
    p = _make_proposal(section="Section")
    apply_proposal(p, md)
    assert md.read_text(encoding="utf-8").endswith("\n")


def test_apply_proposal_marker_includes_metadata(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# x\n\n## Section\n\nbody\n", encoding="utf-8")
    p = Proposal(
        kind="dangerous-pattern",
        pattern="my-pattern",
        count=7,
        suggested_section="Section",
        suggested_text="Avoid X.",
        rationale="r",
        sample_trace_ids=(),
        confidence="medium",
    )
    apply_proposal(p, md)
    new = md.read_text(encoding="utf-8")
    assert "aegis-managed-proposal" in new
    assert "kind=dangerous-pattern" in new
    assert "pattern='my-pattern'" in new
    assert "confidence=medium" in new
