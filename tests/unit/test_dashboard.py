"""Tests for ``aegis.dashboard`` — the rich-based TUI.

Covers:
* :func:`collect_stats` with empty / populated / demo-mode inputs
* :func:`build_layout` produces a well-formed rich.Layout
* :func:`run_dashboard` runs N frames then exits cleanly
* CLI subcommand wiring routes to :func:`cmd_dashboard`
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.layout import Layout

from aegis.context_memory.record import ContextMemoryRecord
from aegis.context_memory.writer import append
from aegis.dashboard import (
    build_layout,
    collect_stats,
    run_dashboard,
)
from aegis.dashboard.main import (
    DashboardStats,
    _demo_records,
    _short_provider,
)

# ── short_provider helper ────────────────────────────────────────


@pytest.mark.parametrize(
    ("inp", "expected_substring"),
    [
        ("openrouter:anthropic-claude-sonnet-4", "OR:"),
        ("anthropic-claude-3-5", "Claude"),
        ("openai-gpt-4o", "GPT"),
        ("google-gemini-1.5", "Gemini"),
        ("local-llama-3.1-8b", "Llama"),
        ("some-unknown-provider", "some-unknown"),
    ],
)
def test_short_provider_examples(inp: str, expected_substring: str) -> None:
    assert expected_substring in _short_provider(inp)


# ── demo records ─────────────────────────────────────────────────


def test_demo_records_yield_at_least_eight() -> None:
    """Demo data must be substantive enough to populate every panel
    (cost / perf / sec / advisor / recent-blocks). Eight is the
    minimum that exercises BLOCK and APPROVAL paths."""
    recs = list(_demo_records())
    assert len(recs) >= 8
    decisions = {r.decision for r in recs}
    assert decisions == {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}


def test_demo_records_have_costs() -> None:
    """Cost panel needs > 0 dollar totals to show meaningful numbers
    on a fresh install. Catch regressions where someone dials demo
    costs down to ~0."""
    recs = list(_demo_records())
    total = sum(r.cost_usd for r in recs)
    assert total > 1.0, f"demo total ${total:.2f} is too low for a meaningful screen"


# ── collect_stats ────────────────────────────────────────────────


def test_collect_stats_empty_falls_back_to_demo(tmp_path: Path) -> None:
    """When ContextMemory is empty, the dashboard MUST still produce
    a stats bundle (via demo records) so the first-run UX is OK."""
    cm = tmp_path / "context_memory.jsonl"
    cm.touch()
    stats = collect_stats(context_memory_path=cm)
    assert isinstance(stats, DashboardStats)
    assert stats.is_demo is True
    assert stats.record_count >= 8


def test_collect_stats_with_explicit_demo_flag(tmp_path: Path) -> None:
    stats = collect_stats(demo=True, context_memory_path=tmp_path / "absent.jsonl")
    assert stats.is_demo is True
    assert stats.cost.total_usd > 1.0


def test_collect_stats_real_records_not_demo(tmp_path: Path) -> None:
    """When ContextMemory has actual records in the window, the
    is_demo flag MUST be False."""
    cm = tmp_path / "context_memory.jsonl"
    # Append one fresh record
    rec = ContextMemoryRecord(
        schema_version=1,
        ts_ns=int(_now_ns()),
        trace_id="real-trace-001",
        invocation_id="real-inv-001",
        aid="agent-a",
        tenant_id="t1",
        tool_name="Bash",
        decision="ALLOW",
        reason="",
        channel="cli",
        provider="anthropic-claude-sonnet-4",
        latency_ms=15.0,
        cost_usd=0.10,
        tokens_in=100, tokens_out=50,
        step_traces={"step310": "safe"},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="local",
    )
    append(rec, path=cm)
    stats = collect_stats(context_memory_path=cm)
    assert stats.is_demo is False
    assert stats.record_count == 1
    assert stats.cost.total_usd == pytest.approx(0.10)


def test_collect_stats_filters_by_window(tmp_path: Path) -> None:
    """A record older than the window MUST be excluded."""
    cm = tmp_path / "context_memory.jsonl"
    # 100 days ago
    old_ns = int(_now_ns()) - int(100 * 24 * 3600 * 1e9)
    rec = ContextMemoryRecord(
        schema_version=1,
        ts_ns=old_ns,
        trace_id="ancient",
        invocation_id="ancient-inv",
        aid="agent-a",
        tenant_id="t1",
        tool_name="Bash",
        decision="ALLOW",
        reason="",
        channel="cli",
        provider="anthropic-claude-sonnet-4",
        latency_ms=15.0, cost_usd=0.10,
        tokens_in=100, tokens_out=50,
        step_traces={},
        m13_score=None, advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None, atv_dim=2080,
        is_sidechain=False, mode="local",
    )
    append(rec, path=cm)
    # 24h window — ancient record excluded → demo fallback
    stats = collect_stats(context_memory_path=cm, since_hours=24.0)
    assert stats.is_demo is True


def test_collect_stats_sorts_recommendations_high_first() -> None:
    """Advisor recommendations MUST be sorted high → low so the
    panel always shows the most actionable ones."""
    stats = collect_stats(demo=True)
    if len(stats.recommendations) >= 2:
        priorities = [r.priority for r in stats.recommendations]
        order = {"high": 0, "medium": 1, "low": 2, "info": 3}
        prio_ranks = [order.get(p, 9) for p in priorities]
        assert prio_ranks == sorted(prio_ranks), (
            f"recommendations not high-first: {priorities}"
        )


def test_collect_stats_recent_blocks_capped_at_five() -> None:
    """Recent-BLOCKs panel is bounded so the layout doesn't overflow."""
    stats = collect_stats(demo=True)
    assert len(stats.recent_blocks) <= 5


