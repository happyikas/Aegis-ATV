"""FastAPI application entrypoint.

For Milestone 1 this only exposes ``/healthz`` so the docker image and CI
have something to smoke-test. The /evaluate and /audit endpoints are wired
in Milestone 6.
"""

from __future__ import annotations

from fastapi import FastAPI

from aegis import __version__

app = FastAPI(title="AegisData T2", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return {"ok": True, "version": __version__}
