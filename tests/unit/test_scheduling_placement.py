"""Unit tests for v3.4 scheduling_advisor + placement_advisor + endpoints."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.api.advisory import make_router
from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.performance import (
    PlacementAdvice,
    SchedulingAdvice,
    placement_advisor,
    scheduling_advisor,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


def _atv_input(
    *,
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    cache_hit_rate: float = 0.0,
    context_util: float = 0.0,
    progress: float = 0.0,
    novelty: float = 0.0,
    operator_present: float = 0.0,
) -> ATVInput:
    args = args or {"command": "ls"}
    cum = expected_flops("claude-haiku-4-5", 1000.0, 500.0) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="demo", aid="agent-test", timestamp_ns=0,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1000.0, output_token_count=500.0,
            cumulative_tokens=1500.0, cumulative_dollars=cum,
            cache_hit_rate=cache_hit_rate,
            context_utilization_ratio=context_util,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
        oversight={"operator_presence": operator_present},
    )


# ── scheduling_advisor ────────────────────────────────────────────────


def test_scheduling_returns_dataclass() -> None:
    inp = _atv_input()
    advice = scheduling_advisor(build_atv(inp), inp)
    assert isinstance(advice, SchedulingAdvice)
    assert advice.priority_class in {"interactive", "batch", "low"}
    assert advice.advisor_hash


def test_operator_present_yields_interactive() -> None:
    inp = _atv_input(operator_present=1.0)
    advice = scheduling_advisor(build_atv(inp), inp)
    assert advice.priority_class == "interactive"


def test_low_blast_low_novelty_yields_batch() -> None:
    inp = _atv_input(tool="read_file", args={"file_path": "/tmp/x"})
    advice = scheduling_advisor(build_atv(inp), inp)
    assert advice.priority_class == "batch"


def test_destructive_arg_disables_preempt_safe() -> None:
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /tmp/x"})
    advice = scheduling_advisor(build_atv(inp), inp)
    assert advice.preempt_safe is False


def test_read_only_enables_preempt_safe() -> None:
    inp = _atv_input(tool="read_file", args={"file_path": "/tmp/x"})
    advice = scheduling_advisor(build_atv(inp), inp)
    assert advice.preempt_safe is True


def test_low_novelty_increases_cohort_concurrency() -> None:
    inp_stable = _atv_input(tool="read_file", args={"file_path": "/tmp/x"}, novelty=0.05)
    inp_unstable = _atv_input(tool="read_file", args={"file_path": "/tmp/x"}, novelty=0.80)
    a_stable = scheduling_advisor(build_atv(inp_stable), inp_stable)
    a_unstable = scheduling_advisor(build_atv(inp_unstable), inp_unstable)
    assert a_stable.max_concurrent_in_cohort > a_unstable.max_concurrent_in_cohort


def test_scheduling_deterministic() -> None:
    inp = _atv_input(operator_present=1.0)
    atv = build_atv(inp)
    a1 = scheduling_advisor(atv, inp)
    a2 = scheduling_advisor(atv, inp)
    assert a1.priority_class == a2.priority_class
    assert a1.deadline_ms == a2.deadline_ms
    assert a1.advisor_hash == a2.advisor_hash


# ── placement_advisor ─────────────────────────────────────────────────


def test_placement_returns_dataclass() -> None:
    inp = _atv_input()
    advice = placement_advisor(build_atv(inp), inp)
    assert isinstance(advice, PlacementAdvice)
    assert advice.kv_quantisation_dtype in {"f16", "q8_0", "q4_0"}
    assert advice.prefetch_window_tokens > 0


def test_low_pressure_keeps_f16() -> None:
    inp = _atv_input(context_util=0.10, cache_hit_rate=0.80)
    advice = placement_advisor(build_atv(inp), inp)
    assert advice.kv_quantisation_dtype == "f16"


def test_high_pressure_quantises_to_q4() -> None:
    inp = _atv_input(context_util=0.85, cache_hit_rate=0.10)
    advice = placement_advisor(build_atv(inp), inp)
    assert advice.kv_quantisation_dtype == "q4_0"


def test_med_pressure_quantises_to_q8() -> None:
    inp = _atv_input(context_util=0.50)
    advice = placement_advisor(build_atv(inp), inp)
    assert advice.kv_quantisation_dtype == "q8_0"


def test_layer_plan_keeps_early_and_late_in_hbm() -> None:
    inp = _atv_input(context_util=0.85, cache_hit_rate=0.10)
    advice = placement_advisor(build_atv(inp), inp, layer_count=32)
    # First 4 layers (32//8) and last 2 layers (32//16) must be HBM
    assert advice.layer_residency_plan[0] == "hbm"
    assert advice.layer_residency_plan[1] == "hbm"
    assert advice.layer_residency_plan[31] == "hbm"
    # Middle layers should be on CPU under high pressure
    assert advice.layer_residency_plan[16] == "cpu"


def test_low_novelty_widens_prefetch_window() -> None:
    inp_stable = _atv_input(novelty=0.05)
    inp_unstable = _atv_input(novelty=0.80)
    a_stable = placement_advisor(build_atv(inp_stable), inp_stable)
    a_unstable = placement_advisor(build_atv(inp_unstable), inp_unstable)
    assert a_stable.prefetch_window_tokens > a_unstable.prefetch_window_tokens


def test_placement_deterministic() -> None:
    inp = _atv_input(context_util=0.50)
    atv = build_atv(inp)
    a1 = placement_advisor(atv, inp)
    a2 = placement_advisor(atv, inp)
    assert a1.kv_quantisation_dtype == a2.kv_quantisation_dtype
    assert a1.prefetch_window_tokens == a2.prefetch_window_tokens
    assert a1.layer_residency_plan == a2.layer_residency_plan
    assert a1.advisor_hash == a2.advisor_hash


def test_placement_zero_atv_does_not_crash() -> None:
    atv = np.zeros(2080, dtype=np.float32)
    advice = placement_advisor(atv, None)
    assert isinstance(advice, PlacementAdvice)


# ── Endpoints ─────────────────────────────────────────────────────────


def test_advisory_scheduling_endpoint() -> None:
    app = FastAPI()
    app.include_router(make_router())
    inp = _atv_input(operator_present=1.0)
    body = json.loads(inp.model_dump_json())
    with TestClient(app) as client:
        r = client.post("/advisory/scheduling", json=body)
    assert r.status_code == 200
    assert r.json()["priority_class"] == "interactive"


def test_advisory_placement_endpoint() -> None:
    app = FastAPI()
    app.include_router(make_router())
    inp = _atv_input(context_util=0.85, cache_hit_rate=0.10)
    body = json.loads(inp.model_dump_json())
    with TestClient(app) as client:
        r = client.post("/advisory/placement", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["kv_quantisation_dtype"] == "q4_0"


def test_advisory_all_endpoint_returns_three_blocks() -> None:
    app = FastAPI()
    app.include_router(make_router())
    inp = _atv_input(progress=0.6, novelty=0.05, operator_present=1.0)
    body = json.loads(inp.model_dump_json())
    with TestClient(app) as client:
        r = client.post("/advisory/all", json=body)
    assert r.status_code == 200
    data = r.json()
    assert "kv_cache" in data
    assert "scheduling" in data
    assert "placement" in data
    assert data["kv_cache"]["residency_class"] == "hot"
    assert data["scheduling"]["priority_class"] == "interactive"
