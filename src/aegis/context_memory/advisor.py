"""ContextMemory advisor — heuristic optimisation recommendations.

Reads :class:`CostStats` / :class:`PerformanceStats` /
:class:`SecurityStats` (the condensed analytics shape — not the raw
records) and produces a list of :class:`Recommendation` per
category.

Why heuristics, not the sLLM advisor?
-------------------------------------
The sLLM advisor (8-advisor pipeline in ``src/aegis/judge/advisor.py``)
operates per-call — it reads one ATV and proposes next-step verbs.
ContextMemory advice is **window-level** — it reads thousands of
ATVs and proposes operational adjustments. Different time scale,
different decision target. Cleanly heuristic so the rules are
auditable and CI-testable.

Each :class:`Recommendation` carries a priority (high/medium/low),
a short headline, a concrete action verb, and a one-line
explanation. The markdown report renders these as a bullet list per
category.

Threshold constants are tuned against the patent's PitchDeck
claims — < 50ms p95 latency, ~0.5% baseline block rate, ~3× drift
trigger from the divergence advisor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aegis.context_memory.analytics import (
    CostStats,
    PerformanceStats,
    SecurityStats,
)

# ── thresholds (audit-friendly constants) ────────────────────────

# Performance
P95_TARGET_MS = 50.0           # PitchDeck claim
P99_DEGRADED_MS = 500.0        # something is very wrong
TOOL_LATENCY_OUTLIER_RATIO = 3.0    # tool p95 vs median tool p95

# Cost
PROVIDER_DOMINANCE_PCT = 0.70  # one provider takes ≥70% of cost
UNPRICED_RATIO_ALERT = 0.30    # >30% records have no cost attribution
SINGLE_TRACE_DOLLAR_ALERT = 0.50  # one trace > $0.50 is unusual

# Security
BLOCK_RATE_HIGH = 0.05         # >5% blocks = elevated
BLOCK_RATE_BASELINE_HIGH = 0.01  # >1% blocks = "above baseline"
PROVIDER_BLOCK_DRIFT_RATIO = 3.0  # provider-drift advisor threshold
DOMINANT_STEP_PCT = 0.60       # one step >60% of blocks


# ── output shape ─────────────────────────────────────────────────


Priority = Literal["high", "medium", "low", "info"]


@dataclass(frozen=True)
class Recommendation:
    priority: Priority
    headline: str
    action: str
    explanation: str

    @property
    def emoji(self) -> str:
        return {
            "high": "🔴",
            "medium": "🟡",
            "low": "🟢",
            "info": "💡",
        }.get(self.priority, "•")


# ── cost ─────────────────────────────────────────────────────────


def cost_advice(stats: CostStats) -> list[Recommendation]:
    out: list[Recommendation] = []

    # Zero-data short-circuit
    if stats.n_priced == 0 and stats.n_unpriced == 0:
        return [Recommendation(
            "info",
            "윈도우 내 데이터 없음",
            "더 긴 --since 옵션으로 재실행",
            "ContextMemory 가 비어있거나 모든 레코드가 윈도우 밖",
        )]

    # Provider dominance
    if stats.by_provider and stats.total_usd > 0:
        top = stats.by_provider[0]
        share = top.total_usd / stats.total_usd
        if share >= PROVIDER_DOMINANCE_PCT:
            out.append(Recommendation(
                "high",
                f"{top.provider} 가 비용의 {share*100:.0f}% 차지",
                "`aegis report --by-provider --since 7d` 로 상세 확인 후 "
                "저비용 provider 로 일부 라우팅 검토",
                f"호출 {top.n}회, 총 ${top.total_usd:.3f}. "
                f"평균 ${top.mean_usd:.4f}/call.",
            ))

    # Unpriced-record dominance — usually means cost attribution
    # ledger is detached. Surface as a setup issue.
    total_records = stats.n_priced + stats.n_unpriced
    if total_records >= 50:
        unpriced_pct = stats.n_unpriced / total_records
        if unpriced_pct >= UNPRICED_RATIO_ALERT:
            out.append(Recommendation(
                "medium",
                f"비용 미부여 레코드 {unpriced_pct*100:.0f}%",
                "transcript 자동 import 또는 `aegis cost-import transcript` "
                "수동 실행 검토",
                f"{stats.n_unpriced} 개 레코드에 cost 정보 없음. "
                f"운영자가 cost attribution ledger 를 켜지 않은 상태.",
            ))

    # Single-trace high-cost outlier
    if stats.top_expensive_traces:
        trace_id, usd, tool = stats.top_expensive_traces[0]
        if usd >= SINGLE_TRACE_DOLLAR_ALERT:
            out.append(Recommendation(
                "medium",
                f"단일 호출 ${usd:.2f} (도구: {tool})",
                f"`aegis forensic show {trace_id[:12]}` 로 어떤 prompt 가 "
                f"이 비용을 발생시켰는지 확인",
                "한 번의 호출이 단일 트랜잭션 한도($0.50)를 초과한 사례. "
                "context window / loop / inefficient prompt 가 흔한 원인.",
            ))

    # Healthy state
    if not out:
        out.append(Recommendation(
            "info",
            "비용 패턴 안정",
            "조치 불필요",
            f"총 ${stats.total_usd:.3f}, {stats.n_priced} 호출. "
            "Provider 분산 양호, 단일 outlier 없음.",
        ))
    return out


# ── performance ──────────────────────────────────────────────────


def performance_advice(stats: PerformanceStats) -> list[Recommendation]:
    out: list[Recommendation] = []

    if stats.overall.n == 0:
        return [Recommendation(
            "info",
            "윈도우 내 latency 데이터 없음",
            "더 긴 --since 옵션으로 재실행",
            "ContextMemory 가 비어있거나 모든 레코드의 latency_ms == 0",
        )]

    # PitchDeck < 50ms p95 contract
    if stats.overall.p95 > P95_TARGET_MS:
        sev: Priority = (
            "high" if stats.overall.p99 > P99_DEGRADED_MS else "medium"
        )
        out.append(Recommendation(
            sev,
            f"p95 latency {stats.overall.p95:.0f} ms — 목표 < 50 ms 초과",
            "sLLM judge cold start / RAG corpus size / step340 lazy "
            "loading 확인. 필요 시 `AEGIS_JUDGE_PROVIDER=attribution_head` "
            "로 fast-path 사용",
            f"p50={stats.overall.p50:.1f} ms, p99={stats.overall.p99:.1f} ms, "
            f"max={stats.overall.max:.0f} ms, n={stats.overall.n}",
        ))
    else:
        out.append(Recommendation(
            "low",
            f"p95 latency {stats.overall.p95:.1f} ms — 목표 충족 ✓",
            "조치 불필요",
            f"PitchDeck 의 < 50 ms p95 약속 충족 (현재 "
            f"p95={stats.overall.p95:.1f} ms)",
        ))

    # Per-tool outlier (one tool's p95 ≫ median)
    if len(stats.by_tool) >= 3:
        p95s = sorted(t.p95 for t in stats.by_tool)
        median_p95 = p95s[len(p95s) // 2]
        slowest = stats.by_tool[0]
        if median_p95 > 0 and slowest.p95 / median_p95 >= TOOL_LATENCY_OUTLIER_RATIO:
            out.append(Recommendation(
                "medium",
                f"{slowest.tool} 도구가 median 대비 "
                f"{slowest.p95/median_p95:.1f}× 느림",
                f"`aegis advise` 로 {slowest.tool} 호출 시 호출 패턴 권고 "
                "확인. 잦은 fail+retry 또는 큰 args 가 흔한 원인",
                f"{slowest.tool} p95={slowest.p95:.0f} ms (median tool "
                f"p95={median_p95:.0f} ms), n={slowest.n}",
            ))

    # Single super-slow trace
    if stats.slowest_traces:
        trace_id, ms, tool = stats.slowest_traces[0]
        if ms > P99_DEGRADED_MS:
            out.append(Recommendation(
                "high",
                f"단일 호출 {ms:.0f} ms 발생 (도구: {tool})",
                f"`aegis forensic show {trace_id[:12]}` — 사용자가 멈춤 "
                "체감했을 가능성",
                "단일 트랜잭션이 500 ms 초과. firewall 안에서 발생한 stall "
                "또는 outside dependency 지연이 흔한 원인",
            ))

    return out


# ── security ─────────────────────────────────────────────────────


def security_advice(stats: SecurityStats) -> list[Recommendation]:
    out: list[Recommendation] = []
    n_total = stats.n_block + stats.n_approval + 0  # n_total computed elsewhere

    # Need the total to talk about rates — recover from block_rate
    if stats.block_rate > 0 and stats.n_block > 0:
        # Inverse: n_block / block_rate = n_total (approx)
        n_total = round(stats.n_block / stats.block_rate)

    if stats.block_rate >= BLOCK_RATE_HIGH:
        out.append(Recommendation(
            "high",
            f"BLOCK rate {stats.block_rate*100:.1f}% — 비정상 수준",
            "`aegis forensic last` 로 가장 최근 BLOCK 케이스부터 검토. "
            "악성 / 손상된 prompt 가 firewall 까지 도달한 경우 가능",
            f"{stats.n_block} 차단 / {n_total or '?'} 총 호출. "
            f"baseline (~0.5%) 의 {stats.block_rate*100/0.5:.1f}× 수준.",
        ))
    elif stats.block_rate >= BLOCK_RATE_BASELINE_HIGH:
        out.append(Recommendation(
            "medium",
            f"BLOCK rate {stats.block_rate*100:.1f}% — baseline 초과",
            "주간 검토 권장 — 한 번 발생한 사건은 우연, 계속되면 패턴",
            f"{stats.n_block} 차단 / {n_total or '?'} 총 호출. "
            "baseline 0.3-1.0% 의 상단을 초과.",
        ))
    else:
        out.append(Recommendation(
            "low",
            f"BLOCK rate {stats.block_rate*100:.2f}% — baseline 안",
            "조치 불필요",
            f"{stats.n_block} 차단. baseline (0.3-1.0%) 안.",
        ))

    # Dominant step
    if stats.n_block >= 3 and stats.block_by_step:
        top_step = stats.block_by_step[0]
        share = top_step.count / stats.n_block
        if share >= DOMINANT_STEP_PCT:
            out.append(Recommendation(
                "medium",
                f"{top_step.step} 단계가 BLOCK 의 "
                f"{share*100:.0f}% 차지",
                "`policies/safe_actions.json` 또는 step regex 검토 — "
                "정상 작업이 false-positive 로 잡혀있을 수 있음",
                f"{top_step.step}: {top_step.count} / {stats.n_block} 총 BLOCK",
            ))

    # Provider drift (PitchDeck 의 provider-drift advisor 매칭)
    real_providers = [
        p for p in stats.by_provider if p.provider != "(no-provider)"
    ]
    if len(real_providers) >= 2:
        rates = [p.block_rate for p in real_providers if p.n >= 10]
        if len(rates) >= 2:
            rates.sort()
            median_rate = rates[len(rates) // 2]
            if median_rate > 0:
                worst = max(real_providers, key=lambda x: x.block_rate)
                if (
                    worst.n >= 10
                    and worst.block_rate / median_rate
                    >= PROVIDER_BLOCK_DRIFT_RATIO
                ):
                    out.append(Recommendation(
                        "high",
                        f"{worst.provider} 의 BLOCK rate 가 cross-provider "
                        f"median 의 {worst.block_rate/median_rate:.1f}× — "
                        "Provider drift 의심",
                        "`aegis report --by-aid-and-provider` 로 정량 비교. "
                        "RLHF / safety 튜닝 차이가 정상 호출까지 차단 가능",
                        f"{worst.provider}: {worst.block_rate*100:.1f}% "
                        f"({worst.n_block}/{worst.n}). median: "
                        f"{median_rate*100:.1f}%",
                    ))

    # Top approval — surfaces user-facing friction
    if stats.approval_rate >= 0.10 and stats.n_approval >= 5:
        out.append(Recommendation(
            "medium",
            f"REQUIRE_APPROVAL rate {stats.approval_rate*100:.0f}% — "
            "사용자 마찰 가능",
            "Coach baseline 재학습 권장 — `aegis burnin train-m13`. "
            "정상 패턴 학습이 부족해 grey-zone 이 과대",
            f"{stats.n_approval} 승인 요청. >10% 면 사용자가 "
            "interrupt 빈도가 높음.",
        ))

    return out


__all__ = [
    "BLOCK_RATE_BASELINE_HIGH",
    "BLOCK_RATE_HIGH",
    "DOMINANT_STEP_PCT",
    "P95_TARGET_MS",
    "P99_DEGRADED_MS",
    "PROVIDER_BLOCK_DRIFT_RATIO",
    "PROVIDER_DOMINANCE_PCT",
    "Priority",
    "Recommendation",
    "SINGLE_TRACE_DOLLAR_ALERT",
    "TOOL_LATENCY_OUTLIER_RATIO",
    "UNPRICED_RATIO_ALERT",
    "cost_advice",
    "performance_advice",
    "security_advice",
]
