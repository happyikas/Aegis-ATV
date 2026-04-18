"""Milestone 1 smoke tests: package imports and config loads."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_package_imports() -> None:
    import aegis

    assert aegis.__version__


def test_config_defaults_to_dummy() -> None:
    from aegis.config import settings

    assert settings.aegis_embedding_provider == "dummy"
    assert settings.aegis_judge_provider == "dummy"
    assert settings.aegis_atv_version == "ATV-2080-v1"


def test_healthz() -> None:
    from aegis.main import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body
