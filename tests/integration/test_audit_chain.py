"""Integration tests for the audit chain endpoint + tamper detection."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _payload(aid: str, *, tool: str = "read_file", args: str = '{"path":"./data/x"}') -> dict[str, Any]:
    return {
        "header": {
            "trace_id": str(uuid.uuid4()),
            "span_id": str(uuid.uuid4()),
            "tenant_id": "demo-tenant",
            "aid": aid,
            "ats": "ATV-2080-v1",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": "chain test",
        "plan_text": "many calls",
        "tool_name": tool,
        "tool_args_json": args,
        "safety_flags": {},
        "cost_estimate": {"exp_dollars": 0.001, "confidence": 0.9},
    }


def test_chain_grows_per_request_and_verifies(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    aid = "chain-agent"
    n = 25
    for _ in range(n):
        r = client.post("/evaluate", json=_payload(aid))
        assert r.status_code == 200
    audit = client.get(f"/audit/{aid}").json()
    assert audit["length"] == n
    assert audit["chain_valid"] is True
    assert audit["chain_error"] is None
    # prev_hash links must form a chain
    chain = audit["chain"]
    prev = "GENESIS"
    for rec in chain:
        assert rec["payload"]["prev_hash"] == prev
        prev = rec["this_hash"]


def test_chain_isolated_per_aid(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    for _ in range(3):
        client.post("/evaluate", json=_payload("agent-A"))
    for _ in range(2):
        client.post("/evaluate", json=_payload("agent-B"))
    a = client.get("/audit/agent-A").json()
    b = client.get("/audit/agent-B").json()
    assert a["length"] == 3
    assert b["length"] == 2
    assert a["chain_valid"] and b["chain_valid"]


def test_audit_unknown_agent_returns_empty(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    audit = client.get("/audit/never-existed").json()
    assert audit["length"] == 0
    assert audit["chain_valid"] is True
    assert audit["head"] == "GENESIS"


def test_approve_endpoint_appends_to_chain(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    # 1. transfer_funds → REQUIRE_APPROVAL
    eval_resp = client.post(
        "/evaluate",
        json=_payload("agent-appr", tool="transfer_funds", args='{"amount":100}'),
    ).json()
    assert eval_resp["decision"] == "REQUIRE_APPROVAL"
    # 2. record approval
    appr = client.post(
        "/approve",
        json={
            "atv_id": eval_resp["atv_id"],
            "aid": "agent-appr",
            "tenant_id": "demo-tenant",
            "approver": "alice",
            "decision": "ALLOW",
            "note": "manually reviewed",
        },
    )
    assert appr.status_code == 200
    assert appr.json()["ok"] is True
    # 3. chain has 2 records and is valid
    audit = client.get("/audit/agent-appr").json()
    assert audit["length"] == 2
    assert audit["chain_valid"] is True


def test_approve_rejects_invalid_decision(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post(
        "/approve",
        json={
            "atv_id": "x",
            "aid": "y",
            "tenant_id": "t",
            "approver": "a",
            "decision": "MAYBE",
        },
    )
    assert r.status_code == 400
