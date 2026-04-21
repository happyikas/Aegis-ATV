"""End-to-end FastAPI tests covering ALLOW / BLOCK / REQUIRE_APPROVAL flows."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _payload(
    *,
    aid: str = "agent-e2e",
    tool_name: str = "read_file",
    tool_args_json: str = '{"path":"./data/x.txt"}',
    safety_flags: dict[str, float] | None = None,
    cost: dict[str, float] | None = None,
) -> dict[str, Any]:
    return {
        "header": {
            "trace_id": str(uuid.uuid4()),
            "span_id": str(uuid.uuid4()),
            "tenant_id": "demo-tenant",
            "aid": aid,
            "ats": "ATV-2080-v1",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": "e2e test",
        "plan_text": "do the thing safely",
        "tool_name": tool_name,
        "tool_args_json": tool_args_json,
        "safety_flags": safety_flags or {},
        "cost_estimate": cost or {
            "input_token_count": 100.0,
            "cumulative_dollars": 0.001,
            "forecasted_cost_to_completion": 0.01,
        },
    }


def test_healthz(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_evaluate_allow_path(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/evaluate", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOW"
    assert body["atv_id"]
    assert body["signature"]
    assert "step340_policy.run" in next(iter(body["step_traces"].keys())) or any(
        "step340" in k for k in body["step_traces"]
    )


def test_evaluate_block_on_dangerous_args(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post(
        "/evaluate",
        json=_payload(tool_args_json='{"cmd":"rm -rf /"}'),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "BLOCK"


def test_evaluate_block_on_policy_match(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post(
        "/evaluate",
        json=_payload(tool_args_json='{"path":"/etc/shadow-mockpath"}'),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "BLOCK"


def test_evaluate_require_approval_for_high_blast(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post(
        "/evaluate",
        json=_payload(
            tool_name="transfer_funds",
            tool_args_json='{"to":"acct-b","amount":500}',
        ),
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "REQUIRE_APPROVAL"


def test_evaluate_require_approval_for_budget_overrun(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post(
        "/evaluate",
        json=_payload(
            tool_name="write_file",
            tool_args_json='{"path":"./data/big.bin"}',
            # Forecasted cost over $1.0 ceiling → step 335 APPROVAL.
            cost={
                "cumulative_dollars": 0.05,
                "forecasted_cost_to_completion": 5.0,
                "budget_burn_rate": 0.9,
            },
        ),
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "REQUIRE_APPROVAL"
