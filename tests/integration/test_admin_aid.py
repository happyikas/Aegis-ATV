"""End-to-end tests for /admin/aid endpoints (M14)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

ADMIN_TOKEN = "dev-admin-token"   # mirrors AEGIS_ADMIN_TOKEN default


def test_list_quarantined_starts_empty(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/admin/aid")
    assert r.status_code == 200
    assert r.json()["quarantined"] == []


def test_get_unknown_aid_returns_normal_state(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/admin/aid/never-existed")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "normal"
    assert body["violations"] == 0


def test_release_requires_admin_token(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post("/admin/aid/release", json={"aid": "x", "reason": "test"})
    assert r.status_code == 401


def test_release_unknown_aid_returns_404(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.post(
        "/admin/aid/release",
        json={"aid": "never-existed", "reason": "test"},
        headers={"X-Aegis-Admin-Token": ADMIN_TOKEN},
    )
    assert r.status_code == 404
