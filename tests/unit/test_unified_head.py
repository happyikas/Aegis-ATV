"""Unit tests for src/aegis/judge/unified_head.py (v3.6)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.api.advisory import make_router
from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.judge.unified_head import (
    UnifiedHead,
    UnifiedVerdict,
    unified_advice_dict,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


def _atv_input(
    *,
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
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
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
        oversight={"operator_presence": operator_present},
    )


# ── Unified head ──────────────────────────────────────────────────────


def test_unified_head_returns_all_four_outputs() -> None:
    head = UnifiedHead()
    inp = _atv_input(progress=0.6, novelty=0.05, operator_present=1.0)
    atv = build_atv(inp)
    uv = head.evaluate_unified("", atv=atv, inp=inp)
    assert isinstance(uv, UnifiedVerdict)
    # All four outputs populated
    assert uv.verdict.decision in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}
    assert uv.kv_cache.residency_class in {"hot", "warm", "cold"}
    assert uv.scheduling.priority_class in {"interactive", "batch", "low"}
    assert uv.placement.kv_quantisation_dtype in {"f16", "q8_0", "q4_0"}


def test_unified_head_deterministic() -> None:
    head = UnifiedHead()
    inp = _atv_input(progress=0.6, novelty=0.05)
    atv = build_atv(inp)
    a = head.evaluate_unified("", atv=atv, inp=inp)
    b = head.evaluate_unified("", atv=atv, inp=inp)
    assert a.verdict.decision == b.verdict.decision
    assert a.verdict.confidence == b.verdict.confidence
    assert a.kv_cache.residency_class == b.kv_cache.residency_class
    assert a.scheduling.priority_class == b.scheduling.priority_class
    assert a.placement.kv_quantisation_dtype == b.placement.kv_quantisation_dtype
    assert a.unified_hash == b.unified_hash


def test_unified_hash_stable_across_calls() -> None:
    """unified_hash binds the four advisor hashes together; should
    not depend on input — only on the heads' versions."""
    head = UnifiedHead()
    inp_a = _atv_input(tool="Bash", args={"command": "ls"})
    inp_b = _atv_input(tool="read_file", args={"file_path": "/tmp/x"}, progress=0.6)
    atv_a = build_atv(inp_a)
    atv_b = build_atv(inp_b)
    h_a = head.evaluate_unified("", atv=atv_a, inp=inp_a).unified_hash
    h_b = head.evaluate_unified("", atv=atv_b, inp=inp_b).unified_hash
    assert h_a == h_b
    assert len(h_a) == 64  # SHA3-256 hex


def test_unified_total_latency_within_budget() -> None:
    head = UnifiedHead()
    inp = _atv_input(progress=0.5, operator_present=1.0)
    atv = build_atv(inp)
    uv = head.evaluate_unified("", atv=atv, inp=inp)
    # M13 + 3 advisors should still be <10ms
    assert uv.total_latency_ms < 10.0


def test_unified_head_rejects_missing_atv() -> None:
    head = UnifiedHead()
    with pytest.raises(ValueError, match="requires an ATV"):
        head.evaluate_unified("", atv=None, inp=None)  # type: ignore[arg-type]


def test_unified_verdict_path_matches_attribution_head() -> None:
    """The trust path must be bit-identical to v2.5 AttributionHead."""
    from aegis.judge.attribution_head import AttributionHead
    head = UnifiedHead()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /"})
    atv = build_atv(inp)
    uv = head.evaluate_unified("", atv=atv, inp=inp)
    standalone = AttributionHead().evaluate_full("", atv=atv, inp=inp)
    assert uv.verdict.decision == standalone.decision
    assert uv.verdict.confidence == standalone.confidence
    assert uv.verdict.subfield_attribution == standalone.subfield_attribution


# ── Endpoint ──────────────────────────────────────────────────────────


def test_advisory_unified_endpoint() -> None:
    app = FastAPI()
    app.include_router(make_router())
    inp = _atv_input(progress=0.6, novelty=0.05, operator_present=1.0)
    body = json.loads(inp.model_dump_json())
    with TestClient(app) as client:
        r = client.post("/advisory/unified", json=body)
    assert r.status_code == 200
    data = r.json()
    assert "verdict" in data
    assert "kv_cache" in data
    assert "scheduling" in data
    assert "placement" in data
    assert "unified_hash" in data
    assert data["verdict"]["decision"] in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}
    assert data["scheduling"]["priority_class"] == "interactive"


# ── Serialiser ────────────────────────────────────────────────────────


def test_unified_advice_dict_round_trip() -> None:
    head = UnifiedHead()
    inp = _atv_input(progress=0.6)
    uv = head.evaluate_unified("", atv=build_atv(inp), inp=inp)
    d = unified_advice_dict(uv)
    blob = json.dumps(d)  # must be JSON-serialisable
    decoded = json.loads(blob)
    assert decoded["verdict"]["decision"] == uv.verdict.decision
    assert decoded["unified_hash"] == uv.unified_hash
