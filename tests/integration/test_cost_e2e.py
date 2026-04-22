"""End-to-end Cost Attestation Ledger tests (M12)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _payload(
    *,
    aid: str = "cost-agent",
    tool: str = "read_file",
    cost: dict[str, float] | None = None,
) -> dict[str, Any]:
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
            "model_hash": "claude-haiku-4-5",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": "cost test",
        "plan_text": "plan",
        "tool_name": tool,
        "tool_args_json": '{"path":"./data/x.txt"}',
        "safety_flags": {},
        "cost_estimate": cost or {
            "input_token_count": 100.0,
            "cumulative_dollars": 0.001,
            "forecasted_cost_to_completion": 0.01,
        },
    }


def test_cost_attestation_record_appended_per_evaluate(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    aid = "cost-attest-" + uuid.uuid4().hex[:8]
    for _ in range(3):
        r = client.post("/evaluate", json=_payload(aid=aid))
        assert r.status_code == 200
    resp = client.get(f"/cost-attestation/{aid}").json()
    assert resp["length"] == 3
    assert resp["chain_valid"] is True
    # Each record carries an ATV commitment + signature + sw_cost_metrics + divergence
    for rec in resp["records"]:
        assert len(rec["atv_commitment"]) == 64       # SHA3-256 hex
        assert rec["signature"]
        assert "input_token_count" in rec["sw_cost_metrics"]
        assert "token_to_flops" in rec["divergence"]
        # T2: all three divergences are 0
        assert rec["divergence"]["token_to_flops"] == 0.0
        assert rec["divergence"]["memory_cost"] == 0.0
        assert rec["divergence"]["dollar_cost"] == 0.0


def test_cost_chain_links_via_prev_hash(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    aid = "cost-chain-" + uuid.uuid4().hex[:8]
    for _ in range(4):
        client.post("/evaluate", json=_payload(aid=aid))
    resp = client.get(f"/cost-attestation/{aid}").json()
    prev = "GENESIS"
    for rec in resp["records"]:
        assert rec["prev_hash"] == prev
        prev = rec["this_hash"]


def test_cost_records_isolated_per_aid(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    a, b = "aid-A-" + uuid.uuid4().hex[:6], "aid-B-" + uuid.uuid4().hex[:6]
    client.post("/evaluate", json=_payload(aid=a))
    client.post("/evaluate", json=_payload(aid=a))
    client.post("/evaluate", json=_payload(aid=b))
    assert client.get(f"/cost-attestation/{a}").json()["length"] == 2
    assert client.get(f"/cost-attestation/{b}").json()["length"] == 1


def test_by_tenant_aggregates_across_aids(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    a, b = "aid-x-" + uuid.uuid4().hex[:6], "aid-y-" + uuid.uuid4().hex[:6]
    client.post("/evaluate", json=_payload(aid=a))
    client.post("/evaluate", json=_payload(aid=b))
    resp = client.get("/cost-attestation/by-tenant/demo-tenant").json()
    assert resp["length"] >= 2


def test_cost_record_signature_is_distinct_from_audit_signature(aegis_app: FastAPI) -> None:
    """Claim 34 — cost-attestation signing key is DISTINCT from audit key,
    so the same /evaluate call produces TWO different signatures."""
    client = TestClient(aegis_app)
    aid = "key-isolation-" + uuid.uuid4().hex[:6]
    v = client.post("/evaluate", json=_payload(aid=aid)).json()
    audit_sig = v["signature"]
    cost_resp = client.get(f"/cost-attestation/{aid}").json()
    cost_sig = cost_resp["records"][0]["signature"]
    assert audit_sig != cost_sig
    assert len(audit_sig) == 128 and len(cost_sig) == 128


def test_missing_aid_returns_empty_chain(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    resp = client.get("/cost-attestation/never-existed").json()
    assert resp["length"] == 0
    assert resp["chain_valid"] is True   # empty chain is trivially valid
    assert resp["head"] == "GENESIS"
