"""End-to-end tests for /attestation + burn_in_id propagation into audit."""

from __future__ import annotations

import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_attestation_endpoint_returns_signed_measurement(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/attestation")
    assert r.status_code == 200
    body = r.json()
    assert body["burn_in_id"] and len(body["burn_in_id"]) == 64  # SHA3-256 hex
    assert body["aegis_version"]
    assert body["atv_version"] == "ATV-2080-v1"
    assert body["signed"]["algorithm"] == "Ed25519"
    assert len(body["signed"]["signature"]) == 128  # Ed25519 sig 64 bytes hex

    # Verify the signature with the embedded public key.
    pub = serialization.load_pem_public_key(body["public_key_pem"].encode())
    assert isinstance(pub, Ed25519PublicKey)
    pub.verify(bytes.fromhex(body["signed"]["signature"]), body["burn_in_id"].encode())


def test_attestation_layers_complete(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    body = client.get("/attestation").json()
    layers = body["layers"]
    assert layers["L3_code"]["files_counted"] >= 1
    assert len(layers["L3_code"]["hash"]) == 64
    assert len(layers["L4_config"]["hash"]) == 64
    assert len(layers["L5_key_binding"]["hash"]) == 64
    assert len(layers["L5_key_binding"]["public_key_fingerprint"]) == 64


def test_burn_in_id_propagates_into_audit_chain(aegis_app: FastAPI) -> None:
    """Every audit record's header should carry the current burn_in_id."""
    client = TestClient(aegis_app)
    expected = client.get("/attestation").json()["burn_in_id"]

    aid = "burnin-test-" + uuid.uuid4().hex[:8]
    r = client.post(
        "/evaluate",
        json={
            "header": {
                "trace_id": str(uuid.uuid4()),
                "span_id": str(uuid.uuid4()),
                "tenant_id": "demo-tenant",
                "aid": aid,
                "ats": "ATV-2080-v1",
                "timestamp_ns": time.time_ns(),
                # Note: no burn_in_id provided — server must inject.
            },
            "agent_state_text": "burn-in propagation test",
            "plan_text": "read a safe file",
            "tool_name": "read_file",
            "tool_args_json": '{"path":"./data/x.txt"}',
            "safety_flags": {},
            "cost_estimate": {"cumulative_dollars": 0.001, "forecasted_cost_to_completion": 0.01},
        },
    )
    assert r.status_code == 200

    chain = client.get(f"/audit/{aid}").json()
    assert chain["length"] == 1
    record = chain["chain"][0]
    assert record["payload"]["header"]["burn_in_id"] == expected


def test_caller_supplied_burn_in_wins(aegis_app: FastAPI) -> None:
    """Cross-attesting a different burn_in_id must round-trip verbatim."""
    client = TestClient(aegis_app)
    custom = "deadbeef" * 8  # 64 hex chars
    aid = "burnin-override-" + uuid.uuid4().hex[:8]
    r = client.post(
        "/evaluate",
        json={
            "header": {
                "trace_id": str(uuid.uuid4()),
                "span_id": str(uuid.uuid4()),
                "tenant_id": "demo-tenant",
                "aid": aid,
                "ats": "ATV-2080-v1",
                "timestamp_ns": time.time_ns(),
                "burn_in_id": custom,
            },
            "agent_state_text": "override test",
            "plan_text": "read a safe file",
            "tool_name": "read_file",
            "tool_args_json": '{"path":"./data/x.txt"}',
            "safety_flags": {},
            "cost_estimate": {"cumulative_dollars": 0.001, "forecasted_cost_to_completion": 0.01},
        },
    )
    assert r.status_code == 200
    chain = client.get(f"/audit/{aid}").json()
    assert chain["chain"][0]["payload"]["header"]["burn_in_id"] == custom


def test_healthz_includes_burn_in_id(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    h = client.get("/healthz").json()
    a = client.get("/attestation").json()
    assert h["burn_in_id"] == a["burn_in_id"]
