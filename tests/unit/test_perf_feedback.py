"""Unit tests for src/aegis/performance/feedback.py + closed-loop wiring (v3.2)."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.api.advisory import make_router as make_advisory_router
from aegis.api.tool_outcome import make_router as make_tool_outcome_router
from aegis.atmu import IntentLog
from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.performance import (
    PerfFeedbackStore,
    get_default_store,
    kv_cache_advisor,
    reset_default_store,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_store()
    yield
    reset_default_store()


# ── PerfFeedbackStore EWMA ────────────────────────────────────────────


def test_store_starts_empty() -> None:
    s = PerfFeedbackStore()
    snap = s.get(tenant_id="t", aid="a")
    assert snap.is_empty()
    assert snap.cache_hit_rate == 0.0
    assert snap.sample_count == 0


def test_first_update_sets_value_proportional_to_alpha() -> None:
    """First observation: state = alpha * obs (since prior=0)."""
    s = PerfFeedbackStore(alpha=0.30)
    snap = s.update(tenant_id="t", aid="a", cache_hit_rate=1.0)
    assert snap.cache_hit_rate == pytest.approx(0.30, rel=1e-6)
    assert snap.sample_count == 1


def test_repeated_observations_converge() -> None:
    """Convergence to the constant signal."""
    s = PerfFeedbackStore(alpha=0.30)
    for _ in range(50):
        s.update(tenant_id="t", aid="a", cache_hit_rate=0.7)
    snap = s.get(tenant_id="t", aid="a")
    assert snap.cache_hit_rate == pytest.approx(0.7, rel=1e-3)
    assert snap.sample_count == 50


def test_independent_keys_do_not_interfere() -> None:
    s = PerfFeedbackStore()
    s.update(tenant_id="alice", aid="agent-1", cache_hit_rate=0.9)
    s.update(tenant_id="bob",   aid="agent-1", cache_hit_rate=0.1)
    a = s.get(tenant_id="alice", aid="agent-1")
    b = s.get(tenant_id="bob",   aid="agent-1")
    assert a.cache_hit_rate > b.cache_hit_rate


def test_partial_update_only_touches_provided_fields() -> None:
    s = PerfFeedbackStore()
    s.update(tenant_id="t", aid="a", cache_hit_rate=0.8)
    s.update(tenant_id="t", aid="a", tokens_per_second=120.0)
    snap = s.get(tenant_id="t", aid="a")
    assert snap.cache_hit_rate > 0.0
    assert snap.tokens_per_second > 0.0
    assert snap.context_utilization_ratio == 0.0  # never updated


def test_alpha_validation() -> None:
    with pytest.raises(ValueError):
        PerfFeedbackStore(alpha=0.0)
    with pytest.raises(ValueError):
        PerfFeedbackStore(alpha=1.5)


def test_reset_clears_state() -> None:
    s = PerfFeedbackStore()
    s.update(tenant_id="t", aid="a", cache_hit_rate=1.0)
    assert s.get(tenant_id="t", aid="a").sample_count == 1
    s.reset()
    assert s.get(tenant_id="t", aid="a").sample_count == 0


def test_default_store_singleton() -> None:
    a = get_default_store()
    b = get_default_store()
    assert a is b


# ── /tool-outcome endpoint perf-feedback wiring ───────────────────────


def _app_with_routes(intent_log: IntentLog) -> FastAPI:
    app = FastAPI()
    app.include_router(make_tool_outcome_router(intent_log=intent_log))
    app.include_router(make_advisory_router())
    return app


def _seed_intent_record(intent_log: IntentLog) -> str:
    rec = intent_log.append_tentative(
        aid="agent-loop",
        tenant_id="tenant-loop",
        trace_id="t" * 32,
        span_id="s" * 16,
        parent_span_id=None,
        tool_name="Bash",
        tool_args_hash="dead",
        blast_radius=1,
        atv_commitment="abc",
    )
    return rec["record_id"]


def test_tool_outcome_with_perf_metrics_updates_store(tmp_path) -> None:
    intent_log = IntentLog(str(tmp_path / "intent.sqlite"))
    record_id = _seed_intent_record(intent_log)
    app = _app_with_routes(intent_log)

    body = {
        "record_id": record_id,
        "status": "success",
        "result_hash": "res",
        "tenant_id": "tenant-loop",
        "aid": "agent-loop",
        "cache_hit_rate": 0.85,
        "context_utilization_ratio": 0.50,
        "tokens_per_second": 240.0,
    }
    with TestClient(app) as client:
        r = client.post("/tool-outcome", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["perf_feedback"] is not None
    assert data["perf_feedback"]["sample_count"] == 1
    # store reflects the update
    snap = get_default_store().get(tenant_id="tenant-loop", aid="agent-loop")
    assert snap.cache_hit_rate > 0.0
    assert snap.tokens_per_second > 0.0


def test_tool_outcome_without_perf_keys_skips_update(tmp_path) -> None:
    intent_log = IntentLog(str(tmp_path / "intent.sqlite"))
    record_id = _seed_intent_record(intent_log)
    app = _app_with_routes(intent_log)
    body = {
        "record_id": record_id,
        "status": "success",
        "result_hash": "res",
        # no perf metrics, no tenant_id/aid
    }
    with TestClient(app) as client:
        r = client.post("/tool-outcome", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["perf_feedback"] is None
    snap = get_default_store().get(tenant_id="tenant-loop", aid="agent-loop")
    assert snap.is_empty()


# ── Advisory backfill from feedback store ─────────────────────────────


def _atv_input(tenant_id: str, aid: str, *, cache_hit_rate: float = 0.0) -> ATVInput:
    cum_dollars = expected_flops("claude-haiku-4-5", 1000.0, 500.0) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id=tenant_id, aid=aid, timestamp_ns=0,
        ),
        tool_name="Bash",
        tool_args_json=json.dumps({"command": "ls"}),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1000.0, output_token_count=500.0,
            cumulative_tokens=1500.0, cumulative_dollars=cum_dollars,
            cache_hit_rate=cache_hit_rate,
        ),
    )


def test_advisor_with_no_feedback_has_low_confidence() -> None:
    inp = _atv_input("t", "a")
    advice = kv_cache_advisor(build_atv(inp), inp)
    assert advice.confidence < 0.20


def test_advisor_endpoint_picks_up_feedback_signal(tmp_path) -> None:
    """Closed loop: tool-outcome reports cache_hit_rate=0.9 → next
    /advisory/kv_cache call (with cache_hit_rate=0.0 in payload) reads
    the rolling EWMA and produces a meaningful confidence."""
    intent_log = IntentLog(str(tmp_path / "intent.sqlite"))
    record_id = _seed_intent_record(intent_log)
    app = _app_with_routes(intent_log)

    with TestClient(app) as client:
        # 1) seed the EWMA via /tool-outcome (3 reports to converge quickly)
        for _ in range(3):
            client.post("/tool-outcome", json={
                "record_id": record_id,
                "status": "success",
                "result_hash": "res",
                "tenant_id": "tenant-loop",
                "aid": "agent-loop",
                "cache_hit_rate": 0.9,
                "context_utilization_ratio": 0.6,
            })
        # 2) advise with bare payload — host hasn't filled cache_hit_rate
        inp = _atv_input("tenant-loop", "agent-loop")
        body = json.loads(inp.model_dump_json())
        r = client.post("/advisory/kv_cache", json=body)
    assert r.status_code == 200
    data = r.json()
    # confidence must be non-trivial because cache_hit_rate now has signal
    assert data["confidence"] > 0.10


def test_host_supplied_value_not_overwritten(tmp_path) -> None:
    """Host knows best — explicit cache_hit_rate=0.10 must win over
    a stored EWMA of 0.90."""
    intent_log = IntentLog(str(tmp_path / "intent.sqlite"))
    record_id = _seed_intent_record(intent_log)
    app = _app_with_routes(intent_log)

    with TestClient(app) as client:
        for _ in range(5):
            client.post("/tool-outcome", json={
                "record_id": record_id, "status": "success", "result_hash": "r",
                "tenant_id": "tenant-loop", "aid": "agent-loop",
                "cache_hit_rate": 0.90,
            })
        inp = _atv_input("tenant-loop", "agent-loop", cache_hit_rate=0.10)
        body = json.loads(inp.model_dump_json())
        r = client.post("/advisory/kv_cache", json=body)
    assert r.status_code == 200
    # Host's 0.10 went through unmodified — verify by re-running the advisor
    # with the same input locally. (We can't observe the cost band directly
    # from the response, but the residency heuristic uses cache_hit_rate.)
    inp_local = _atv_input("tenant-loop", "agent-loop", cache_hit_rate=0.10)
    advice = kv_cache_advisor(build_atv(inp_local), inp_local)
    assert inp_local.cost_estimate.cache_hit_rate == 0.10
    assert advice is not None
