"""POST /advisory/kv_cache — out-of-band KV cache advice endpoint (v3.1).

The runtime (vLLM / MLX-LM / llama.cpp / SGLang) posts the same
``ATVInput`` it would post to ``/evaluate``, and gets back a
``KVCacheAdvice`` payload it can apply (or ignore) in its block
manager / scheduler.

Contract — advisory only
------------------------
* This endpoint returns NO verdict. It is independent of the trust
  firewall. The runtime is the enforcer; Aegis is only the advisor.
* Same ATV → same advice (no global state, deterministic).
* Latency budget: ≤ 5 ms p99. Pure numpy slicing.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from aegis.atv.builder import build_atv
from aegis.judge.unified_head import UnifiedHead, unified_advice_dict
from aegis.performance import (
    context_advisor,
    get_default_store,
    kv_cache_advisor,
    placement_advisor,
    scheduling_advisor,
)
from aegis.schema import ATVInput

_UNIFIED_HEAD = UnifiedHead()


class AdvisoryResponse(BaseModel):
    prefetch_segment_ids: list[str]
    evict_candidates: list[str]
    residency_class: str
    batch_key: str
    speculative_decode: bool
    confidence: float
    reasons: list[str]
    latency_ms: float
    advisor_hash: str


class SchedulingResponse(BaseModel):
    priority_class: str
    preempt_safe: bool
    max_concurrent_in_cohort: int
    deadline_ms: int
    confidence: float
    reasons: list[str]
    latency_ms: float
    advisor_hash: str


class PlacementResponse(BaseModel):
    layer_residency_plan: dict[int, str]
    kv_quantisation_dtype: str
    prefetch_window_tokens: int
    swap_threshold_bytes: int
    confidence: float
    reasons: list[str]
    latency_ms: float
    advisor_hash: str


class CombinedAdvisoryResponse(BaseModel):
    kv_cache: AdvisoryResponse
    scheduling: SchedulingResponse
    placement: PlacementResponse


class HistoryTurn(BaseModel):
    turn_id: str
    atv_input: ATVInput
    token_cost: int


class ContextRequest(BaseModel):
    current: ATVInput
    history: list[HistoryTurn] = []
    token_budget: int


class TurnAdviceResponse(BaseModel):
    turn_id: str
    decision: str
    score: float
    token_cost: int


class ContextAdviceResponse(BaseModel):
    keep_verbatim_turn_ids: list[str]
    summarize_turn_ids: list[str]
    replace_with_atv_turn_ids: list[str]
    drop_turn_ids: list[str]
    per_turn: list[TurnAdviceResponse]
    expected_token_savings: int
    total_token_cost_after: int
    confidence: float
    reasons: list[str]
    latency_ms: float
    advisor_hash: str


def _backfill_perf_signals(payload: ATVInput) -> None:
    """If the cost band's perf slots are still 0, pull from the EWMA
    feedback store keyed by (tenant_id, aid). Mutates ``payload`` in-place.

    This is the v3.2 closed-loop hook: a fresh ATV from a runtime
    that hasn't measured yet inherits the rolling per-(tenant, aid)
    averages so the advisor isn't blind on the first call after restart
    or a new agent's first tool invocation.
    """
    store = get_default_store()
    snap = store.get(tenant_id=payload.header.tenant_id, aid=payload.header.aid)
    if snap.is_empty():
        return
    ce = payload.cost_estimate
    if ce.cache_hit_rate == 0.0 and snap.cache_hit_rate > 0.0:
        ce.cache_hit_rate = snap.cache_hit_rate
    if ce.context_utilization_ratio == 0.0 and snap.context_utilization_ratio > 0.0:
        ce.context_utilization_ratio = snap.context_utilization_ratio


def make_router() -> APIRouter:
    r = APIRouter()

    @r.post("/advisory/kv_cache", response_model=AdvisoryResponse)
    def advise_kv_cache(payload: ATVInput) -> dict[str, Any]:
        # v3.2 — if the host hasn't filled the cost band's perf slots,
        # pull rolling EWMA from the feedback store. Host-supplied values
        # are NEVER overwritten (host knows best when it provides signal).
        _backfill_perf_signals(payload)
        atv = build_atv(payload)
        advice = kv_cache_advisor(atv, payload)
        return asdict(advice)

    @r.post("/advisory/scheduling", response_model=SchedulingResponse)
    def advise_scheduling(payload: ATVInput) -> dict[str, Any]:
        _backfill_perf_signals(payload)
        atv = build_atv(payload)
        advice = scheduling_advisor(atv, payload)
        return asdict(advice)

    @r.post("/advisory/placement", response_model=PlacementResponse)
    def advise_placement(payload: ATVInput) -> dict[str, Any]:
        _backfill_perf_signals(payload)
        atv = build_atv(payload)
        advice = placement_advisor(atv, payload)
        return asdict(advice)

    @r.post("/advisory/all", response_model=CombinedAdvisoryResponse)
    def advise_all(payload: ATVInput) -> dict[str, Any]:
        """One-shot — build the ATV once, fan out to all 3 advisors."""
        _backfill_perf_signals(payload)
        atv = build_atv(payload)
        return {
            "kv_cache":   asdict(kv_cache_advisor(atv, payload)),
            "scheduling": asdict(scheduling_advisor(atv, payload)),
            "placement":  asdict(placement_advisor(atv, payload)),
        }

    @r.post("/advisory/unified")
    def advise_unified(payload: ATVInput) -> dict[str, Any]:
        """v3.6 unified head — trust verdict + all 3 perf advices in
        a single ATV pass. Bound by ``unified_hash`` so audit replay
        catches any head change."""
        _backfill_perf_signals(payload)
        atv = build_atv(payload)
        uv = _UNIFIED_HEAD.evaluate_unified("", atv=atv, inp=payload)
        return unified_advice_dict(uv)

    @r.post("/advisory/context", response_model=ContextAdviceResponse)
    def advise_context(payload: ContextRequest) -> dict[str, Any]:
        """v3.7 — context window advisor. Decide per historical turn
        whether to keep verbatim, summarise, or drop, under a token budget.
        Pure function of (current ATV, history ATVs, token costs)."""
        _backfill_perf_signals(payload.current)
        current_atv = build_atv(payload.current)

        history_atvs = []
        history_ids: list[str] = []
        history_costs: list[int] = []
        for turn in payload.history:
            history_atvs.append(build_atv(turn.atv_input))
            history_ids.append(turn.turn_id)
            history_costs.append(turn.token_cost)

        advice = context_advisor(
            current_atv,
            history_atvs,
            history_ids,
            history_costs,
            token_budget=payload.token_budget,
        )
        return asdict(advice)

    return r
