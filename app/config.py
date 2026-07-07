"""Application settings via pydantic-settings (DB/Redis URLs, model names, Langfuse keys, HITL threshold)."""
# Implemented in M1. Langfuse keys are loaded here but unused until M6; the HITL threshold until M2.

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config. Every value can come from the environment / .env; defaults suit local dev.

    Note: `app/db/database.py` deliberately reads DATABASE_URL from os.environ itself (kept
    independent of this module so M0 scripts and Alembic run standalone) — the field here exists
    for health checks and docs, and must stay in sync with that default.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- infrastructure ---
    database_url: str = "postgresql+psycopg://itsm:itsm@localhost:5432/itsm"
    redis_url: str = "redis://localhost:6379/0"

    # --- models (LiteLLM names; a bare name goes to OpenAI, "litellm/<provider>/<model>" elsewhere) ---
    triage_model: str = "gpt-5-mini"
    specialist_model: str = "gpt-5-mini"
    embedding_model: str = "text-embedding-3-small"  # must produce EMBED_DIM=1536 vectors

    # --- retrieval knobs (ADR-011 / ADR-016; tuned against `make eval`, not vibes) ---
    retrieval_top_k: int = 5
    rrf_k: int = 60
    # Refusal gate = best cosine similarity among retrieved chunks (NOT the rank-based RRF score).
    retrieval_refusal_threshold: float = 0.45

    # --- observability (M6) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- app ---
    app_env: str = "dev"
    log_level: str = "INFO"
    hitl_approval_threshold_usd: float = 500.0  # M2

    @field_validator(
        "triage_model",
        "specialist_model",
        "embedding_model",
        "hitl_approval_threshold_usd",
        "retrieval_refusal_threshold",
        mode="before",
    )
    @classmethod
    def _empty_env_means_default(cls, v: object, info) -> object:
        """`.env` ships `TRIAGE_MODEL=` (blank) placeholders; treat blank as 'use the default'."""
        if isinstance(v, str) and v.strip() == "":
            return cls.model_fields[info.field_name].default
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
