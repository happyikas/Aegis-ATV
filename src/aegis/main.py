"""FastAPI application entrypoint (PLAN 6.8).

Endpoints:
    GET  /                    — web dashboard (single-page)
    GET  /static/*            — dashboard assets
    GET  /healthz             — liveness
    POST /evaluate            — main firewall + sign + audit
    POST /approve             — record human approval
    GET  /audit/{aid}         — return signed chain for one agent
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aegis import __version__
from aegis.api.approve import make_router as _approve_router
from aegis.api.audit_query import make_router as _audit_router
from aegis.api.evaluate import make_router as _evaluate_router
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.config import settings
from aegis.sign.ed25519 import load_or_create_key

_STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(
    *,
    key: Any | None = None,
    db: AuditDB | None = None,
    log: JsonlStore | None = None,
) -> FastAPI:
    """Build a FastAPI app. Override key/db/log for tests."""
    app = FastAPI(title="AegisData T2", version=__version__)

    real_key = key if key is not None else load_or_create_key(Path(settings.aegis_signing_key_path))
    real_db = db if db is not None else AuditDB(settings.aegis_audit_db)
    real_log = log if log is not None else JsonlStore(Path(settings.aegis_audit_jsonl))

    app.include_router(_evaluate_router(key=real_key, db=real_db, log=real_log))
    app.include_router(_approve_router(key=real_key, db=real_db, log=real_log))
    app.include_router(_audit_router(db=real_db))

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    return app


app = create_app()
