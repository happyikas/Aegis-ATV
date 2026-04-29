"""POST /tool-outcome — host reports the post-release outcome of a
tool invocation. Updates the matching ATMU (Agent Telemetry
Management Unit) intent_log record with status + result hash +
side-effect receipt (patent ¶[0063H-1]).

v3.2 — optional perf-feedback fields (cache_hit_rate, context_util,
tokens_per_second, runtime_latency_ms, memory_peak_bytes) close the
loop with the v3.1 KV cache advisor. When present, they're folded
into the per-(tenant, aid) EWMA in :mod:`aegis.performance.feedback`,
so the next ATV's s-10/s-11 reflect measured reality.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aegis.atmu import IntentLog, TxState
from aegis.performance import get_default_store


class ToolOutcome(BaseModel):
    record_id: str               # ATMU intent_log.record_id, returned by /evaluate
    status: str                  # success | failure | timeout | partial | compensated
    result_hash: str             # SHA3 of returned payload (or commitment thereto)
    side_effect_receipt: str | None = None
    return_ts_ns: int | None = None
    # Optional follow-up state transition (e.g. compensated, rolled-back)
    follow_up_state: str | None = None
    follow_up_reason: str | None = None
    # v3.2 — optional perf-feedback envelope. The runtime (vLLM/MLX/llama.cpp)
    # measures these and reports them so the next ATV cost band is grounded.
    # Identification keys for the EWMA store.
    tenant_id: str | None = None
    aid: str | None = None
    # The metrics themselves — all optional, all clamped to non-negative.
    cache_hit_rate: float | None = None             # [0, 1]
    context_utilization_ratio: float | None = None  # [0, 1]
    tokens_per_second: float | None = None
    runtime_latency_ms: float | None = None
    memory_peak_bytes: float | None = None


def make_router(*, intent_log: IntentLog) -> APIRouter:
    r = APIRouter()

    @r.post("/tool-outcome")
    def post_outcome(payload: ToolOutcome) -> dict[str, Any]:
        try:
            rec = intent_log.append_tool_outcome(
                payload.record_id,
                status=payload.status,
                result_hash=payload.result_hash,
                side_effect_receipt=payload.side_effect_receipt,
                return_ts_ns=payload.return_ts_ns,
            )
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

        if payload.follow_up_state:
            try:
                target = TxState(payload.follow_up_state)
            except ValueError as e:
                raise HTTPException(400, f"unknown follow_up_state: {payload.follow_up_state}") from e
            try:
                rec = intent_log.transition(
                    payload.record_id,
                    new_state=target,
                    reason=payload.follow_up_reason or "post-outcome transition",
                )
            except Exception as e:
                raise HTTPException(409, str(e)) from e

        # v3.2 — fold perf metrics into the in-memory EWMA store. Best-effort:
        # missing identification keys silently skip the update so legacy
        # callers (no tenant_id/aid) keep working.
        perf_snapshot: dict[str, Any] | None = None
        any_perf = any(
            v is not None for v in (
                payload.cache_hit_rate,
                payload.context_utilization_ratio,
                payload.tokens_per_second,
                payload.runtime_latency_ms,
                payload.memory_peak_bytes,
            )
        )
        if any_perf and payload.tenant_id and payload.aid:
            store = get_default_store()
            snap = store.update(
                tenant_id=payload.tenant_id,
                aid=payload.aid,
                cache_hit_rate=payload.cache_hit_rate,
                context_utilization_ratio=payload.context_utilization_ratio,
                tokens_per_second=payload.tokens_per_second,
                runtime_latency_ms=payload.runtime_latency_ms,
                memory_peak_bytes=payload.memory_peak_bytes,
            )
            perf_snapshot = {
                "cache_hit_rate":            snap.cache_hit_rate,
                "context_utilization_ratio": snap.context_utilization_ratio,
                "tokens_per_second":         snap.tokens_per_second,
                "runtime_latency_ms":        snap.runtime_latency_ms,
                "memory_peak_bytes":         snap.memory_peak_bytes,
                "sample_count":              snap.sample_count,
            }

        return {
            "ok": True,
            "record_id": rec["record_id"],
            "current_state": rec["current_state"],
            "tool_outcome": rec.get("tool_outcome"),
            "perf_feedback": perf_snapshot,
        }

    return r
