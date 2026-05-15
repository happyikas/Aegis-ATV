"""``aegis dashboard`` — one-screen rich-based TUI.

Why a dashboard
---------------

The CLI surface today is 8+ commands. A non-engineer running Claude
Code wants ONE place to glance at: "is everything OK right now?" The
dashboard answers that in 5 seconds — colored panels, sparklines,
top advisor recommendations — without remembering any subcommand.

Design constraints
------------------

* **No textual full-framework dep** — ``rich`` already gives Layout +
  Live + Panel + Table, which is enough for v1. Upgrading to textual
  (full key-bindings, scroll regions, modal popups) is reserved for
  v2 once user feedback lands.
* **Refresh from ContextMemory only** — no network calls, no audit-
  chain re-walk per tick. ContextMemory is already the analytics
  fast-path; we just read recent records via
  :mod:`aegis.context_memory.report`.
* **Auto-refresh, no clicks** — sets ``Live(refresh_per_second=)`` so
  the panels redraw without user input. Ctrl-C exits cleanly.
* **Demo mode** — when ContextMemory is empty (fresh install / first
  run), render plausible synthetic data with a clear "DEMO" banner so
  newcomers see what to expect.

Layout
------

::

    ╭─ Aegis Console · 14:32 · 🟢 healthy ──────────────────────╮
    │ ┌─ 💰 Cost ──┐ ┌─ ⚡ Perf ──┐ ┌─ 🛡️ Security ─────────┐ │
    │ │ $4.18      │ │ p95 47ms  │ │ ALLOW   APPROVAL  BLK │ │
    │ │ Claude 94% │ │ rate 12/s │ │ 1,198   38        7    │ │
    │ └────────────┘ └───────────┘ └────────────────────────┘ │
    │                                                          │
    │ 💡 Advisor Recommendations (top 5)                       │
    │  [HIGH] cost-optimizer  swap-model Bash → Haiku (12%)    │
    │  [MED]  loop-breaker    same call 5× in last hour        │
    │  …                                                       │
    │                                                          │
    │ Recent BLOCKs (last 24h)                                 │
    │  14:28 destructive-bash  trace=abc123  'rm production'   │
    │  …                                                       │
    │                                                          │
    │  Press Ctrl-C to exit · refresh 2s                       │
    ╰──────────────────────────────────────────────────────────╯
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aegis.context_memory import (
    ContextMemoryRecord,
    read_all,
)
from aegis.context_memory.advisor import (
    Recommendation,
    cost_advice,
    performance_advice,
    security_advice,
)
from aegis.context_memory.analytics import (
    CostStats,
    PerformanceStats,
    SecurityStats,
    WindowSummary,
    cost_stats,
    performance_stats,
    security_stats,
    window_summary,
)

DEFAULT_WINDOW_HOURS = 24
DEFAULT_REFRESH_SECONDS = 2.0


# ── stats bundle ────────────────────────────────────────────────


@dataclass(frozen=True)
class DashboardStats:
    """Everything the dashboard needs to draw one frame.

    Computed once per refresh tick. Immutable so the renderer can't
    silently drift between panels (e.g. cost panel showing 24h while
    security panel shows 7d due to a mutation race).
    """

    window: WindowSummary
    cost: CostStats
    perf: PerformanceStats
    security: SecurityStats
    recommendations: list[Recommendation]
    recent_blocks: list[ContextMemoryRecord]
    record_count: int
    is_demo: bool


def collect_stats(
    *,
    since_hours: float = DEFAULT_WINDOW_HOURS,
    context_memory_path: Path | None = None,
    demo: bool = False,
) -> DashboardStats:
    """Read ContextMemory + compute all stats needed for one frame.

    When ``demo=True`` OR the store is empty, synthesises plausible
    records so a fresh install still shows a meaningful screen.
    """
    if demo:
        records = list(_demo_records())
        is_demo = True
    else:
        all_records = read_all(context_memory_path)
        cutoff_ns = int(
            (time.time() - since_hours * 3600.0) * 1_000_000_000,
        )
        records = [r for r in all_records if r.ts_ns >= cutoff_ns]
        is_demo = not records
        if is_demo:
            records = list(_demo_records())

    win = window_summary(records)
    cost = cost_stats(records)
    perf = performance_stats(records)
    sec = security_stats(records)
    recs = (
        cost_advice(cost)
        + performance_advice(perf)
        + security_advice(sec)
    )
    # Prioritise — high > medium > low > info
    priority_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    recs.sort(key=lambda r: priority_order.get(r.priority, 9))
    blocks = sorted(
        [r for r in records if r.decision == "BLOCK"],
        key=lambda r: r.ts_ns,
        reverse=True,
    )[:5]
    return DashboardStats(
        window=win, cost=cost, perf=perf, security=sec,
        recommendations=recs[:5],
        recent_blocks=blocks,
        record_count=len(records),
        is_demo=is_demo,
    )


# ── panel builders ──────────────────────────────────────────────


def _cost_panel(cost: CostStats, demo: bool) -> Panel:
    """💰 Cost panel — total + top provider."""
    total_usd = cost.total_usd
    table = Table.grid(padding=(0, 1))
    table.add_row(
        Text(f"${total_usd:>6.2f}", style="bold yellow"),
        Text("total", style="dim"),
    )
    if cost.by_provider:
        top = cost.by_provider[0]  # already sorted desc
        share = (
            top.total_usd / total_usd * 100.0
            if total_usd > 0 else 0.0
        )
        table.add_row(
            Text(_short_provider(top.provider), style="cyan"),
            Text(f"{share:.0f}%", style="dim"),
        )
    n_priced = cost.n_priced
    n_unpriced = cost.n_unpriced
    table.add_row(
        Text(f"{n_priced:>5,}", style="bold"),
        Text(f"priced ({n_unpriced} unpriced)", style="dim"),
    )
    return Panel(
        table,
        title="💰 Cost",
        title_align="left",
        border_style="yellow" if not demo else "dim yellow",
    )


def _perf_panel(perf: PerformanceStats, demo: bool) -> Panel:
    table = Table.grid(padding=(0, 1))
    p95 = perf.overall.p95
    p95_color = (
        "green" if p95 < 50.0 else "yellow" if p95 < 100.0 else "red"
    )
    table.add_row(
        Text(f"{p95:>5.0f}ms", style=f"bold {p95_color}"),
        Text("p95", style="dim"),
    )
    table.add_row(
        Text(f"{perf.overall.p50:>5.0f}ms", style="bold"),
        Text("p50", style="dim"),
    )
    if perf.by_tool:
        slowest = perf.by_tool[0]  # already sorted by p95 desc
        table.add_row(
            Text(slowest.tool[:10], style="cyan"),
            Text(f"{slowest.p95:.0f}ms", style="dim"),
        )
    return Panel(
        table,
        title="⚡ Performance",
        title_align="left",
        border_style="cyan" if not demo else "dim cyan",
    )


def _security_panel(
    sec: SecurityStats, window: WindowSummary, demo: bool,
) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_row(
        Text(f"{window.n_allow:>5,}", style="bold green"),
        Text("ALLOW", style="dim"),
    )
    table.add_row(
        Text(f"{sec.n_approval:>5,}", style="bold yellow"),
        Text("APPROVAL", style="dim"),
    )
    block_color = "bold red" if sec.n_block > 0 else "dim"
    table.add_row(
        Text(f"{sec.n_block:>5,}", style=block_color),
        Text("BLOCK", style="dim"),
    )
    return Panel(
        table,
        title="🛡️ Security",
        title_align="left",
        border_style="red" if sec.n_block > 0 else "green",
    )


def _recommendations_panel(recs: list[Recommendation]) -> Panel:
    if not recs:
        body: Any = Text("(no advisor recommendations)", style="dim italic")
        return Panel(
            body, title="💡 Advisor Recommendations",
            title_align="left", border_style="magenta",
        )
    table = Table.grid(padding=(0, 1))
    table.add_column(width=8, no_wrap=True)
    table.add_column(overflow="ellipsis")
    for r in recs:
        label = f"[{r.priority.upper()}]"
        style = {
            "high": "bold red",
            "medium": "yellow",
            "low": "dim cyan",
            "info": "dim",
        }.get(r.priority, "white")
        table.add_row(
            Text(label, style=style),
            Text(f"{r.headline}  ", style="bold")
            + Text(r.action, style="white"),
        )
    return Panel(
        table, title="💡 Advisor Recommendations",
        title_align="left", border_style="magenta",
    )


def _blocks_panel(blocks: list[ContextMemoryRecord]) -> Panel:
    if not blocks:
        body: Any = Text("(no BLOCKs in window)", style="dim italic green")
        return Panel(
            body, title="🚫 Recent BLOCKs",
            title_align="left", border_style="dim",
        )
    table = Table.grid(padding=(0, 1))
    table.add_column(width=8, no_wrap=True, style="dim")
    table.add_column(width=14, no_wrap=True)
    table.add_column(width=10, no_wrap=True, style="dim")
    table.add_column(overflow="ellipsis")
    for r in blocks:
        when = datetime.fromtimestamp(
            r.ts_ns / 1e9, tz=UTC,
        ).strftime("%H:%M:%S")
        trace_short = r.trace_id[:8] + "…" if len(r.trace_id) > 9 else r.trace_id
        table.add_row(
            when, Text(r.tool_name, style="cyan"),
            trace_short, Text(r.reason[:60], style="white"),
        )
    return Panel(
        table, title="🚫 Recent BLOCKs (last 24h)",
        title_align="left", border_style="red",
    )


def _header_text(stats: DashboardStats) -> Panel:
    health = "🟢 healthy"
    if stats.security.n_block > 0:
        health = "🔴 attention"
    elif stats.security.n_approval > stats.window.n_allow * 0.05:
        health = "🟡 review"
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    line = Text.assemble(
        ("Aegis Console", "bold cyan"),
        " · ",
        (now, "white"),
        " · ",
        (health, "bold"),
        " · ",
        (f"{stats.record_count} records", "dim"),
    )
    if stats.is_demo:
        line.append("  · ", style="dim")
        line.append("DEMO DATA", style="bold yellow on dark_red")
    return Panel(line, border_style="cyan")


def _footer_text() -> Panel:
    body = Text.assemble(
        ("[Ctrl-C]", "bold"), " exit  · ",
        ("[r]", "bold"), "efresh  · ",
        ("aegis", "cyan"), " doctor / rule / forensic / verify-audit for deep dive",
        style="dim",
    )
    return Panel(body, border_style="dim")


# ── full layout ─────────────────────────────────────────────────


def build_layout(stats: DashboardStats) -> Layout:
    """Compose the rich Layout for one frame."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="stats", size=8),
        Layout(name="recommendations", size=9),
        Layout(name="blocks", size=9),
        Layout(name="footer", size=3),
    )
    layout["header"].update(_header_text(stats))
    stats_row = Layout()
    stats_row.split_row(
        Layout(_cost_panel(stats.cost, stats.is_demo), name="cost"),
        Layout(_perf_panel(stats.perf, stats.is_demo), name="perf"),
        Layout(_security_panel(
            stats.security, stats.window, stats.is_demo,
        ), name="sec"),
    )
    layout["stats"].update(stats_row)
    layout["recommendations"].update(
        _recommendations_panel(stats.recommendations),
    )
    layout["blocks"].update(_blocks_panel(stats.recent_blocks))
    layout["footer"].update(_footer_text())
    return layout


