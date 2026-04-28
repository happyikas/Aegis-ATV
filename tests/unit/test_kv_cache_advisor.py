"""Unit tests for src/aegis/performance/kv_cache_advisor.py (v3.1)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.performance import KVCacheAdvice, kv_cache_advisor
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


def _atv_input(
    *,
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    cache_hit_rate: float = 0.0,
    context_util: float = 0.0,
    progress: float = 0.0,
    novelty: float = 0.0,
    plan_text: str = "",
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
) -> ATVInput:
    args = args or {"command": "ls"}
    cum_dollars = expected_flops("claude-haiku-4-5", in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="demo",
            aid="agent-test",
            timestamp_ns=0,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        plan_text=plan_text,
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens,
            output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
            cache_hit_rate=cache_hit_rate,
            context_utilization_ratio=context_util,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
    )


# ── Pure-function shape ───────────────────────────────────────────────


def test_advisor_returns_kvcacheadvice_dataclass() -> None:
    inp = _atv_input()
    atv = build_atv(inp)
    advice = kv_cache_advisor(atv, inp)
    assert isinstance(advice, KVCacheAdvice)
    assert advice.residency_class in {"hot", "warm", "cold"}
    assert isinstance(advice.batch_key, str) and len(advice.batch_key) > 0
    assert advice.advisor_hash  # non-empty
    assert advice.latency_ms >= 0.0


def test_advisor_is_pure_function_deterministic() -> None:
    """Same ATV → same advice, bit-identical (modulo wall-clock latency)."""
    inp = _atv_input(progress=0.5, novelty=0.1)
    atv = build_atv(inp)
    a1 = kv_cache_advisor(atv, inp)
    a2 = kv_cache_advisor(atv, inp)
    assert a1.prefetch_segment_ids == a2.prefetch_segment_ids
    assert a1.evict_candidates == a2.evict_candidates
    assert a1.residency_class == a2.residency_class
    assert a1.batch_key == a2.batch_key
    assert a1.speculative_decode == a2.speculative_decode
    assert a1.advisor_hash == a2.advisor_hash


def test_advisor_no_input_signal_collapses_confidence() -> None:
    """When the cost band is empty (host hasn't filled s-10/s-11/s-15)
    and novelty is 0, advisor confidence collapses → runtime should
    fall back to its own heuristic."""
    inp = _atv_input()  # all zeros
    atv = build_atv(inp)
    advice = kv_cache_advisor(atv, inp)
    assert advice.confidence < 0.20


# ── Residency-class heuristic ─────────────────────────────────────────


def test_high_progress_low_novelty_yields_hot() -> None:
    inp = _atv_input(progress=0.6, novelty=0.05)
    advice = kv_cache_advisor(build_atv(inp), inp)
    assert advice.residency_class == "hot"
    # hot path includes prefetch IDs from memory + inter-agent bands
    assert len(advice.prefetch_segment_ids) > 0
    assert len(advice.evict_candidates) == 0


def test_low_cachehit_high_context_yields_cold() -> None:
    inp = _atv_input(cache_hit_rate=0.05, context_util=0.85)
    advice = kv_cache_advisor(build_atv(inp), inp)
    assert advice.residency_class == "cold"
    # cold path emits eviction candidates, no prefetch
    assert len(advice.prefetch_segment_ids) == 0
    assert len(advice.evict_candidates) > 0


def test_default_input_yields_warm() -> None:
    inp = _atv_input(cache_hit_rate=0.5, context_util=0.4, progress=0.1)
    advice = kv_cache_advisor(build_atv(inp), inp)
    assert advice.residency_class == "warm"


# ── Speculative-decode heuristic ──────────────────────────────────────


def test_codeblock_low_novelty_enables_speculative() -> None:
    # 30 repetitions × ~30 chars = 900 chars → length_norm > 0.10
    inp = _atv_input(
        plan_text="```python\nprint('hello world here')\n```\n" * 30,
        novelty=0.05,
    )
    advice = kv_cache_advisor(build_atv(inp), inp)
    assert advice.speculative_decode is True


def test_high_novelty_disables_speculative() -> None:
    inp = _atv_input(plan_text="```python\nprint('hi')\n```", novelty=0.9)
    advice = kv_cache_advisor(build_atv(inp), inp)
    assert advice.speculative_decode is False


# ── Batch-key cohort behaviour ────────────────────────────────────────


def test_same_agent_same_blast_yield_same_batch_key() -> None:
    """Two requests from the same agent invoking same-blast tool should
    land in the same batch cohort."""
    inp_a = _atv_input(tool="Bash", args={"command": "ls /a"})
    inp_b = _atv_input(tool="Bash", args={"command": "ls /b"})
    a_a = kv_cache_advisor(build_atv(inp_a), inp_a)
    a_b = kv_cache_advisor(build_atv(inp_b), inp_b)
    assert a_a.batch_key == a_b.batch_key


def test_different_blast_yields_different_batch_key() -> None:
    """rm -rf is high-blast; ls is low-blast → different cohorts."""
    inp_safe = _atv_input(tool="read_file", args={"file_path": "/tmp/x"})
    inp_dangerous = _atv_input(tool="execute_shell", args={"command": "rm -rf /tmp/x"})
    a_safe = kv_cache_advisor(build_atv(inp_safe), inp_safe)
    a_danger = kv_cache_advisor(build_atv(inp_dangerous), inp_dangerous)
    assert a_safe.batch_key != a_danger.batch_key


# ── Segment-ID determinism ────────────────────────────────────────────


def test_segment_ids_are_stable_strings() -> None:
    inp = _atv_input(progress=0.6, novelty=0.05)
    advice = kv_cache_advisor(build_atv(inp), inp)
    for seg_id in advice.prefetch_segment_ids:
        assert isinstance(seg_id, str)
        # mem-XXXXXXXX or iag-XXXXXXXX format
        assert seg_id.startswith(("mem-", "iag-", "hist-"))


# ── Sub-millisecond latency ───────────────────────────────────────────


def test_latency_within_budget() -> None:
    inp = _atv_input(progress=0.6, novelty=0.05)
    atv = build_atv(inp)
    advice = kv_cache_advisor(atv, inp)
    assert advice.latency_ms < 10.0  # generous: real budget is <1ms


# ── Empty/edge ATV ────────────────────────────────────────────────────


def test_zero_atv_does_not_crash() -> None:
    """Pathological all-zero ATV still produces a valid advice."""
    atv = np.zeros(2080, dtype=np.float32)
    advice = kv_cache_advisor(atv, None)
    assert isinstance(advice, KVCacheAdvice)


# ── HTTP endpoint integration ─────────────────────────────────────────


def test_advisory_kv_cache_endpoint_via_app() -> None:
    """End-to-end: POST /advisory/kv_cache returns KVCacheAdvice JSON."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.advisory import make_router
    app = FastAPI()
    app.include_router(make_router())

    inp = _atv_input(progress=0.6, novelty=0.05)
    body = json.loads(inp.model_dump_json())
    with TestClient(app) as client:
        r = client.post("/advisory/kv_cache", json=body)
    assert r.status_code == 200
    data = r.json()
    assert "residency_class" in data
    assert "batch_key" in data
    assert "advisor_hash" in data
    assert data["residency_class"] in {"hot", "warm", "cold"}
