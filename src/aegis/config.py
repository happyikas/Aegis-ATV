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

    aegis_audit_db: str = "./data/audit.sqlite"
    aegis_audit_jsonl: str = "./data/audit.jsonl"

    aegis_policy_dir: str = "./policies"


settings = Settings()
