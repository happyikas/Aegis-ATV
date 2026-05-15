"""ContextMemory analytics — pure functions over record lists.

All three aggregations (cost / performance / security) take a list
of :class:`ContextMemoryRecord` and return a dataclass of stats.
The renderer (:mod:`aegis.context_memory.report`) and the advisor
(:mod:`aegis.context_memory.advisor`) consume those stats — never
the raw records.

This separation matches the silicon roadmap: the eventual CXL/CSD
device runs the aggregation in storage and returns only the
condensed stats blob over the bus. Keeping the Python boundary at
the same shape lets us swap implementations without changing
callers.

All functions are pure — no I/O, no global state. Time complexity
is O(n) over the input list; memory is O(distinct keys).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from aegis.context_memory.record import ContextMemoryRecord

# ── window summary ───────────────────────────────────────────────


@dataclass(frozen=True)
class WindowSummary:
    """Top-of-report numbers — answer "what happened in this window"."""

    n_total: int
    n_allow: int
    n_approval: int
    n_block: int
    first_ts_ns: int
    last_ts_ns: int

    @property
    def block_rate(self) -> float:
        return self.n_block / self.n_total if self.n_total else 0.0

    @property
    def approval_rate(self) -> float:
        return self.n_approval / self.n_total if self.n_total else 0.0

    @property
    def allow_rate(self) -> float:
        return self.n_allow / self.n_total if self.n_total else 0.0

    @property
    def span_seconds(self) -> float:
        if not self.first_ts_ns or not self.last_ts_ns:
            return 0.0
        return max((self.last_ts_ns - self.first_ts_ns) / 1e9, 0.0)


def window_summary(records: Iterable[ContextMemoryRecord]) -> WindowSummary:
    recs = list(records)
    if not recs:
        return WindowSummary(0, 0, 0, 0, 0, 0)
    n_allow = sum(1 for r in recs if r.decision == "ALLOW")
    n_block = sum(1 for r in recs if r.decision == "BLOCK")
    n_approval = sum(1 for r in recs if r.decision == "REQUIRE_APPROVAL")
    return WindowSummary(
        n_total=len(recs),
        n_allow=n_allow,
        n_approval=n_approval,
        n_block=n_block,
        first_ts_ns=min(r.ts_ns for r in recs if r.ts_ns) or 0,
        last_ts_ns=max(r.ts_ns for r in recs),
    )


# ── cost ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderCost:
    provider: str
    n: int
    total_usd: float

    @property
    def mean_usd(self) -> float:
        return self.total_usd / self.n if self.n else 0.0


@dataclass(frozen=True)
class CostStats:
    total_usd: float
    n_priced: int            # records with cost_usd > 0
    n_unpriced: int          # records where we have no cost attribution
    by_provider: tuple[ProviderCost, ...]  # sorted by total_usd desc
    by_aid: tuple[tuple[str, float], ...]  # (aid, total_usd) desc, top 10
    top_expensive_traces: tuple[tuple[str, float, str], ...]  # (trace, $, tool)


def cost_stats(records: Iterable[ContextMemoryRecord]) -> CostStats:
    by_provider_acc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0.0, "total_usd": 0.0},
    )
    by_aid_acc: dict[str, float] = defaultdict(float)
    expensive: list[tuple[str, float, str]] = []
    total = 0.0
    n_priced = 0
    n_unpriced = 0
    for r in records:
        if r.cost_usd > 0:
            total += r.cost_usd
            n_priced += 1
            prov = r.provider or "(no-provider)"
            by_provider_acc[prov]["n"] += 1
            by_provider_acc[prov]["total_usd"] += r.cost_usd
            if r.aid:
                by_aid_acc[r.aid] += r.cost_usd
            expensive.append((r.trace_id, r.cost_usd, r.tool_name))
        else:
            n_unpriced += 1
    by_provider = tuple(
        sorted(
            (
                ProviderCost(
                    provider=p, n=int(d["n"]), total_usd=d["total_usd"],
                )
                for p, d in by_provider_acc.items()
            ),
            key=lambda x: x.total_usd,
            reverse=True,
        )
    )
    by_aid = tuple(
        sorted(by_aid_acc.items(), key=lambda kv: kv[1], reverse=True)[:10]
    )
    expensive.sort(key=lambda t: t[1], reverse=True)
    top_traces = tuple(expensive[:5])
    return CostStats(
        total_usd=total,
        n_priced=n_priced,
        n_unpriced=n_unpriced,
        by_provider=by_provider,
        by_aid=by_aid,
        top_expensive_traces=top_traces,
    )


# ── performance ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LatencyPercentiles:
    p50: float
    p95: float
    p99: float
    max: float
    n: int


@dataclass(frozen=True)
class ToolLatency:
    tool: str
    p50: float
    p95: float
    n: int


@dataclass(frozen=True)
class PerformanceStats:
    overall: LatencyPercentiles
    by_tool: tuple[ToolLatency, ...]    # sorted by p95 desc
    slowest_traces: tuple[tuple[str, float, str], ...]   # (trace, ms, tool)


def performance_stats(
    records: Iterable[ContextMemoryRecord],
) -> PerformanceStats:
    latencies: list[float] = []
    by_tool_lat: dict[str, list[float]] = defaultdict(list)
    slowest: list[tuple[str, float, str]] = []
    for r in records:
        if r.latency_ms <= 0:
            continue
        latencies.append(r.latency_ms)
        if r.tool_name:
            by_tool_lat[r.tool_name].append(r.latency_ms)
        slowest.append((r.trace_id, r.latency_ms, r.tool_name))
    overall = _percentiles(latencies)
    by_tool = tuple(
        sorted(
            (
                ToolLatency(
                    tool=t,
                    p50=_percentile(lats, 50.0),
                    p95=_percentile(lats, 95.0),
                    n=len(lats),
                )
                for t, lats in by_tool_lat.items()
            ),
            key=lambda x: x.p95,
            reverse=True,
        )
    )
    slowest.sort(key=lambda t: t[1], reverse=True)
    return PerformanceStats(
        overall=overall, by_tool=by_tool,
        slowest_traces=tuple(slowest[:5]),
    )


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (no numpy dependency)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _percentiles(values: list[float]) -> LatencyPercentiles:
    if not values:
        return LatencyPercentiles(0.0, 0.0, 0.0, 0.0, 0)
    return LatencyPercentiles(
        p50=_percentile(values, 50.0),
        p95=_percentile(values, 95.0),
        p99=_percentile(values, 99.0),
        max=max(values),
        n=len(values),
    )


# ── security ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepCount:
    step: str
    count: int


@dataclass(frozen=True)
class ProviderRisk:
    provider: str
    n: int
    n_block: int

    @property
    def block_rate(self) -> float:
        return self.n_block / self.n if self.n else 0.0


@dataclass(frozen=True)
class SecurityStats:
    n_block: int
    n_approval: int
    block_rate: float
    approval_rate: float
    block_by_step: tuple[StepCount, ...]          # sorted desc
    approval_by_step: tuple[StepCount, ...]        # sorted desc
    by_provider: tuple[ProviderRisk, ...]          # sorted by block_rate desc
    top_block_traces: tuple[
        tuple[str, str, str], ...
    ] = field(default_factory=tuple)  # (trace, tool, reason)


def security_stats(
    records: Iterable[ContextMemoryRecord],
) -> SecurityStats:
    block_steps: Counter[str] = Counter()
    approval_steps: Counter[str] = Counter()
    per_provider: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "n_block": 0},
    )
    top_blocks: list[tuple[str, str, str]] = []
    n_block = n_approval = n_total = 0
    for r in records:
        n_total += 1
        prov = r.provider or "(no-provider)"
        per_provider[prov]["n"] += 1
        if r.decision == "BLOCK":
            n_block += 1
            per_provider[prov]["n_block"] += 1
            for step in r.step_traces:
                block_steps[step] += 1
            top_blocks.append((r.trace_id, r.tool_name, r.reason))
        elif r.decision == "REQUIRE_APPROVAL":
            n_approval += 1
            for step in r.step_traces:
                approval_steps[step] += 1
    by_provider = tuple(
        sorted(
            (
                ProviderRisk(provider=p, n=d["n"], n_block=d["n_block"])
                for p, d in per_provider.items()
            ),
            key=lambda x: (x.block_rate, x.n),
            reverse=True,
        )
    )
    return SecurityStats(
        n_block=n_block,
        n_approval=n_approval,
        block_rate=n_block / n_total if n_total else 0.0,
        approval_rate=n_approval / n_total if n_total else 0.0,
        block_by_step=tuple(
            StepCount(step=s, count=c) for s, c in block_steps.most_common()
        ),
        approval_by_step=tuple(
            StepCount(step=s, count=c)
            for s, c in approval_steps.most_common()
        ),
        by_provider=by_provider,
        top_block_traces=tuple(top_blocks[:5]),
    )


__all__ = [
    "CostStats",
    "LatencyPercentiles",
    "PerformanceStats",
    "ProviderCost",
    "ProviderRisk",
    "SecurityStats",
    "StepCount",
    "ToolLatency",
    "WindowSummary",
    "cost_stats",
    "performance_stats",
    "security_stats",
    "window_summary",
]
