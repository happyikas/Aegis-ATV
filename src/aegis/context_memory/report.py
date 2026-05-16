"""ContextMemory markdown report renderer.

Composes :class:`WindowSummary` + :class:`CostStats` +
:class:`PerformanceStats` + :class:`SecurityStats` + their
recommendation lists into a single markdown document.

The output is meant to be:

* Readable in any text viewer (no special markdown extensions)
* Pasted into Slack / GitHub / email without re-formatting
* Diffable in git when redirected to a file (deterministic)
* Streamed to stdout as the default ``aegis doctor`` mode

The renderer is pure — given the same stats dataclasses it emits
identical bytes, modulo the embedded "Generated at" timestamp.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

# v0.5.14: import the autonomy outlier walker so `aegis doctor`
# can surface auto-approval postmortems alongside its other
# sections. The outliers module has zero side-effects at import
# time and only depends on aegis.context_memory.record, so the
# import is safe to keep at module top despite the cross-package
# dependency.
from aegis.autonomy.outliers import OutlierEvent, detect_outliers
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
from aegis.context_memory.record import ContextMemoryRecord


def render_doctor_report(
    records: list[ContextMemoryRecord],
    *,
    since_seconds: int | None = None,
    generated_at: _dt.datetime | None = None,
    context_memory_path_str: str = "",
) -> str:
    """Compose the full markdown report.

    Parameters
    ----------
    records:
        ContextMemory records in the window. Caller filters by time;
        this function does not.
    since_seconds:
        Window size in seconds, for the "기간" line. ``None`` => the
        renderer infers from the first/last record timestamps.
    generated_at:
        UTC timestamp shown in the footer. Defaults to ``utcnow()``.
    context_memory_path_str:
        Path string for the footer. Default empty (omits).
    """
    if generated_at is None:
        generated_at = _dt.datetime.now(_dt.UTC)

    summary = window_summary(records)
    c_stats = cost_stats(records)
    p_stats = performance_stats(records)
    s_stats = security_stats(records)
    a_stats = autonomy_stats(records)

    parts: list[str] = []
    parts.append(_header(summary, since_seconds))
    parts.append(_summary_section(summary))
    parts.append(_cost_section(c_stats, cost_advice(c_stats)))
    parts.append(_performance_section(p_stats, performance_advice(p_stats)))
    parts.append(_security_section(s_stats, security_advice(s_stats)))
    parts.append(_autonomy_section(a_stats))
    parts.append(_next_actions(c_stats, p_stats, s_stats))
    parts.append(_footer(summary, generated_at, context_memory_path_str))

    return "\n\n".join(parts).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────────
# Autonomy section — v0.5.14 doctor postmortem integration
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AutonomyStats:
    """Snapshot of autonomy activity in a doctor window.

    ``n_bypass``  — records carrying the step331.run stamp
                    (REQUIRE_APPROVAL auto-approved by trust)
    ``n_explore`` — records carrying the step331.explore stamp
                    (trusted pattern matched but ε-greedy forced
                    the human back into the loop)
    ``outliers``  — auto-approvals followed by a BLOCK within
                    the lookahead window
    ``n_records`` — total records in the window (denominator)
    """

    n_bypass: int = 0
    n_explore: int = 0
    outliers: tuple[OutlierEvent, ...] = field(default_factory=tuple)
    n_records: int = 0


def autonomy_stats(records: list[ContextMemoryRecord]) -> AutonomyStats:
    """Compute the autonomy snapshot for the window.

    Pure function: deterministic given the same records, no env
    reads, no I/O. The doctor section calls this and renders the
    output; tests assert against the dataclass directly."""
    n_bypass = 0
    n_explore = 0
    for r in records:
        traces = r.step_traces or {}
        if "aegis.autonomy.step331.run" in traces:
            n_bypass += 1
        if "aegis.autonomy.step331.explore" in traces:
            n_explore += 1
    return AutonomyStats(
        n_bypass=n_bypass,
        n_explore=n_explore,
        outliers=tuple(detect_outliers(records)),
        n_records=len(records),
    )


def _autonomy_section(stats: AutonomyStats) -> str:
    """Render the autonomy section. Stays silent (single-line
    "no data") when the operator hasn't enabled autonomy — keeps
    the report concise for default deployments."""
    n_bypass = stats.n_bypass
    n_explore = stats.n_explore
    outliers = stats.outliers
    n_records = stats.n_records

    if n_bypass == 0 and n_explore == 0 and not outliers:
        return (
            "## 🤖 Autonomy (v0.5.13+)\n\n"
            "_(autonomy disabled or no bypass events in this window)_"
        )

    bypass_pct = (n_bypass / n_records * 100.0) if n_records > 0 else 0.0
    lines = [
        "## 🤖 Autonomy (v0.5.13+)",
        "",
        f"- **자동 승인**: {n_bypass:,} 건 "
        f"(전체의 {bypass_pct:.2f}%)",
        f"- **강제 탐색 (ε-greedy)**: {n_explore:,} 건",
    ]
    if outliers:
        lines.append(
            f"- **⚠️ Outliers**: {len(outliers)} 건 "
            f"(auto-approve 직후 BLOCK 발생)"
        )
        lines.append("")
        lines.append("| trace_id | tool | bypass signature | follow-up BLOCK |")
        lines.append("|---|---|---|---|")
        for ev in outliers[:5]:
            sig = (ev.bypass_stamp or "").split("signature=", 1)
            sig_text = sig[1].split(" ")[0] if len(sig) > 1 else "?"
            follow = ev.followup_block_reason or "(unknown)"
            if len(follow) > 60:
                follow = follow[:57] + "..."
            lines.append(
                f"| `{ev.trace_id[:16]}` | {ev.tool_name} | "
                f"{sig_text} | {follow} |"
            )
        if len(outliers) > 5:
            lines.append("")
            lines.append(f"_(+{len(outliers) - 5} more — see `aegis autonomy outliers`)_")
        lines.append("")
        lines.append(
            "**다음 액션**: 의심스러운 trace_id 에 대해 "
            "`aegis autonomy deny <trace_id>` 실행 → "
            "`aegis autonomy learn` 재실행으로 trust table 갱신."
        )
    else:
        lines.append("- **Outliers**: 0 건 — clean window")
    return "\n".join(lines)


# ── section renderers ────────────────────────────────────────────


def _header(summary: WindowSummary, since_seconds: int | None) -> str:
    if since_seconds is not None and since_seconds > 0:
        window_str = _humanise_duration(since_seconds)
    elif summary.span_seconds > 0:
        window_str = _humanise_duration(int(summary.span_seconds))
    else:
        window_str = "(전체 기간)"
    return f"# Aegis Doctor Report\n\n**기간**: {window_str}"


def _summary_section(summary: WindowSummary) -> str:
    if summary.n_total == 0:
        return (
            "## 📊 요약\n\n"
            "ContextMemory 가 비어있거나 윈도우 내 레코드가 없습니다.\n"
            "더 긴 `--since` 값으로 다시 실행하거나, "
            "`AEGIS_CONTEXT_MEMORY_PATH` 환경 변수를 확인하세요."
        )
    return (
        "## 📊 요약\n\n"
        f"- **총 ATV**: {summary.n_total:,}\n"
        f"- **Decision 분포**:\n"
        f"  - ALLOW: {summary.n_allow:,} ({summary.allow_rate*100:.1f}%)\n"
        f"  - REQUIRE_APPROVAL: {summary.n_approval:,} "
        f"({summary.approval_rate*100:.1f}%)\n"
        f"  - BLOCK: {summary.n_block:,} ({summary.block_rate*100:.2f}%)"
    )


def _cost_section(stats: CostStats, recs: list[Recommendation]) -> str:
    if stats.n_priced == 0 and stats.n_unpriced == 0:
        return "## 💰 Cost\n\n_(데이터 없음)_"
    lines = ["## 💰 Cost", "", "### 통계"]
    lines.append(f"- 총 비용: **${stats.total_usd:.4f}**")
    total = stats.n_priced + stats.n_unpriced
    lines.append(f"- 비용 부여 호출: {stats.n_priced:,} / {total:,}")
    if stats.by_provider:
        lines.extend(["", "**Provider 별 (비용 desc)**", ""])
        lines.append("| Provider | 호출 수 | 총 비용 | 평균/호출 |")
        lines.append("|---|---:|---:|---:|")
        for p in stats.by_provider[:6]:
            lines.append(
                f"| `{p.provider}` | {p.n:,} | ${p.total_usd:.4f} | "
                f"${p.mean_usd:.5f} |"
            )
    if stats.top_expensive_traces:
        lines.extend(["", "**비용 상위 5 호출**", ""])
        for trace_id, usd, tool in stats.top_expensive_traces:
            short = trace_id[:12] + "…" if len(trace_id) > 13 else trace_id
            lines.append(f"- `{short}`  ({tool})  —  **${usd:.4f}**")
    lines.append("")
    lines.append(_recommendations_section(recs))
    return "\n".join(lines)


def _performance_section(
    stats: PerformanceStats, recs: list[Recommendation],
) -> str:
    if stats.overall.n == 0:
        return "## ⚡ Performance\n\n_(latency 데이터 없음)_"
    lines = ["## ⚡ Performance", "", "### 통계"]
    lines.append(
        f"- Overall latency: "
        f"**p50 {stats.overall.p50:.1f} ms** · "
        f"**p95 {stats.overall.p95:.1f} ms** · "
        f"**p99 {stats.overall.p99:.1f} ms** · "
        f"max {stats.overall.max:.0f} ms · "
        f"n={stats.overall.n:,}"
    )
    if stats.by_tool:
        lines.extend(["", "**도구 별 (p95 desc)**", ""])
        lines.append("| 도구 | p50 | p95 | 호출 수 |")
        lines.append("|---|---:|---:|---:|")
        for t in stats.by_tool[:8]:
            lines.append(
                f"| `{t.tool}` | {t.p50:.1f} ms | {t.p95:.1f} ms | {t.n:,} |"
            )
    if stats.slowest_traces:
        lines.extend(["", "**가장 느린 호출 (top 5)**", ""])
        for trace_id, ms, tool in stats.slowest_traces:
            short = trace_id[:12] + "…" if len(trace_id) > 13 else trace_id
            lines.append(f"- `{short}`  ({tool})  —  **{ms:.0f} ms**")
    lines.append("")
    lines.append(_recommendations_section(recs))
    return "\n".join(lines)


def _security_section(
    stats: SecurityStats, recs: list[Recommendation],
) -> str:
    lines = ["## 🛡️ Security", "", "### 통계"]
    lines.append(
        f"- BLOCK rate: **{stats.block_rate*100:.2f}%** "
        f"({stats.n_block:,} 차단)"
    )
    lines.append(
        f"- REQUIRE_APPROVAL rate: **{stats.approval_rate*100:.2f}%** "
        f"({stats.n_approval:,} 승인 요청)"
    )
    if stats.block_by_step:
        lines.extend(["", "**BLOCK 원인 분포 (step 별)**", ""])
        lines.append("| Step | 횟수 |")
        lines.append("|---|---:|")
        for sc in stats.block_by_step[:8]:
            lines.append(f"| `{sc.step}` | {sc.count:,} |")
    if stats.by_provider and len(stats.by_provider) >= 2:
        lines.extend(["", "**Provider 별 위험도 (BLOCK rate desc)**", ""])
        lines.append("| Provider | 호출 수 | BLOCK | BLOCK rate |")
        lines.append("|---|---:|---:|---:|")
        for p in stats.by_provider[:6]:
            lines.append(
                f"| `{p.provider}` | {p.n:,} | {p.n_block:,} | "
                f"{p.block_rate*100:.2f}% |"
            )
    if stats.top_block_traces:
        lines.extend(["", "**최근 BLOCK 사례 (top 5)**", ""])
        for trace_id, tool, reason in stats.top_block_traces:
            short = trace_id[:12] + "…" if len(trace_id) > 13 else trace_id
            reason_short = (reason or "")[:80]
            lines.append(f"- `{short}`  ({tool})  —  {reason_short}")
    lines.append("")
    lines.append(_recommendations_section(recs))
    return "\n".join(lines)


def _next_actions(
    c: CostStats, p: PerformanceStats, s: SecurityStats,
) -> str:
    lines = ["## 📌 다음 액션", ""]
    lines.append(
        "1. **Cost** — `aegis report --by-provider --since 7d` 로 "
        "provider 별 상세 breakdown"
    )
    lines.append(
        "2. **Performance** — `aegis advise` 로 advisor 권고 종합 확인"
    )
    if s.top_block_traces:
        first_trace = s.top_block_traces[0][0]
        short = first_trace[:12] + "…" if len(first_trace) > 13 else first_trace
        lines.append(
            f"3. **Security** — `aegis forensic show {short}` 로 "
            "가장 최근 BLOCK 케이스 자세히"
        )
    else:
        lines.append(
            "3. **Security** — `aegis forensic last` 로 최근 BLOCK 케이스 자세히"
        )
    lines.append(
        "4. **감사 체인 검증** — `aegis verify-audit` 로 audit log 무결성 확인"
    )
    return "\n".join(lines)


def _footer(
    summary: WindowSummary,
    generated_at: _dt.datetime,
    context_memory_path_str: str,
) -> str:
    when = generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    path_part = f" from `{context_memory_path_str}`" if context_memory_path_str else ""
    return (
        "---\n\n"
        f"*Generated at {when} by `aegis doctor`{path_part} "
        f"({summary.n_total:,} records)*"
    )


# ── recommendation section ──────────────────────────────────────


def _recommendations_section(recs: list[Recommendation]) -> str:
    if not recs:
        return "### 권고\n\n_(없음)_"
    lines = ["### 권고", ""]
    for r in recs:
        lines.append(f"- {r.emoji} **{r.headline}**")
        lines.append(f"  - **action**: {r.action}")
        lines.append(f"  - {r.explanation}")
    return "\n".join(lines)


# ── duration humaniser ──────────────────────────────────────────


def _humanise_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"최근 {seconds} 초"
    if seconds < 3600:
        return f"최근 {seconds // 60} 분"
    if seconds < 86400:
        hours = seconds / 3600
        return f"최근 {hours:.1f} 시간"
    days = seconds / 86400
    return f"최근 {days:.1f} 일"


__all__ = ["AutonomyStats", "autonomy_stats", "render_doctor_report"]
