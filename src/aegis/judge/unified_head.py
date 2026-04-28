"""M13 Unified Head (v3.6) — verdict + KV cache + scheduling + placement
in one forward pass over the 2080-D ATV.

Patent extension
----------------
The original M13 attribution head (Claim 8) emits three outputs:
``(decision, confidence, per-subfield attribution)``. v3.6 extends
the same architectural pattern to **four output families** that share
the input ATV but project to different output spaces:

* **Trust head** — 3-class ALLOW/BLOCK/REQUIRE_APPROVAL + attribution
* **KV-cache head** — prefetch/evict/residency/batch_key/speculative
* **Scheduling head** — priority/preempt/cohort/deadline
* **Placement head** — layer plan/KV dtype/prefetch window

Design properties (preserved from M13)
--------------------------------------
1. **Pure function of ATV** — same input bytes → same outputs.
2. **Sub-millisecond** — single ATV traversal, all heads share slicing.
3. **Auditable** — ``unified_hash`` is SHA3-256 over the four
   constituent ``advisor_hash`` / ``model_hash`` values, so
   ``aegis verify-audit`` can re-run the full output bundle.
4. **Backward compatible** — the trust verdict path is bit-identical
   to the v2.5 AttributionHead. The perf heads are *additive*; if
   the caller doesn't want them, ``evaluate_full`` keeps the
   original three-tuple contract.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from aegis.judge.attribution_head import AttributionHead
from aegis.judge.base import JudgeVerdict
from aegis.performance.kv_cache_advisor import KVCacheAdvice, kv_cache_advisor
from aegis.performance.placement_advisor import PlacementAdvice, placement_advisor
from aegis.performance.scheduling_advisor import SchedulingAdvice, scheduling_advisor
from aegis.schema import ATVInput


@dataclass(frozen=True)
class UnifiedVerdict:
    """Trust + perf advice emitted from a single ATV pass."""

    verdict: JudgeVerdict
    kv_cache: KVCacheAdvice
    scheduling: SchedulingAdvice
    placement: PlacementAdvice
    unified_hash: str
    total_latency_ms: float


def _unified_hash(parts: list[str]) -> str:
    """SHA3-256 over the constituent advisor hashes, sorted for stability."""
    blob = "|".join(sorted(parts)).encode("utf-8")
    return hashlib.sha3_256(blob).hexdigest()


class UnifiedHead:
    """v3.6 — single attribution head emitting trust + perf bundle.

    Internally composes the v2.5 AttributionHead and the v3.1/v3.4
    advisors. The point is *not* code reuse alone; it's establishing
    the pattern that a *single* learned head can drive both the
    safety verdict and the performance hints (Claim 8 extension).

    For v3.6 the sub-heads are still the hand-tuned advisors — a
    learned unified head is the v4.x training milestone, kept off
    the v3.x critical path.
    """

    def __init__(
        self,
        *,
        attribution_head: AttributionHead | None = None,
    ) -> None:
        self._attribution = attribution_head or AttributionHead()

    @property
    def model_hash(self) -> str:
        return self._attribution.model_hash

    def evaluate_unified(
        self,
        summary: str,
        *,
        atv: np.ndarray,
        inp: ATVInput | None = None,
    ) -> UnifiedVerdict:
        """One ATV pass → JudgeVerdict + KVCacheAdvice + SchedulingAdvice + PlacementAdvice."""
        if atv is None or atv.shape[0] == 0:
            raise ValueError("UnifiedHead.evaluate_unified requires an ATV")

        t0 = time.perf_counter_ns()

        verdict = self._attribution.evaluate_full(summary, atv=atv, inp=inp)
        kv = kv_cache_advisor(atv, inp)
        sch = scheduling_advisor(atv, inp)
        pl = placement_advisor(atv, inp)

        total_ms = (time.perf_counter_ns() - t0) / 1_000_000

        # Unified hash binds all four heads' versions together. If any
        # head's frozen weights / version string changes, this hash
        # changes — so audit replay is detect-everything-or-nothing.
        unified = _unified_hash([
            verdict.model_hash or "",
            kv.advisor_hash,
            sch.advisor_hash,
            pl.advisor_hash,
        ])

        return UnifiedVerdict(
            verdict=verdict,
            kv_cache=kv,
            scheduling=sch,
            placement=pl,
            unified_hash=unified,
            total_latency_ms=round(total_ms, 3),
        )


def unified_advice_dict(uv: UnifiedVerdict) -> dict[str, Any]:
    """Serialise a UnifiedVerdict to a JSON-friendly dict (for endpoint)."""
    from dataclasses import asdict

    return {
        "verdict": {
            "decision":             uv.verdict.decision,
            "confidence":           uv.verdict.confidence,
            "reason":               uv.verdict.reason,
            "subfield_attribution": uv.verdict.subfield_attribution,
            "model_hash":           uv.verdict.model_hash,
            "latency_ms":           uv.verdict.latency_ms,
            "layer_traces":         uv.verdict.layer_traces,
        },
        "kv_cache":     asdict(uv.kv_cache),
        "scheduling":   asdict(uv.scheduling),
        "placement":    asdict(uv.placement),
        "unified_hash": uv.unified_hash,
        "total_latency_ms": uv.total_latency_ms,
    }


__all__ = ["UnifiedHead", "UnifiedVerdict", "unified_advice_dict"]
