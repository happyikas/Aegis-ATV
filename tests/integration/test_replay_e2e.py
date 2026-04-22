"""End-to-end forensic replay tests (M15)."""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _payload(aid: str = "replay-agent") -> dict:
    return {
        "header": {
            "trace_id": str(uuid.uuid4()),
            "span_id":  str(uuid.uuid4()),
            "tenant_id": "demo-tenant",
            "aid": aid,
            "ats": "ATV-2080-v1",
            "schema_version": "ATV-2080-v1",
            "tier_profile": "T2",
            "cost_attestation_profile": "software",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": "replay e2e",
        "plan_text": "test forensic replay",
        "tool_name": "read_file",
        "tool_args_json": '{"path":"./data/x.txt"}',
        "safety_flags": {},
        "cost_estimate": {
            "input_token_count": 100.0,
            "cumulative_dollars": 0.001,
            "forecasted_cost_to_completion": 0.01,
        },
    }


def test_replay_returns_available(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/forensic/replay")
    assert r.status_code == 200
    assert r.json()["available"] is True


def test_replay_after_evaluate_decrypts_records(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    aid = "replay-" + uuid.uuid4().hex[:6]
    for _ in range(3):
        client.post("/evaluate", json=_payload(aid=aid))
    rep = client.get("/forensic/replay").json()
    assert rep["available"] is True
    assert rep["decrypted_count"] >= 3
    assert rep["tampered_count"] == 0
    assert aid in rep["aids_seen"]
    assert rep["per_aid_chain_valid"][aid] is True


def test_replay_includes_aid_in_per_aid_head(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    aid = "head-aid-" + uuid.uuid4().hex[:6]
    client.post("/evaluate", json=_payload(aid=aid))
    rep = client.get("/forensic/replay").json()
    assert aid in rep["per_aid_head"]
    assert len(rep["per_aid_head"][aid]) >= 32  # SHA3 hex
