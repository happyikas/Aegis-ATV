"""End-to-end Burn-in integration test (M11).

Verifies that /evaluate observations bump the controller, /burnin-status
reflects sample counts, and /burnin/graduate enforces gates.
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _payload(aid: str = "burnin-agent") -> dict:
    return {
        "header": {
            "trace_id": str(uuid.uuid4()),
            "span_id": str(uuid.uuid4()),
            "tenant_id": "demo-tenant",
            "aid": aid,
            "ats": "ATV-2080-v1",
            "schema_version": "ATV-2080-v1",
            "tier_profile": "T2",
            "cost_attestation_profile": "software",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": "burnin",
        "plan_text": "test",
        "tool_name": "read_file",
        "tool_args_json": '{"path":"./data/x.txt"}',
        "safety_flags": {},
        "cost_estimate": {
            "input_token_count": 100.0,
            "cumulative_dollars": 0.001,
            "forecasted_cost_to_completion": 0.01,
        },
    }


def test_burnin_status_starts_empty(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    body = client.get("/burnin-status").json()
    assert body["layers"] == []
    assert "expected_samples" in body
    assert "weights" in body


def test_burnin_observe_after_evaluate(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    for _ in range(3):
        r = client.post("/evaluate", json=_payload())
        assert r.status_code == 200
    body = client.get("/burnin-status").json()
    # 5 layer slots, each with samples=3 because all 3 calls share aid+tenant.
    assert len(body["layers"]) == 5
    for layer in body["layers"]:
        assert layer["samples"] == 3
        assert layer["phase"] == "observation"


def test_burnin_graduation_blocked_under_threshold(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    client.post("/evaluate", json=_payload())
    layers = client.get("/burnin-status").json()["layers"]
    key = layers[0]["key"]
    r = client.post("/burnin/graduate", json={"layer_key": key})
    assert r.status_code == 409
    # PR #159 — error envelope is now structured.
    assert "graduation blocked" in r.json()["error"]["message"]


def test_burnin_label_endpoint_updates_metrics(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    pl = _payload()
    r = client.post("/evaluate", json=pl)
    verdict = r.json()
    label = client.post(
        "/burnin/label",
        json={
            "inp": pl,
            "verdict": verdict,
            "ground_truth": "benign",
        },
    )
    assert label.status_code == 200
    body = client.get("/burnin-status").json()
    # ALLOW vs benign → true negative; tpr/fpr stay 0 but human_total_decisions increments
    for layer in body["layers"]:
        assert layer["fpr"] == 0.0


def test_burnin_label_invalid_ground_truth_400(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    pl = _payload()
    verdict = client.post("/evaluate", json=pl).json()
    r = client.post(
        "/burnin/label",
        json={"inp": pl, "verdict": verdict, "ground_truth": "maybe"},
    )
    assert r.status_code == 400


def test_evaluate_response_includes_composite_score_trace(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    v = client.post("/evaluate", json=_payload()).json()
    assert any("composite=" in t for t in v["step_traces"].values())
