"""vLLM integration shim (v3.5).

vLLM ships PagedAttention + a BlockManager that allocates/evicts KV
blocks in HBM. The integration plan: a vLLM ``Plugin`` that reads
:class:`KVCacheAdvice` per request and biases the BlockManager's
choices without forking core scheduler logic.

Three plug points
-----------------
1. **AegisAwareBlockManager**: subclass ``vllm.core.block_manager.BlockManager``
   and override ``_evict_block_internal`` to consult ``evict_candidates``
   first. ``allocate`` honours ``prefetch_segment_ids`` by pinning blocks.
2. **AegisAwareScheduler**: subclass ``vllm.core.scheduler.Scheduler``;
   override ``schedule()`` to group requests by ``batch_key`` and
   honour ``priority_class`` from the scheduling advisor.
3. **AegisAwarePrefetcher**: a background coroutine that polls
   ``/advisory/kv_cache`` for in-flight requests' ATVs and starts
   async H2D copies for ``prefetch_segment_ids`` ahead of decode.

Plus an OpenAI-protocol middleware that:
- Reads tenant_id/aid from request headers (or session metadata).
- Builds an ATVInput from the tokenised prompt + decode state.
- Calls /advisory/all once per turn, attaches advice to the request
  via vLLM's ``LLMEngineRequest.metrics`` extension dict.

Why this is "advisory only"
---------------------------
The base BlockManager / Scheduler logic is unchanged. Aegis advice
is consulted *before* vLLM's heuristics, but never overrides hard
constraints (memory limits, fairness). If Aegis is unreachable or
returns low confidence, vLLM falls back to its built-in policy.

This module ships the shim — actually wiring into a running vLLM
requires a vLLM environment, which v3.5 stops short of (vLLM not
installed in this repo). The shim is testable on its own (mocks
out the vLLM API surface).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VLLMAdvice:
    """vLLM-shaped projection — combines all three advisories."""

    # KV cache directives
    pin_block_ids: list[str] = field(default_factory=list)
    evict_priority_block_ids: list[str] = field(default_factory=list)
    speculative_decode: bool = False

    # Scheduler directives
    priority_class: str = "batch"
    deadline_ms: int = 30000
    cohort: str = ""
    max_concurrent: int = 8

    # Placement directives
    kv_dtype: str = "f16"
    prefetch_tokens: int = 32

    # Bookkeeping
    confidence: float = 0.0
    raw_kv_cache: dict[str, Any] = field(default_factory=dict)
    raw_scheduling: dict[str, Any] = field(default_factory=dict)
    raw_placement: dict[str, Any] = field(default_factory=dict)


def _project_to_vllm(advisory_all: dict[str, Any]) -> VLLMAdvice:
    kv = advisory_all.get("kv_cache", {})
    sch = advisory_all.get("scheduling", {})
    pl = advisory_all.get("placement", {})
    confs = [
        float(kv.get("confidence", 0.0)),
        float(sch.get("confidence", 0.0)),
        float(pl.get("confidence", 0.0)),
    ]
    return VLLMAdvice(
        pin_block_ids=list(kv.get("prefetch_segment_ids", [])),
        evict_priority_block_ids=list(kv.get("evict_candidates", [])),
        speculative_decode=bool(kv.get("speculative_decode", False)),
        priority_class=str(sch.get("priority_class", "batch")),
        deadline_ms=int(sch.get("deadline_ms", 30000)),
        cohort=str(kv.get("batch_key", "")),
        max_concurrent=int(sch.get("max_concurrent_in_cohort", 8)),
        kv_dtype=str(pl.get("kv_quantisation_dtype", "f16")),
        prefetch_tokens=int(pl.get("prefetch_window_tokens", 32)),
        confidence=sum(confs) / 3.0 if confs else 0.0,
        raw_kv_cache=dict(kv),
        raw_scheduling=dict(sch),
        raw_placement=dict(pl),
    )


class VLLMAegisAdvisor:
    """HTTP advisor adapter for vLLM. Hits ``/advisory/all`` once per turn
    and projects the combined response onto vLLM's BlockManager + Scheduler
    surface.

    Usage (in a vLLM Plugin)::

        advisor = VLLMAegisAdvisor("http://aegis:8080")
        # Inside Scheduler.schedule(), for each new SequenceGroup:
        atv_input_dict = build_atv_input(seq_group)
        advice = advisor.advise(atv_input_dict)
        if advice.confidence > 0.30:
            block_manager.pin(advice.pin_block_ids)
            block_manager.bias_evict(advice.evict_priority_block_ids)
            seq_group.priority = advice.priority_class
    """

    def __init__(self, base_url: str = "http://localhost:8080", *, timeout_s: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def advise(self, atv_input: dict[str, Any]) -> VLLMAdvice:
        body = json.dumps(atv_input).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/advisory/all",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        return _project_to_vllm(payload)

    def report(
        self,
        record_id: str,
        *,
        tenant_id: str,
        aid: str,
        cache_hit_rate: float | None = None,
        context_utilization_ratio: float | None = None,
        tokens_per_second: float | None = None,
    ) -> None:
        """Close the loop. Same shape as MLX-LM adapter."""
        body = json.dumps({
            "record_id": record_id,
            "status": "success",
            "result_hash": "vllm-runtime",
            "tenant_id": tenant_id, "aid": aid,
            "cache_hit_rate": cache_hit_rate,
            "context_utilization_ratio": context_utilization_ratio,
            "tokens_per_second": tokens_per_second,
        }).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/tool-outcome",
            data=body, headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s):
            pass


__all__ = ["VLLMAdvice", "VLLMAegisAdvisor"]
