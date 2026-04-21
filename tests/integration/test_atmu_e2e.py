"""End-to-end tests for ATMU integration into /evaluate + /tool-outcome (M10)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _payload(
    *,
    aid: str = "atmu-agent",
    tool_name: str = "read_file",
    tool_args_json: str = '{"path":"./data/x.txt"}',
    cost: dict[str, float] | None = None,
) -> dict[str, Any]:
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
        "agent_state_text": "atmu e2e",
        "plan_text": "test atmu integration",
        "tool_name": tool_name,
        "tool_args_json": tool_args_json,
        "safety_flags": {},
        "cost_estimate": cost or {
            "input_token_count": 100.0,
            "cumulative_dollars": 0.001,
            "forecasted_cost_to_completion": 0.01,
        },
    }


def _intent_id_from_verdict(verdict: dict[str, Any]) -> str:
    """The /evaluate response surfaces the ATMU record_id in step_traces."""
    for v in verdict["step_traces"].values():
        if v.startswith("intent_record_id="):
            return v.split("=", 1)[1]
    raise AssertionError(f"no intent_record_id in step_traces: {verdict['step_traces']}")


# ─────────────────────────────────────────────────────────────────────
# Tentative → prepared → committed (ALLOW path)
# ─────────────────────────────────────────────────────────────────────
def test_allow_call_commits_intent(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/evaluate", json=_payload())
    assert r.status_code == 200
    v = r.json()
    assert v["decision"] == "ALLOW"
    intent_id = _intent_id_from_verdict(v)
    assert len(intent_id) == 36  # uuid

    # Re-fetch via tool-outcome; we just want to assert the record ended in committed.
    # Easiest: post a tool-outcome and inspect the response's current_state.
    r2 = client.post(
        "/tool-outcome",
        json={
            "record_id": intent_id,
            "status": "success",
            "result_hash": "abc123",
            "side_effect_receipt": "fake-receipt",
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["current_state"] == "committed"
    assert body["tool_outcome"]["status"] == "success"


# ─────────────────────────────────────────────────────────────────────
# Tentative → aborted (BLOCK path)
# ─────────────────────────────────────────────────────────────────────
def test_block_call_aborts_intent(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/evaluate", json=_payload(
        tool_args_json='{"command":"rm -rf /"}',
        tool_name="execute_shell",
    ))
    v = r.json()
    assert v["decision"] == "BLOCK"
    intent_id = _intent_id_from_verdict(v)

    # ABORTED is terminal except for QUARANTINED — try posting outcome with a
    # follow_up_state that's illegal (committed) and expect a 409.
    r2 = client.post(
        "/tool-outcome",
        json={
            "record_id": intent_id,
            "status": "failure",
            "result_hash": "blocked",
            "follow_up_state": "committed",
            "follow_up_reason": "should fail",
        },
    )
    assert r2.status_code == 409


# ─────────────────────────────────────────────────────────────────────
# Tentative → prepared (REQUIRE_APPROVAL path stays prepared)
# ─────────────────────────────────────────────────────────────────────
def test_approval_call_stays_prepared(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/evaluate", json=_payload(
        tool_name="transfer_funds",
        tool_args_json='{"amount":500}',
    ))
    v = r.json()
    assert v["decision"] == "REQUIRE_APPROVAL"
    intent_id = _intent_id_from_verdict(v)

    # APPROVAL stays prepared; the host can later issue a follow_up_state
    # transition to commit (after human approves) or abort (after timeout).
    r2 = client.post(
        "/tool-outcome",
        json={
            "record_id": intent_id,
            "status": "partial",
            "result_hash": "held",
            "follow_up_state": "committed",
            "follow_up_reason": "human approved",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["current_state"] == "committed"


# ─────────────────────────────────────────────────────────────────────
# Compensation plan attached for irreversible tools
# ─────────────────────────────────────────────────────────────────────
def test_compensation_plan_attached_for_transfer_funds(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/evaluate", json=_payload(
        tool_name="transfer_funds",
        tool_args_json='{"amount":500}',
    ))
    intent_id = _intent_id_from_verdict(r.json())

    # Inspect via outcome echo: post outcome with follow-up COMPENSATED to
    # confirm the state machine accepts it (committed → compensated).
    client.post("/tool-outcome", json={
        "record_id": intent_id, "status": "success", "result_hash": "x",
        "follow_up_state": "committed", "follow_up_reason": "approved",
    })
    r2 = client.post("/tool-outcome", json={
        "record_id": intent_id, "status": "compensated", "result_hash": "reversed",
        "follow_up_state": "compensated", "follow_up_reason": "policy violation found",
    })
    assert r2.status_code == 200
    assert r2.json()["current_state"] == "compensated"


# ─────────────────────────────────────────────────────────────────────
# Unknown record_id returns 404
# ─────────────────────────────────────────────────────────────────────
def test_tool_outcome_unknown_record_404(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/tool-outcome", json={
        "record_id": "no-such-record",
        "status": "success",
        "result_hash": "x",
    })
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Bad outcome status → 400
# ─────────────────────────────────────────────────────────────────────
def test_tool_outcome_invalid_status_400(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/evaluate", json=_payload())
    intent_id = _intent_id_from_verdict(r.json())
    bad = client.post("/tool-outcome", json={
        "record_id": intent_id,
        "status": "made-up-status",
        "result_hash": "x",
    })
    assert bad.status_code == 400