# ── runner ───────────────────────────────────────────────────────


def run_dashboard(
    *,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    since_hours: float = DEFAULT_WINDOW_HOURS,
    context_memory_path: Path | None = None,
    demo: bool = False,
    console: Console | None = None,
    max_frames: int | None = None,
) -> int:
    """Run the live-refreshing dashboard until Ctrl-C.

    Parameters mostly mirror CLI flags. ``max_frames`` is for tests —
    when set, the loop exits after that many ticks instead of running
    forever. Production callers leave it ``None``.
    """
    console = console or Console()
    frames = 0
    try:
        stats = collect_stats(
            since_hours=since_hours,
            context_memory_path=context_memory_path,
            demo=demo,
        )
        with Live(
            build_layout(stats),
            console=console,
            refresh_per_second=max(1.0, 1.0 / refresh_seconds),
            screen=False,  # don't take over alt-buffer for v1 simplicity
        ) as live:
            while True:
                time.sleep(refresh_seconds)
                stats = collect_stats(
                    since_hours=since_hours,
                    context_memory_path=context_memory_path,
                    demo=demo,
                )
                live.update(build_layout(stats))
                frames += 1
                if max_frames is not None and frames >= max_frames:
                    break
        return 0
    except KeyboardInterrupt:
        console.print()
        console.print("[dim]exited dashboard.[/dim]")
        return 0