# ── build_layout ─────────────────────────────────────────────────


def test_build_layout_produces_rich_layout() -> None:
    stats = collect_stats(demo=True)
    layout = build_layout(stats)
    assert isinstance(layout, Layout)
    # Five top-level rows: header / stats / recs / blocks / footer
    children = list(layout.children)
    assert len(children) == 5


def test_build_layout_renders_without_error() -> None:
    """The composed layout must render to a string without raising."""
    stats = collect_stats(demo=True)
    layout = build_layout(stats)
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    console.print(layout)
    output = buf.getvalue()
    assert "Aegis Console" in output
    assert "Cost" in output
    assert "Performance" in output
    assert "Security" in output


def test_build_layout_marks_demo_mode_visibly() -> None:
    stats = collect_stats(demo=True)
    layout = build_layout(stats)
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    console.print(layout)
    output = buf.getvalue()
    assert "DEMO DATA" in output


# ── run_dashboard ────────────────────────────────────────────────


def test_run_dashboard_exits_after_max_frames(tmp_path: Path) -> None:
    """The runner MUST honour ``max_frames`` so tests don't hang.
    Uses a stub Console writing to a buffer (no actual terminal)."""
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False, legacy_windows=False)
    rc = run_dashboard(
        refresh_seconds=0.01,
        demo=True,
        console=console,
        max_frames=2,
    )
    assert rc == 0


def test_run_dashboard_handles_missing_context_memory(tmp_path: Path) -> None:
    """A missing ContextMemory file MUST fall back to demo mode, not
    crash."""
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False, legacy_windows=False)
    rc = run_dashboard(
        refresh_seconds=0.01,
        context_memory_path=tmp_path / "does-not-exist.jsonl",
        console=console,
        max_frames=1,
    )
    assert rc == 0


# ── CLI wiring ───────────────────────────────────────────────────


def test_cli_dashboard_subcommand_wired() -> None:
    """`aegis dashboard` MUST parse + route to cmd_dashboard."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    args = parser.parse_args(["dashboard", "--demo", "--refresh", "0.05"])
    assert args.fn is aegis_cli.cmd_dashboard
    assert args.demo is True
    assert args.refresh == 0.05


def test_cli_dashboard_default_refresh_is_2s() -> None:
    """Default refresh is 2s — the spec target. Catches regressions
    if someone changes the default to something annoyingly aggressive."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    args = parser.parse_args(["dashboard"])
    assert args.refresh == 2.0
    assert args.since_hours == 24.0
    assert args.demo is False


# ── helpers ─────────────────────────────────────────────────────


def _now_ns() -> float:
    import time
    return time.time() * 1_000_000_000
