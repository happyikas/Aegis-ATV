"""FastAPI application entrypoint (PLAN 6.8 + Section 10).

Endpoints:
    GET  /                    — web dashboard (single-page)
    GET  /static/*            — dashboard assets
    GET  /healthz             — liveness
    GET  /attestation         — signed Burn-in L3-L5 measurement
    POST /evaluate            — main firewall + sign + audit (+ ATMU intent)
    POST /approve             — record human approval
    POST /tool-outcome        — host posts post-release outcome (M10)
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
from aegis.api.attestation import make_router as _attestation_router
from aegis.api.audit_query import make_router as _audit_router
from aegis.api.evaluate import make_router as _evaluate_router
from aegis.api.source import make_router as _source_router
from aegis.api.tool_outcome import make_router as _tool_outcome_router
from aegis.atmu import IntentLog
from aegis.attest.burn_in import BurnInMeasurement, compute_burn_in
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.config import settings
from aegis.sign.ed25519 import load_or_create_key

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_PACKAGE_ROOT = Path(__file__).parent  # src/aegis/


def create_app(
    *,
    key: Any | None = None,
    db: AuditDB | None = None,
    log: JsonlStore | None = None,
    intent_log: IntentLog | None = None,
    measurement: BurnInMeasurement | None = None,
) -> FastAPI:
    """Build a FastAPI app. Override key/db/log/intent_log/measurement for tests."""
    app = FastAPI(title="AegisData T2", version=__version__)

    real_key = key if key is not None else load_or_create_key(Path(settings.aegis_signing_key_path))
    real_db = db if db is not None else AuditDB(settings.aegis_audit_db)
    real_log = log if log is not None else JsonlStore(Path(settings.aegis_audit_jsonl))
    real_intent_log = (
        intent_log if intent_log is not None else IntentLog(settings.aegis_intent_log_db)
    )
    real_measurement = measurement if measurement is not None else compute_burn_in(
        code_root=_PACKAGE_ROOT,
        policy_dir=Path(settings.aegis_policy_dir),
        embedding_provider=settings.aegis_embedding_provider,
        judge_provider=settings.aegis_judge_provider,
        public_key=real_key.public_key(),
        signing_key=real_key,
    )

    app.include_router(
        _evaluate_router(
            key=real_key, db=real_db, log=real_log,
            intent_log=real_intent_log, burn_in_id=real_measurement.burn_in_id,
        )
    )
    app.include_router(_approve_router(key=real_key, db=real_db, log=real_log))
    app.include_router(_audit_router(db=real_db))
    app.include_router(_attestation_router(measurement=real_measurement))
    app.include_router(_source_router(package_root=_PACKAGE_ROOT))
    app.include_router(_tool_outcome_router(intent_log=real_intent_log))

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

        @app.get("/theater", include_in_schema=False)
        def theater() -> FileResponse:
            return FileResponse(_STATIC_DIR / "theater.html")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "version": __version__,
            "burn_in_id": real_measurement.burn_in_id,
        }

    return app


app = create_app()