# ── helpers ─────────────────────────────────────────────────────


def _short_provider(provider: str) -> str:
    """Compress noisy provider strings for the cost panel.

    ``openrouter:anthropic-claude-sonnet-4`` → ``OR:claude-sonnet-4``
    ``anthropic-claude-3-5``                  → ``Claude``
    ``openai-gpt-4o``                         → ``GPT-4o``
    """
    if provider.startswith("openrouter:"):
        rest = provider.removeprefix("openrouter:")
        parts = rest.split("-", 1)
        return f"OR:{parts[1] if len(parts) > 1 else parts[0]}"[:16]
    if "claude" in provider.lower():
        return "Claude"
    if "openai" in provider.lower() or "gpt" in provider.lower():
        return "GPT"
    if "gemini" in provider.lower() or "google" in provider.lower():
        return "Gemini"
    if "llama" in provider.lower():
        return "Llama"
    return provider[:12]


def _demo_records() -> Iterable[ContextMemoryRecord]:
    """Synthetic demo records — used when ContextMemory is empty so
    a fresh install still sees a meaningful screen.

    These are deterministic for screencast/demo reproducibility.
    """
    base_ns = int(time.time() * 1_000_000_000)
    minute = 60_000_000_000
    samples: list[tuple[str, str, float, float, int, int, str, str, dict[str, str]]] = [
        # (tool, decision, latency_ms, cost_usd, tok_in, tok_out,
        #  trace_id, reason, step_traces)
        ("Bash", "ALLOW", 12.0, 0.18, 1200, 500, "demo-trace-001",
         "", {"step310": "safe"}),
        ("Bash", "ALLOW", 18.0, 0.24, 2200, 800, "demo-trace-002",
         "", {"step310": "safe"}),
        ("Read", "ALLOW", 8.0, 0.05, 800, 300, "demo-trace-003",
         "", {"step310": "safe"}),
        ("Edit", "ALLOW", 45.0, 0.42, 3200, 2400, "demo-trace-004",
         "", {"step310": "safe", "step340": "sLLM low-risk"}),
        ("Bash", "BLOCK", 47.0, 0.31, 1800, 0, "demo-trace-005",
         "dangerous pattern: destructive command on production path",
         {"step310": "matched destructive_bash", "step311": "regex hit"}),
        ("Bash", "REQUIRE_APPROVAL", 62.0, 0.56, 2600, 900,
         "demo-trace-006",
         "loop detected — same call 5x in last hour",
         {"step336": "loop-detector hit", "step340": "low confidence"}),
        ("Edit", "ALLOW", 38.0, 0.38, 2900, 2000, "demo-trace-007",
         "", {"step340": "sLLM low-risk"}),
        ("Bash", "ALLOW", 22.0, 0.12, 1400, 600, "demo-trace-008",
         "", {"step310": "safe"}),
    ]
    for i, (
        tool, decision, lat_ms, cost, tin, tout, trace, reason, traces,
    ) in enumerate(samples):
        yield ContextMemoryRecord(
            schema_version=1,
            ts_ns=base_ns - (len(samples) - i) * minute,
            trace_id=trace,
            invocation_id=f"demo-inv-{i:03d}",
            aid="demo-agent",
            tenant_id="demo-tenant",
            tool_name=tool,
            decision=decision,
            reason=reason,
            channel="cli",
            provider="anthropic-claude-sonnet-4",
            latency_ms=lat_ms,
            cost_usd=cost,
            tokens_in=tin,
            tokens_out=tout,
            step_traces=traces,
            m13_score=None,
            advisor_invoked=False,
            recommended_advisors=(),
            atv_sha3=None,
            atv_dim=2080,
            is_sidechain=False,
            mode="local",
        )
