"""Centralized configuration loaded from environment / .env (pydantic-settings)."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All AegisData runtime settings.

    Defaults are chosen so the service runs end-to-end with NO external API
    keys (dummy embedding + dummy judge). Switch to the real providers by
    overriding ``AEGIS_EMBEDDING_PROVIDER`` / ``AEGIS_JUDGE_PROVIDER`` in
    ``.env`` once the keys are available.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    aegis_atv_version: str = "ATV-2080-v1"
    aegis_tenant_default: str = "demo-tenant"

    aegis_embedding_provider: Literal["openai", "dummy", "bge-local"] = "dummy"
    aegis_embedding_model: str = "text-embedding-3-small"

    aegis_judge_provider: Literal[
        "haiku", "dummy", "local-phi", "attribution_head", "hybrid"
    ] = "dummy"
    aegis_judge_temperature: float = 0.0
    aegis_judge_seed: int = 42

    aegis_signing_key_path: str = "./keys/ed25519.pem"
    aegis_public_key_path: str = "./keys/ed25519.pub"

    # Cost Attestation Ledger — patent Claim 34 requires the cost-attestation
    # signing key slot be DISTINCT from the telemetry signing-key slot, so
    # customers/regulators can be granted cost-only access.
    aegis_cost_signing_key_path: str = "./keys/ed25519_cost.pem"
    aegis_cost_public_key_path: str = "./keys/ed25519_cost.pub"
    aegis_cost_ledger_db: str = "./data/cost_attestation.sqlite"
    aegis_cost_ledger_jsonl: str = "./data/cost_attestation.jsonl"

    aegis_audit_db: str = "./data/audit.sqlite"
    aegis_audit_jsonl: str = "./data/audit.jsonl"

    # ATMU (Agent Telemetry Management Unit) Write-Ahead Intent Log
    # — separate SQLite store from audit (M10).
    aegis_intent_log_db: str = "./data/intent_log.sqlite"

    aegis_policy_dir: str = "./policies"

    # v2.2 — Instruction baseline (poisoned-instruction detector).
    # Empty string disables step309 drift detection (default for the
    # sidecar service so the existing 650-test surface is unaffected;
    # local-mode + plugin packaging opt in by writing to .aegis/).
    aegis_instruction_baseline_path: str = ""
    aegis_instruction_root: str = "."

    # v2.3 — HW telemetry emulation (T3 SW-emulated double-check).
    # ``none`` (default) keeps the v2.2 zero-fill path and 650-test
    # surface untouched. ``sim`` switches on the deterministic
    # SHA3-seeded simulator in aegis.hw_telemetry, populating the
    # 200-D ATV HW band and feeding M12's cost-divergence escalation.
    aegis_hw_provider: Literal["none", "sim"] = "none"
    # Comma-separated attack mode list for demos / tests
    # (token_flops_mismatch, hbm_exfil, cost_underreport, thermal_spike,
    # network_exfil, iommu_violation). Unknown modes silently ignored.
    aegis_hw_inject_attack: str = ""

    # M15 — encrypted ATV journal (AES-GCM AEAD). The data key is
    # auto-generated on first run if the file is missing. T3 will seal
    # this under the hardware TEE.
    aegis_journal_data_key_path: str = "./keys/journal_data.key"
    aegis_journal_path: str = "./data/audit_encrypted.jsonl"

    # M16 — Hierarchical Agent Memory (T2 emulation, L3+L4).
    aegis_ham_db: str = "./data/ham.sqlite"
    aegis_ham_data_key_path: str = "./keys/ham_data.key"

    # v3.8 — Persistent perf-feedback EWMA snapshot.
    # Empty string disables the periodic snapshotter (default for tests
    # so they run hermetically). Production deployments set this to a
    # path under ./data/ to survive restarts.
    aegis_perf_feedback_snapshot_db: str = ""
    aegis_perf_feedback_snapshot_interval_sec: float = 30.0
    aegis_perf_feedback_snapshot_updates_threshold: int = 100

    # v3.8 — Group-commit on the encrypted ATV journal. ``False`` keeps
    # the v3.0 sync-per-append path. ``True`` enables batched fsync
    # (one fsync per up-to-batch_size records OR every interval_ms).
    aegis_journal_group_commit: bool = False
    aegis_journal_group_commit_batch_size: int = 100
    aegis_journal_group_commit_interval_ms: float = 1.0

    # v3.9 — Tiered archive of rotated journal segments. Empty ``cold_dir``
    # disables the migrator. Production: set cold_dir to an NFS mount or
    # to an S3 mount-point (s3fs). The S3ArchiveStub interface is
    # available for native object-store backends.
    aegis_tiered_archive_cold_dir: str = ""
    aegis_tiered_archive_rotate_bytes: int = 100 * 1024 * 1024  # 100 MB
    aegis_tiered_archive_rotate_seconds: float = 3600.0          # 1 hour
    aegis_tiered_archive_hot_retention_segments: int = 3
    aegis_tiered_archive_poll_seconds: float = 10.0

    # v4.0 — AuditPatrol (Claim 54). Periodic background integrity
    # check across all audit stores. Disabled by default; production
    # deployments enable by setting AEGIS_AUDIT_PATROL_ENABLED=true.
    aegis_audit_patrol_enabled: bool = False
    aegis_audit_patrol_full_interval_sec: float = 21600.0      # 6 hours
    aegis_audit_patrol_sample_interval_sec: float = 3600.0     # 1 hour
    aegis_audit_patrol_sequence_interval_sec: float = 300.0    # 5 minutes
    aegis_audit_patrol_consistency_interval_sec: float = 3600.0
    aegis_audit_patrol_cold_interval_sec: float = 86400.0      # 24 hours
    aegis_audit_patrol_sample_fraction: float = 0.01           # 1 %
    aegis_audit_patrol_cold_segments_per_run: int = 3
    aegis_audit_patrol_poll_seconds: float = 30.0


settings = Settings()
