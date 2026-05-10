"""FastAPI application entrypoint (PLAN 6.8 + Section 10).

Endpoints:
    GET  /                    — web dashboard (single-page)
    GET  /static/*            — dashboard assets
    GET  /healthz             — liveness
    GET  /attestation         — signed Burn-in L3-L5 measurement
    POST /evaluate            — main firewall + sign + audit (+ ATMU intent;
                                ATMU = Agent Telemetry Management Unit, §5A)
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
from aegis.api.admin_aid import make_router as _admin_aid_router
from aegis.api.advisory import make_router as _advisory_router
from aegis.api.approve import make_router as _approve_router
from aegis.api.attestation import make_router as _attestation_router
from aegis.api.audit_query import make_router as _audit_router
from aegis.api.burnin_status import make_router as _burnin_router
from aegis.api.cost_attestation import make_router as _cost_router
from aegis.api.evaluate import make_router as _evaluate_router
from aegis.api.ham import make_router as _ham_router
from aegis.api.replay import make_router as _replay_router
from aegis.api.source import make_router as _source_router
from aegis.api.tool_outcome import make_router as _tool_outcome_router
from aegis.atmu import IntentLog
from aegis.attest.burn_in import BurnInMeasurement, compute_burn_in
from aegis.audit.encrypted_journal import EncryptedJournal, load_or_create_data_key
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.burnin import BurnInController
from aegis.config import settings
from aegis.cost.ledger import CostAttestationLedger
from aegis.firewall.step315_aid_auth import get_circuit_breaker
from aegis.ham import HierarchicalMemoryStore
from aegis.sign.ed25519 import load_or_create_key

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_PACKAGE_ROOT = Path(__file__).parent  # src/aegis/


def create_app(
    *,
    key: Any | None = None,
    db: AuditDB | None = None,
    log: JsonlStore | None = None,
    intent_log: IntentLog | None = None,
    burnin_controller: BurnInController | None = None,
    cost_ledger: CostAttestationLedger | None = None,
    encrypted_journal: EncryptedJournal | None = None,
    ham_store: HierarchicalMemoryStore | None = None,
    measurement: BurnInMeasurement | None = None,
) -> FastAPI:
    """Build a FastAPI app. Override stores/keys for tests."""
    app = FastAPI(title="AegisData T2", version=__version__)

    real_key = key if key is not None else load_or_create_key(Path(settings.aegis_signing_key_path))
    real_db = db if db is not None else AuditDB(settings.aegis_audit_db)
    real_log = log if log is not None else JsonlStore(Path(settings.aegis_audit_jsonl))
    real_intent_log = (
        intent_log if intent_log is not None else IntentLog(settings.aegis_intent_log_db)
    )
    real_burnin = burnin_controller if burnin_controller is not None else BurnInController()
    # Gap C (#146) — expose the controller on app.state so tests and
    # future surfaces (`aegis burnin status --by-provider` via API)
    # can inspect per-(aid × provider) baselines without re-importing.
    app.state.burnin_controller = real_burnin
    # Claim 34 — cost-attestation signing key is DISTINCT from the
    # telemetry signing key. Auto-create on first run.
    if cost_ledger is None:
        cost_key = load_or_create_key(Path(settings.aegis_cost_signing_key_path))
        cost_ledger = CostAttestationLedger(
            db_path=settings.aegis_cost_ledger_db,
            jsonl_path=Path(settings.aegis_cost_ledger_jsonl),
            signing_key=cost_key,
        )
    # M15 — encrypted ATV journal. Auto-create the data key on first run.
    # v3.8 — Optional group-commit wrapper for higher throughput.
    journal_data_key: bytes | None = None
    if encrypted_journal is None:
        data_key = load_or_create_data_key(Path(settings.aegis_journal_data_key_path))
        journal_data_key = data_key
        if settings.aegis_journal_group_commit:
            from aegis.audit.group_commit import make_journal
            encrypted_journal = make_journal(  # type: ignore[assignment]
                path=Path(settings.aegis_journal_path),
                data_key=data_key,
                group_commit=True,
                batch_size=settings.aegis_journal_group_commit_batch_size,
                interval_ms=settings.aegis_journal_group_commit_interval_ms,
            )
        else:
            encrypted_journal = EncryptedJournal(
                path=Path(settings.aegis_journal_path),
                data_key=data_key,
            )
    # M16 — Hierarchical Agent Memory (T2 L3+L4 emulation).
    if ham_store is None:
        ham_key = load_or_create_data_key(Path(settings.aegis_ham_data_key_path))
        ham_store = HierarchicalMemoryStore(
            db_path=settings.aegis_ham_db,
            data_key=ham_key,
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
            intent_log=real_intent_log, burnin_controller=real_burnin,
            cost_ledger=cost_ledger, encrypted_journal=encrypted_journal,
            burn_in_id=real_measurement.burn_in_id,
        )
    )
    app.include_router(_approve_router(key=real_key, db=real_db, log=real_log))
    app.include_router(_audit_router(db=real_db))
    app.include_router(_attestation_router(measurement=real_measurement))
    app.include_router(_source_router(package_root=_PACKAGE_ROOT))
    app.include_router(_tool_outcome_router(intent_log=real_intent_log))
    app.include_router(_burnin_router(controller=real_burnin))
    app.include_router(_cost_router(ledger=cost_ledger))
    app.include_router(_admin_aid_router(breaker=get_circuit_breaker()))
    app.include_router(_replay_router(journal=encrypted_journal))
    app.include_router(_ham_router(store=ham_store))
    app.include_router(_advisory_router())

    # v3.8 — Persistent perf-feedback snapshotter. Disabled by default
    # (empty path); production deployments enable it via
    # AEGIS_PERF_FEEDBACK_SNAPSHOT_DB.
    snap_db = settings.aegis_perf_feedback_snapshot_db
    if snap_db:
        from aegis.performance import (
            PerfFeedbackSnapshotter,
            SnapshotterConfig,
            get_default_store,
        )
        snapshotter = PerfFeedbackSnapshotter(
            store=get_default_store(),
            db_path=snap_db,
            config=SnapshotterConfig(
                interval_sec=settings.aegis_perf_feedback_snapshot_interval_sec,
                updates_per_snapshot=settings.aegis_perf_feedback_snapshot_updates_threshold,
            ),
        )
        snapshotter.load_into_store()  # restore prior EWMA on boot
        snapshotter.start()
        app.state.perf_feedback_snapshotter = snapshotter

        @app.on_event("shutdown")
        def _stop_snapshotter() -> None:
            snapshotter.close()

    # v3.9 — Tiered archive migrator. Disabled by default (empty cold_dir);
    # production deployments enable via AEGIS_TIERED_ARCHIVE_COLD_DIR.
    cold_dir = settings.aegis_tiered_archive_cold_dir
    if cold_dir:
        from aegis.audit.tiered_archive import (
            ArchivePolicy,
            FilesystemArchive,
            TieredArchiveMigrator,
        )
        migrator = TieredArchiveMigrator(
            live_path=Path(settings.aegis_journal_path),
            backend=FilesystemArchive(cold_dir=Path(cold_dir)),
            policy=ArchivePolicy(
                rotate_bytes=settings.aegis_tiered_archive_rotate_bytes,
                rotate_seconds=settings.aegis_tiered_archive_rotate_seconds,
                hot_retention_segments=settings.aegis_tiered_archive_hot_retention_segments,
                poll_seconds=settings.aegis_tiered_archive_poll_seconds,
            ),
        )
        migrator.start()
        app.state.tiered_archive_migrator = migrator

        @app.on_event("shutdown")
        def _stop_migrator() -> None:
            migrator.stop()

    # v4.0 — AuditPatrol (Claim 54). Background integrity check across
    # the audit chain, JSONL mirror, encrypted journal, ATMU intent log,
    # cost ledger, and (when configured) the v3.9 cold tier.
    patrol = None
    if settings.aegis_audit_patrol_enabled:
        from aegis.audit.patrol import AuditPatrol, PatrolConfig
        patrol = AuditPatrol(
            public_key=real_key.public_key(),
            audit_db=real_db,
            jsonl=real_log,
            intent_log=real_intent_log,
            cost_ledger=cost_ledger,
            encrypted_journal=encrypted_journal,
            cold_archive_dir=(
                Path(settings.aegis_tiered_archive_cold_dir)
                if settings.aegis_tiered_archive_cold_dir else None
            ),
            cold_data_key=journal_data_key,
            config=PatrolConfig(
                full_interval_sec=settings.aegis_audit_patrol_full_interval_sec,
                sample_interval_sec=settings.aegis_audit_patrol_sample_interval_sec,
                sequence_interval_sec=settings.aegis_audit_patrol_sequence_interval_sec,
                consistency_interval_sec=settings.aegis_audit_patrol_consistency_interval_sec,
                cold_interval_sec=settings.aegis_audit_patrol_cold_interval_sec,
                sample_fraction=settings.aegis_audit_patrol_sample_fraction,
                cold_segments_per_run=settings.aegis_audit_patrol_cold_segments_per_run,
                poll_seconds=settings.aegis_audit_patrol_poll_seconds,
            ),
        )
        patrol.start()
        app.state.audit_patrol = patrol

        @app.on_event("shutdown")
        def _stop_patrol() -> None:
            if patrol is not None:
                patrol.stop()

    from aegis.api.audit_patrol import make_router as _audit_patrol_router
    app.include_router(_audit_patrol_router(patrol=patrol))

    # v4.3 — Compliance evidence collection (Claim 57).
    from aegis.api.compliance import make_router as _compliance_router
    app.include_router(_compliance_router(
        audit_db=real_db,
        intent_log=real_intent_log,
        cost_ledger=cost_ledger,
        encrypted_journal=encrypted_journal,
    ))

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
