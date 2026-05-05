"""Aegis-ATV Token-Eviction Advisor Demo
=========================================

End-to-end runnable demo of the v4.3 ``eviction_advisor`` head.

What it shows
-------------

For each of N synthetic agent contexts (varying context_utilization
+ a synthetic per-token attention vector that mimics real LLM
attention shapes), runs the eviction advisor and:

* prints the recommended policy + parameters
* materialises the concrete ``evict_token_indices`` from the
  per-token attention vector
* simulates the resulting cache footprint and computes
  observed memory savings — with the cardinal rule "the kept
  subset must include every heavy-hitter position"
* compares against three baseline strategies:
    - **always-keep-all** (no eviction; safe but wastes memory)
    - **fixed sliding-window-128** (cheap default — naive)
    - **advisor-guided**

Why this works without a real runtime
-------------------------------------

Token eviction is implemented inside the LLM serving runtime
(vLLM, custom llama-cpp, etc.), and Anthropic's hosted API
doesn't expose per-token attention. So we *simulate* the kept
subset analytically: for each scenario we know which positions
are the heavy hitters; the cache footprint after eviction is
just len(kept) × bytes_per_token. The advisor's job is to pick a
policy that retains those heavy hitters; we verify it does.

Run
---

::

    uv run python demo/eviction_advisor_demo.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ruff: noqa: E402  -- imports follow sys.path bootstrap above.
from aegis.atv.builder import build_atv
from aegis.performance.eviction_advisor import (
    EvictionAdvice,
    eviction_advisor,
    summarise_attention,
)
from aegis.schema import (
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)

# TinyLlama-1.1B-Chat constants — matches PR #49 demo.
N_LAYER = 22
N_KV_HEAD = 4
HEAD_DIM = 64
BYTES_PER_KV_ELEMENT_F16 = 2.0


def kv_bytes_for(n_tokens: int) -> int:
    """Analytical KV-cache memory for ``n_tokens`` of TinyLlama @ F16."""
    return int(
        N_LAYER * 2 * N_KV_HEAD * HEAD_DIM * n_tokens
        * BYTES_PER_KV_ELEMENT_F16
    )


# ─────────────────────────────────────────────────────────────────────
# Scenarios — synthetic per-token attention shapes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    description: str
    n_tokens: int
    context_util: float
    attention: list[float]
    heavy_hitters: list[int]   # ground-truth positions that MUST be retained


def _heavy_hitter_attention(
    n: int, heavy_positions: list[int],
    *, heavy_mass: float = 0.8,
) -> list[float]:
    """Manufacture an attention vector where ``heavy_positions`` carry
    most of the mass. Returns a length-``n`` list summing to 1.0."""
    arr = np.full(n, 0.001, dtype=np.float64)
    if heavy_positions:
        per_heavy = heavy_mass / len(heavy_positions)
        for p in heavy_positions:
            if 0 <= p < n:
                arr[p] = per_heavy
    arr = arr / arr.sum()
    return arr.tolist()


def _streaming_attention(
    n: int, sink: int = 4, recent: int = 32,
    *, sink_mass: float = 0.30, recent_mass: float = 0.65,
) -> list[float]:
    """Mass concentrated on first ``sink`` tokens AND last ``recent``.

    Defaults match the StreamingLLM paper's empirically-tuned shape:
    4 attention sinks + 32-token recent window. Background mass is
    kept small (1e-5) so post-normalisation sink/recent ratios cross
    the advisor's policy gates (sink > 0.20, recency > 0.40).
    """
    arr = np.full(n, 1e-5, dtype=np.float64)
    sink = min(sink, n)
    recent = min(recent, n - sink)
    if sink:
        arr[:sink] = sink_mass / sink
    if recent:
        arr[-recent:] = recent_mass / recent
    arr = arr / arr.sum()
    return arr.tolist()


def _uniform_attention(n: int) -> list[float]:
    return [1.0 / n] * n


def _build_scenarios() -> list[Scenario]:
    n = 512
    scenarios: list[Scenario] = []

    scenarios.append(Scenario(
        name="hot-h2o",
        description=(
            "high context_util, heavy-tailed attention (12 heavy hitters "
            "carry ~80 % mass) → advisor wants H2O"
        ),
        n_tokens=n, context_util=0.80,
        attention=_heavy_hitter_attention(
            n, heavy_positions=[10, 25, 47, 88, 112, 145, 200, 270, 333, 400, 444, 500],
        ),
        heavy_hitters=[10, 25, 47, 88, 112, 145, 200, 270, 333, 400, 444, 500],
    ))

    scenarios.append(Scenario(
        name="streaming-llm",
        description=(
            "context full, attention sinks (first 4) + recent tail "
            "(last 32) → advisor wants StreamingLLM"
        ),
        n_tokens=n, context_util=0.85,
        attention=_streaming_attention(n, sink=4, recent=32),
        heavy_hitters=list(range(0, 4)) + list(range(n - 32, n)),
    ))

    scenarios.append(Scenario(
        name="cold-fits-in-hbm",
        description="low context_util — no eviction needed",
        n_tokens=n, context_util=0.20,
        attention=_uniform_attention(n),
        heavy_hitters=[],
    ))

    scenarios.append(Scenario(
        name="uniform-no-evict",
        description=(
            "high context_util but attention is near-uniform → "
            "advisor declines (no clear heavy hitters)"
        ),
        n_tokens=n, context_util=0.80,
        attention=_uniform_attention(n),
        heavy_hitters=[],
    ))

    scenarios.append(Scenario(
        name="recency-only",
        description=(
            "moderate context, attention biased to last 32 tokens "
            "(no sink) → advisor wants sliding window"
        ),
        n_tokens=n, context_util=0.65,
        attention=_streaming_attention(n, sink=0, recent=32),
        heavy_hitters=list(range(n - 32, n)),
    ))

    return scenarios


# ─────────────────────────────────────────────────────────────────────
# Strategy comparison
# ─────────────────────────────────────────────────────────────────────


def _kept_subset_sliding_window(
    n: int, window: int,
) -> list[int]:
    return list(range(max(0, n - window), n))


def _kept_subset_advisor(
    n: int, advice: EvictionAdvice,
) -> list[int]:
    if advice.evict_token_indices is None:
        return list(range(n))
    evicted = set(advice.evict_token_indices)
    return [i for i in range(n) if i not in evicted]


def _heavy_hitters_retained_pct(
    kept: list[int], heavy: list[int],
) -> float:
    if not heavy:
        return 1.0
    kept_set = set(kept)
    n_retained = sum(1 for h in heavy if h in kept_set)
    return n_retained / len(heavy)


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────


_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _color_pct(pct: float) -> str:
    if pct >= 0.95:
        return _GREEN
    if pct >= 0.70:
        return _YELLOW
    return _RED


def section(title: str) -> None:
    print()
    print(f"{_BOLD}{_BLUE}── {title} {'─' * (66 - len(title))}{_RESET}")


def fmt_kb(b: int) -> str:
    return f"{b / 1024:7.1f} KB"


def main() -> int:
    print(f"{_BOLD}Aegis Eviction Advisor — token-level KV eviction "
          f"verification{_RESET}")
    print(
        f"{_DIM}TinyLlama-1.1B layout: {N_LAYER} layers × {N_KV_HEAD} KV "
        f"heads × head_dim={HEAD_DIM} × F16 (2 B/elt)"
        f"{_RESET}"
    )
    print(f"{_DIM}analytical KV bytes per token = "
          f"{N_LAYER * 2 * N_KV_HEAD * HEAD_DIM * 2:,} B{_RESET}")

    scenarios = _build_scenarios()

    # Per-scenario rows.
    section(
        "Per-scenario: advisor vs naive sliding-window-128 vs always-keep-all"
    )
    print(
        f"  {'scenario':<22} {'policy':>14}  "
        f"{'advisor mem':>12} {'win-128 mem':>12} {'all-keep mem':>13}  "
        f"{'HH retained':>12}"
    )
    print(f"  {_DIM}{'-' * 92}{_RESET}")

    aggregate_advisor_bytes = 0
    aggregate_baseline_bytes = 0
    aggregate_window_bytes = 0
    aggregate_hh_retained: list[float] = []

    for s in scenarios:
        # Build ATV input with full attention info.
        summary = summarise_attention(s.attention)
        inp = ATVInput(
            header=ATVHeader(
                trace_id="t" * 32, span_id="s" * 16,
                tenant_id="demo", aid="evict-demo", timestamp_ns=0,
            ),
            tool_name="Bash",
            tool_args_json="{}",
            plan_text=s.description,
            cost_estimate=CostEfficiencyMetrics(
                context_utilization_ratio=s.context_util,
            ),
            attention_summary=summary,
            attention_per_token=s.attention,
        )
        atv = build_atv(inp)
        advice = eviction_advisor(atv, inp)

        # Three strategies' kept subsets.
        kept_advisor = _kept_subset_advisor(s.n_tokens, advice)
        kept_window = _kept_subset_sliding_window(s.n_tokens, window=128)

        # Memory.
        mem_advisor = kv_bytes_for(len(kept_advisor))
        mem_window = kv_bytes_for(len(kept_window))
        mem_all = kv_bytes_for(s.n_tokens)
        aggregate_advisor_bytes += mem_advisor
        aggregate_window_bytes += mem_window
        aggregate_baseline_bytes += mem_all

        # Heavy-hitter retention.
        hh_advisor = _heavy_hitters_retained_pct(kept_advisor, s.heavy_hitters)
        hh_window = _heavy_hitters_retained_pct(kept_window, s.heavy_hitters)
        aggregate_hh_retained.append(hh_advisor)

        policy_color = (
            _GREEN if advice.policy == "none"
            else (_YELLOW if advice.policy in {"sliding_window", "streaming_llm"}
                  else _RED)
        )
        print(
            f"  {s.name:<22} "
            f"{policy_color}{advice.policy:>14}{_RESET}  "
            f"{fmt_kb(mem_advisor):>12} "
            f"{fmt_kb(mem_window):>12} "
            f"{fmt_kb(mem_all):>13}  "
            f"{_color_pct(hh_advisor)}{hh_advisor*100:>11.0f}%{_RESET}"
            + (
                f"  {_DIM}(win-128: {hh_window*100:.0f}%){_RESET}"
                if s.heavy_hitters else ""
            )
        )

    # Aggregate rollup.
    section("Strategy comparison")
    saved_vs_baseline = aggregate_baseline_bytes - aggregate_advisor_bytes
    saved_vs_baseline_pct = (
        saved_vs_baseline / aggregate_baseline_bytes * 100.0
        if aggregate_baseline_bytes else 0.0
    )
    saved_window_vs_baseline = (
        aggregate_baseline_bytes - aggregate_window_bytes
    )
    saved_window_pct = (
        saved_window_vs_baseline / aggregate_baseline_bytes * 100.0
        if aggregate_baseline_bytes else 0.0
    )
    avg_hh = np.mean(aggregate_hh_retained) if aggregate_hh_retained else 1.0

    print(
        f"  {'strategy':<32} {'KV mem':>12}  {'mem saved':>11}  "
        f"{'avg HH retained':>17}"
    )
    print(f"  {_DIM}{'-' * 80}{_RESET}")
    print(
        f"  {'always-keep-all':<32} "
        f"{fmt_kb(aggregate_baseline_bytes):>12}  "
        f"{'-':>11}  "
        f"{_GREEN}{'100%':>16}{_RESET}"
    )
    print(
        f"  {'naive sliding-window-128':<32} "
        f"{fmt_kb(aggregate_window_bytes):>12}  "
        f"{_GREEN}{saved_window_pct:>10.1f}%{_RESET}  "
        f"{_DIM}varies{_RESET}"
    )
    print(
        f"  {_BOLD}{'advisor-guided':<32}{_RESET} "
        f"{fmt_kb(aggregate_advisor_bytes):>12}  "
        f"{_GREEN}{saved_vs_baseline_pct:>10.1f}%{_RESET}  "
        f"{_color_pct(avg_hh)}{avg_hh*100:>16.1f}%{_RESET}"
    )

    # Verdict.
    section("Verdict")
    print(
        f"  Advisor cuts KV memory by "
        f"{_GREEN}{saved_vs_baseline_pct:.1f}%{_RESET} vs always-keep-all,"
    )
    print(
        f"  while preserving {_color_pct(avg_hh)}{avg_hh*100:.0f}%{_RESET} "
        f"of ground-truth heavy-hitter positions."
    )
    print()
    print(
        f"  {_DIM}Compare with naive sliding-window-128 — same memory class{_RESET}"
    )
    print(
        f"  {_DIM}but blind to attention shape: it drops the early sinks{_RESET}"
    )
    print(
        f"  {_DIM}of streaming-llm scenarios and the early heavy hitters{_RESET}"
    )
    print(
        f"  {_DIM}of h2o scenarios. The advisor adapts per-context.{_RESET}"
    )

    # Per-tier breakdown.
    section("Where the savings come from")
    print(
        "  * h2o scenarios: keep top-K heavy hitters → drop the long tail"
    )
    print(
        "  * streaming-llm: keep first-4 sinks + last-32 → drop middle"
    )
    print(
        "  * sliding-window: keep the recent tail when attention is "
        "recency-biased"
    )
    print(
        "  * none: when context fits or attention is near-uniform — "
        "the advisor"
    )
    print(
        "    declines to evict because dropping any token would lose mass"
    )
    print()
    print(
        f"  {_DIM}Patent linkage: same ATV in, different output projection — "
        f"sibling{_RESET}"
    )
    print(
        f"  {_DIM}of placement / scheduling / kv_cache advisors. M13 "
        f"attribution structure.{_RESET}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
