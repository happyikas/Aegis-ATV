"""Tests for GET /source — security boundaries + correctness."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_source_full_file(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/source", params={"path": "schema.py", "max_lines": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "schema.py"
    assert body["start_line"] == 1
    assert body["total_lines"] > 50
    assert "ATV_VERSION" in body["snippet"]


def test_source_function_slice(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get(
        "/source",
        params={"path": "atv/builder.py", "function": "build_atv", "max_lines": 30},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["function"] == "build_atv"
    assert body["start_line"] >= 1
    assert "def build_atv" in body["snippet"]
    # Should NOT bleed into the next def in the file.
    next_def_count = body["snippet"].count("def ")
    assert next_def_count == 1, f"sliced too far, got {next_def_count} def-lines"


def test_source_function_not_found(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get(
        "/source",
        params={"path": "atv/builder.py", "function": "nope_does_not_exist"},
    )
    assert r.status_code == 404


def test_source_path_traversal_rejected(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    for evil in ["../../../etc/passwd", "../../etc/passwd", "../config.py"]:
        r = client.get("/source", params={"path": evil})
        assert r.status_code == 400, f"path traversal accepted: {evil}"


def test_source_absolute_path_rejected(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/source", params={"path": "/etc/passwd"})
    assert r.status_code == 400


def test_source_non_python_rejected(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    # web/static/index.html exists in the package — try to read it
    r = client.get("/source", params={"path": "web/static/index.html"})
    assert r.status_code == 400
    assert "py" in r.json()["detail"].lower()


def test_source_missing_file_404(aegis_app: FastAPI) -> None:
    client = TestClient(aegis_app)
    r = client.get("/source", params={"path": "no_such_file.py"})
    assert r.status_code == 404


def test_source_known_step_functions_resolve(aegis_app: FastAPI) -> None:
    """Sanity check: every firewall step's run() is locatable.

    The Theater UI relies on these being present. If a step file gets
    renamed and this test breaks, the UI's code-path links break too.
    """
    client = TestClient(aegis_app)
    cases = [
        ("firewall/step310_args.py", "run"),
        ("firewall/step320_blast.py", "run"),
        ("firewall/step330_human.py", "run"),
        ("firewall/step335_cost.py", "run"),
        ("firewall/step340_policy.py", "run"),
        ("firewall/core.py", "run_firewall"),
        ("atv/builder.py", "build_atv"),
        ("api/evaluate.py", "_evaluate_impl"),
        ("sign/ed25519.py", "sign_atv"),
    ]
    for path, fn in cases:
        r = client.get("/source", params={"path": path, "function": fn})
        assert r.status_code == 200, f"{path}::{fn} → {r.status_code} {r.text}"
        assert f"def {fn}" in r.json()["snippet"]
