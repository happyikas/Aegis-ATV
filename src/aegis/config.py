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

    aegis_judge_provider: Literal["haiku", "dummy", "local-phi"] = "dummy"
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

    # ATMU Write-Ahead Intent Log — separate SQLite store from audit (M10).
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


settings = Settings()
