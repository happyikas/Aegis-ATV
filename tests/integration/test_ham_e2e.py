"""End-to-end HAM API tests (M16)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_memory_then_recall_round_trip(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/ham/memory", json={
        "aid": "a", "tenant_id": "t", "body": {"text": "hello"}, "tags": ["greeting"],
    })
    assert r.status_code == 200
    obj_id = r.json()["object_id"]

    r2 = client.post("/ham/recall", json={"aid": "a", "tenant_id": "t"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["length"] == 1
    assert body["items"][0]["object_id"] == obj_id
    assert body["items"][0]["body"]["text"] == "hello"


def test_recall_with_tag_filter(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    client.post("/ham/memory", json={"aid": "a", "tenant_id": "t",
                                     "body": {"x": 1}, "tags": ["red"]})
    client.post("/ham/memory", json={"aid": "a", "tenant_id": "t",
                                     "body": {"x": 2}, "tags": ["blue"]})
    r = client.post("/ham/recall", json={
        "aid": "a", "tenant_id": "t", "tags": ["red"],
    })
    assert r.json()["length"] == 1
    assert r.json()["items"][0]["body"]["x"] == 1


def test_context_returns_bundle(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    for i in range(5):
        client.post("/ham/memory", json={"aid": "a", "tenant_id": "t",
                                          "body": {"i": i}})
    r = client.post("/ham/context", json={
        "aid": "a", "tenant_id": "t", "max_items": 3,
    })
    body = r.json()
    assert len(body["bundle"]["items"]) == 3
    assert len(body["source_ids"]) == 3


def test_forget_tombstones(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    obj_id = client.post("/ham/memory", json={
        "aid": "a", "tenant_id": "t", "body": {"x": 1},
    }).json()["object_id"]
    f = client.post("/ham/forget", json={
        "object_id": obj_id, "aid": "a", "tenant_id": "t",
    })
    assert f.status_code == 200
    # Recall should return empty.
    r = client.post("/ham/recall", json={"aid": "a", "tenant_id": "t"})
    assert r.json()["length"] == 0


def test_forget_unknown_404(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/ham/forget", json={
        "object_id": "never-existed", "aid": "a", "tenant_id": "t",
    })
    assert r.status_code == 404


def test_summarize_returns_counts(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    for tag in ("red", "red", "blue"):
        client.post("/ham/memory", json={"aid": "a", "tenant_id": "t",
                                          "body": {"t": tag}, "tags": [tag]})
    s = client.post("/ham/summarize", json={"aid": "a", "tenant_id": "t"}).json()
    assert s["item_count"] == 3
    assert s["tag_histogram"]["red"] == 2


def test_ground_binds_claim_to_references(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    o1 = client.post("/ham/memory", json={"aid": "a", "tenant_id": "t",
                                           "body": {"x": 1}}).json()["object_id"]
    o2 = client.post("/ham/memory", json={"aid": "a", "tenant_id": "t",
                                           "body": {"x": 2}}).json()["object_id"]
    g = client.post("/ham/ground", json={
        "aid": "a", "tenant_id": "t",
        "claim": "x ranges 1..2",
        "reference_ids": [o1, o2, "fake-id"],
    }).json()
    assert sorted(g["references"]) == sorted([o1, o2])
    assert g["missing"] == ["fake-id"]
    assert g["claim_hash"]


def test_stats_returns_counts(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    client.post("/ham/memory", json={"aid": "a", "tenant_id": "t", "body": {"x": 1}})
    client.post("/ham/memory", json={"aid": "a", "tenant_id": "t", "body": {"x": 2}})
    s = client.get("/ham/stats?tenant_id=t").json()
    assert s["total_objects"] == 2
    assert s["live"] == 2
    assert s["tombstoned"] == 0
