"""Memory placement advisor (v3.4) — pure function ATV → PlacementAdvice.

Reads ATV signals to advise the runtime memory tiering layer on:

* **layer_residency_plan** — dict[layer_index → tier]
  early layers (embedding, first few transformer blocks) → HBM
  middle layers → HBM if hot, CPU/RAM if warm/cold
  late layers + heads → always HBM (smallest, latency-critical)
* **kv_quantisation_dtype** — f16 | q8_0 | q4_0
  hot path → f16 (no precision loss)
  warm → q8_0 (2× memory savings, ~0.5 % quality loss)
  cold → q4_0 (4× savings, ~2 % quality loss; OOM avoidance)
* **prefetch_window_tokens** — how many tokens ahead to async-stream
  weights for. Higher when novelty is low (prediction stable).
* **swap_threshold_bytes** — at what HBM pressure to start CPU-RAM
  swapping. Tightens under high context_util.

Patent linkage
--------------
Tier-aware placement is a CSD/HBM-conscious layout. With T3 hardware
(M19+), the same advice maps to NVMe-CSD compute zones, where the
runtime can place "cold" segments in the CSD's compressed cache and
keep "hot" segments resident in HBM. Same M13 attribution structure.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aegis.schema import (
    SLICE_AID_ATS_SCALARS,
    SLICE_COST_EFFICIENCY_METRICS,
    SLICE_NOVELTY_SCORE,
    ATVInput,
)

KVDType = Literal["f16", "q8_0", "q4_0"]
LayerTier = Literal["hbm", "cpu", "csd"]

_COST_S10_CACHE_HIT_RATE = 9
_COST_S11_CONTEXT_UTIL = 10
_COST_S15_TASK_PROGRESS = 14


@dataclass(frozen=True)
class PlacementAdvice:
    layer_residency_plan: dict[int, LayerTier] = field(default_factory=dict)
    kv_quantisation_dtype: KVDType = "f16"
    prefetch_window_tokens: int = 32
    swap_threshold_bytes: int = 0
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    advisor_hash: str = ""


_VERSION = "placement_advisor_v1"
_HASH = hashlib.sha3_256(_VERSION.encode()).hexdigest()

# Default 32-layer model layout — caller can pass a different
# layer count via inp.capability_manifest in the future.
_DEFAULT_LAYER_COUNT = 32


def _make_residency_plan(
    *,
    layer_count: int,
    tier_pressure: Literal["low", "med", "high"],
    has_csd: bool,
) -> dict[int, LayerTier]:
    """Hand-tuned plan: early embedding + late head always HBM;
    middle blocks demoted progressively under pressure."""
    plan: dict[int, LayerTier] = {}
    early = max(2, layer_count // 8)
    late_head = max(2, layer_count // 16)
    for i in range(layer_count):
        if i < early or i >= layer_count - late_head or tier_pressure == "low":
            plan[i] = "hbm"
        elif tier_pressure == "med":
            plan[i] = "hbm" if (i % 2 == 0) else "cpu"
        else:
            # High pressure: cold middle layers go to CPU or CSD
            plan[i] = "csd" if has_csd else "cpu"
    return plan


def placement_advisor(
    atv: np.ndarray,
    inp: ATVInput | None = None,
    *,
    layer_count: int = _DEFAULT_LAYER_COUNT,
) -> PlacementAdvice:
    """ATV → PlacementAdvice. Pure function, sub-millisecond."""
    t0 = time.perf_counter_ns()

    cost = atv[SLICE_COST_EFFICIENCY_METRICS]
    cache_hit_rate = float(cost[_COST_S10_CACHE_HIT_RATE])
    context_util = float(cost[_COST_S11_CONTEXT_UTIL])
    progress = float(cost[_COST_S15_TASK_PROGRESS])

    novelty_band = atv[SLICE_NOVELTY_SCORE]
    composite_novelty = float(novelty_band[3]) if novelty_band.size >= 4 else 0.0

    aid_band = atv[SLICE_AID_ATS_SCALARS]
    is_t3 = float(aid_band[4]) if aid_band.size >= 5 else 0.0

    reasons: list[str] = []

    # Pressure tier:
    #   high  — context util >= 0.70 OR cache_hit < 0.20
    #   med   — context util >= 0.40
    #   low   — otherwise
    if context_util >= 0.70 or (cache_hit_rate > 0 and cache_hit_rate < 0.20):
        pressure: Literal["low", "med", "high"] = "high"
        reasons.append(
            f"high pressure: context_util={context_util:.2f}, cache_hit_rate={cache_hit_rate:.2f}"
        )
    elif context_util >= 0.40:
        pressure = "med"
        reasons.append(f"med pressure: context_util={context_util:.2f}")
    else:
        pressure = "low"
        reasons.append("low pressure")

    has_csd = is_t3 > 0.5  # T3 deployments expose CSD tier

    plan = _make_residency_plan(
        layer_count=layer_count, tier_pressure=pressure, has_csd=has_csd,
    )

    # KV quantisation
    kv_dtype: KVDType
    if pressure == "low":
        kv_dtype = "f16"
    elif pressure == "med":
        kv_dtype = "q8_0"
        reasons.append("kv quantised to q8_0 (med pressure)")
    else:
        kv_dtype = "q4_0"
        reasons.append("kv quantised to q4_0 (high pressure / OOM avoidance)")

    # Prefetch window: low novelty = stable prediction = larger window safe
    if composite_novelty < 0.20:
        prefetch_window = 128
    elif composite_novelty < 0.50:
        prefetch_window = 64
    else:
        prefetch_window = 16
    reasons.append(f"prefetch_window_tokens={prefetch_window}")

    # Swap threshold scales inversely with context util
    swap_threshold = int((1.0 - context_util) * 8 * (1024**3))  # bytes; up to 8GB
    reasons.append(f"swap_threshold_bytes={swap_threshold:_}")

    has_signal = (
        (cache_hit_rate > 0)
        + (context_util > 0)
        + (progress > 0)
        + (composite_novelty > 0)
    )
    confidence = float(min(1.0, has_signal / 4.0))

    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

    return PlacementAdvice(
        layer_residency_plan=plan,
        kv_quantisation_dtype=kv_dtype,
        prefetch_window_tokens=prefetch_window,
        swap_threshold_bytes=swap_threshold,
        confidence=confidence,
        reasons=reasons,
        latency_ms=round(elapsed_ms, 3),
        advisor_hash=_HASH,
    )
