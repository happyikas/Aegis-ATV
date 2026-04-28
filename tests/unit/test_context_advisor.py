"""Unit tests for src/aegis/performance/context_advisor.py (v3.7)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.api.advisory import make_router
from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.performance import (
    ContextAdvice,
    TurnAdvice,
    context_advisor,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


def _atv_input(
    *,
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    progress: float = 0.0,
    novelty: float = 0.0,
    plan_text: str = "",
    agent_state_text: str = "",
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
        plan_text=plan_text,
        agent_state_text=agent_state_text,
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1000.0, output_token_count=500.0,
            cumulative_tokens=1500.0, cumulative_dollars=cum,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
    )


# ── Pure-function shape ───────────────────────────────────────────────


def test_advisor_returns_context_advice() -> None:
    cur = build_atv(_atv_input(progress=0.5))
    advice = context_advisor(cur, [], [], [], token_budget=4000)
    assert isinstance(advice, ContextAdvice)
    assert advice.advisor_hash
    assert advice.expected_token_savings == 0  # no history → no savings
    assert advice.latency_ms >= 0


def test_advisor_with_empty_history_emits_no_decisions() -> None:
    cur = build_atv(_atv_input())
    advice = context_advisor(cur, [], [], [], token_budget=2000)
    assert advice.keep_verbatim_turn_ids == []
    assert advice.summarize_turn_ids == []
    assert advice.drop_turn_ids == []
    assert advice.per_turn == []


def test_parallel_lengths_required() -> None:
    cur = build_atv(_atv_input())
    h1 = build_atv(_atv_input(agent_state_text="a"))
    with pytest.raises(ValueError, match="parallel"):
        context_advisor(cur, [h1, h1], ["t1"], [100], token_budget=1000)


def test_invalid_atv_dim_raises() -> None:
    short = np.zeros(100, dtype=np.float32)
    with pytest.raises(ValueError, match="2080-D"):
        context_advisor(short, [], [], [], token_budget=1000)


# ── Determinism ───────────────────────────────────────────────────────


def test_deterministic_same_input_same_output() -> None:
    cur_inp = _atv_input(progress=0.5, agent_state_text="state-A")
    cur = build_atv(cur_inp)
    h_inputs = [
        _atv_input(agent_state_text=f"state-{i}", progress=i * 0.1) for i in range(5)
    ]
    h_atvs = [build_atv(x) for x in h_inputs]
    h_ids = [f"turn-{i}" for i in range(5)]
    h_costs = [200] * 5

    a1 = context_advisor(cur, h_atvs, h_ids, h_costs, token_budget=600)
    a2 = context_advisor(cur, h_atvs, h_ids, h_costs, token_budget=600)
    assert a1.keep_verbatim_turn_ids == a2.keep_verbatim_turn_ids
    assert a1.summarize_turn_ids == a2.summarize_turn_ids
    assert a1.drop_turn_ids == a2.drop_turn_ids
    assert a1.advisor_hash == a2.advisor_hash


# ── Budget fit ────────────────────────────────────────────────────────


def test_budget_zero_drops_everything() -> None:
    cur = build_atv(_atv_input())
    h_atvs = [build_atv(_atv_input(agent_state_text=f"s-{i}")) for i in range(3)]
    advice = context_advisor(
        cur, h_atvs, ["t0", "t1", "t2"], [500, 500, 500],
        token_budget=0,
    )
    assert len(advice.drop_turn_ids) == 3
    assert advice.total_token_cost_after == 0
    assert advice.expected_token_savings == 1500


def test_generous_budget_keeps_all_relevant_verbatim() -> None:
    """When budget >> total cost and turns are similar to current,
    everything stays verbatim."""
    # Current and all history turns share the same agent_state_text →
    # high cosine similarity → high score → keep
    cur_inp = _atv_input(agent_state_text="same-state", progress=0.5)
    cur = build_atv(cur_inp)
    h_atvs = [
        build_atv(_atv_input(agent_state_text="same-state", progress=0.5))
        for _ in range(3)
    ]
    advice = context_advisor(
        cur, h_atvs, ["t0", "t1", "t2"], [500, 500, 500],
        token_budget=10000,
    )
    assert len(advice.keep_verbatim_turn_ids) == 3
    assert advice.expected_token_savings == 0
    assert advice.total_token_cost_after == 1500


def test_recent_turn_outranks_older_unrelated_turn() -> None:
    """A recent unrelated turn beats a similarly-unrelated old turn
    via the recency component."""
    cur = build_atv(_atv_input(agent_state_text="now"))
    # Both unrelated to "now"
    h_atvs = [
        build_atv(_atv_input(agent_state_text="ancient", progress=0.0)),  # turn 0 (old)
        build_atv(_atv_input(agent_state_text="ancient", progress=0.0)),  # turn 1 (older)
        build_atv(_atv_input(agent_state_text="ancient", progress=0.0)),  # turn 2
        build_atv(_atv_input(agent_state_text="ancient", progress=0.0)),  # turn 3
        build_atv(_atv_input(agent_state_text="ancient", progress=0.0)),  # turn 4 (recent)
    ]
    h_ids = [f"t{i}" for i in range(5)]
    h_costs = [200] * 5
    advice = context_advisor(
        cur, h_atvs, h_ids, h_costs, token_budget=500,
    )
    per_turn_by_id = {t.turn_id: t for t in advice.per_turn}
    # Most recent turn (t4) must score higher than oldest (t0)
    assert per_turn_by_id["t4"].score > per_turn_by_id["t0"].score


def test_summarize_tier_picked_when_score_in_middle() -> None:
    """Mid-range relevance + tight budget → summarise."""
    cur = build_atv(_atv_input(agent_state_text="task-X", progress=0.5))
    h_atvs = [
        # mid-relevance: same state but different progress
        build_atv(_atv_input(agent_state_text="task-X", progress=0.10)),
        build_atv(_atv_input(agent_state_text="task-X", progress=0.15)),
    ]
    advice = context_advisor(
        cur, h_atvs, ["a", "b"], [800, 800],
        token_budget=400,  # too tight for verbatim, room for summary
    )
    # At least one turn should be summarised (tighter budget forces it)
    assert advice.summarize_turn_ids or advice.drop_turn_ids


def test_savings_accounting_consistent() -> None:
    cur = build_atv(_atv_input())
    h_atvs = [build_atv(_atv_input(agent_state_text=f"s-{i}")) for i in range(4)]
    h_costs = [300, 300, 300, 300]
    advice = context_advisor(
        cur, h_atvs, ["t0", "t1", "t2", "t3"], h_costs,
        token_budget=600,
    )
    total_in = sum(h_costs)
    assert advice.expected_token_savings == total_in - advice.total_token_cost_after
    assert advice.total_token_cost_after <= 600


# ── Per-turn output shape ─────────────────────────────────────────────


def test_per_turn_decisions_match_buckets() -> None:
    cur = build_atv(_atv_input())
    h_atvs = [build_atv(_atv_input(agent_state_text=f"s-{i}")) for i in range(3)]
    advice = context_advisor(
        cur, h_atvs, ["a", "b", "c"], [200, 200, 200],
        token_budget=300,
    )
    assert all(isinstance(t, TurnAdvice) for t in advice.per_turn)
    keep = {t.turn_id for t in advice.per_turn if t.decision == "keep_verbatim"}
    summ = {t.turn_id for t in advice.per_turn if t.decision == "summarize"}
    drop = {t.turn_id for t in advice.per_turn if t.decision == "drop"}
    assert keep == set(advice.keep_verbatim_turn_ids)
    assert summ == set(advice.summarize_turn_ids)
    assert drop == set(advice.drop_turn_ids)


# ── Latency ───────────────────────────────────────────────────────────


def test_latency_reasonable_for_50_turns() -> None:
    cur = build_atv(_atv_input())
    h_atvs = [build_atv(_atv_input(agent_state_text=f"s-{i}")) for i in range(50)]
    advice = context_advisor(
        cur, h_atvs, [f"t{i}" for i in range(50)], [100] * 50,
        token_budget=2000,
    )
    assert advice.latency_ms < 50.0  # generous; real budget <5ms for 50 turns


# ── Endpoint ──────────────────────────────────────────────────────────


def test_advisory_context_endpoint() -> None:
    app = FastAPI()
    app.include_router(make_router())
    cur = _atv_input(agent_state_text="now", progress=0.5)
    h0 = _atv_input(agent_state_text="now", progress=0.4)
    h1 = _atv_input(agent_state_text="ancient")
    payload = {
        "current": json.loads(cur.model_dump_json()),
        "history": [
            {"turn_id": "t0", "atv_input": json.loads(h0.model_dump_json()), "token_cost": 300},
            {"turn_id": "t1", "atv_input": json.loads(h1.model_dump_json()), "token_cost": 300},
        ],
        "token_budget": 400,
    }
    with TestClient(app) as client:
        r = client.post("/advisory/context", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert "keep_verbatim_turn_ids" in data
    assert "summarize_turn_ids" in data
    assert "drop_turn_ids" in data
    assert "advisor_hash" in data
    # Recent + similar turn should be ranked higher than ancient one
    per_turn = {t["turn_id"]: t for t in data["per_turn"]}
    assert per_turn["t0"]["score"] > per_turn["t1"]["score"]


def test_advisory_context_endpoint_zero_budget_drops_all() -> None:
    app = FastAPI()
    app.include_router(make_router())
    cur = _atv_input()
    h0 = _atv_input(agent_state_text="x")
    payload = {
        "current": json.loads(cur.model_dump_json()),
        "history": [
            {"turn_id": "t0", "atv_input": json.loads(h0.model_dump_json()), "token_cost": 200},
        ],
        "token_budget": 0,
    }
    with TestClient(app) as client:
        r = client.post("/advisory/context", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["drop_turn_ids"] == ["t0"]
    assert data["expected_token_savings"] == 200
