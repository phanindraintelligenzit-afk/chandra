"""Centralized runtime configuration loaded from environment variables.

All other modules should import the ``settings`` singleton from here.
Never read ``os.environ`` directly outside this module — that breaks the
single source of truth and bypasses validation.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration. Values are loaded from the process environment
    and (in development) from a ``.env`` file at the repo root."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    aws_profile: str | None = Field(default=None, alias="AWS_PROFILE")
    aws_default_region: str = Field(default="us-east-1", alias="AWS_DEFAULT_REGION")

    # ── LLM provider ────────────────────────────────────────────────────────
    # One of: bedrock, openai, ollama
    llm_provider: str = Field(default="bedrock", alias="LLM_PROVIDER")

    # Model name used by the active provider.
    # Bedrock:  anthropic.claude-sonnet-4-5-20250929-v1:0
    # OpenAI:   Qwen/Qwen2.5-32B-Coder-Instruct, gpt-4o, etc.
    # Ollama:   qwen2.5-coder:32b
    llm_model: str = Field(
        default="anthropic.claude-sonnet-4-5-20250929-v1:0",
        alias="LLM_MODEL",
    )

    # Sampling temperature (0.0 = deterministic, 0.7 = creative)
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE")

    # Max output tokens
    llm_max_tokens: int = Field(default=4096, alias="LLM_MAX_TOKENS")

    # ― Bedrock-specific ―
    bedrock_model_id: str = Field(
        default="anthropic.claude-sonnet-4-5-20250929-v1:0",
        alias="BEDROCK_MODEL_ID",
    )

    # ― OpenAI-compatible endpoint ―
    openai_api_base: str | None = Field(default=None, alias="OPENAI_API_BASE")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")

    # ― Ollama-specific ―
    ollama_base_url: str | None = Field(default=None, alias="OLLAMA_BASE_URL")

    # ── Postgres ─────────────────────────────────────────────────────────────
    postgres_url: str = Field(
        default="postgresql+psycopg://chandra:chandra@localhost:5432/chandra",
        alias="POSTGRES_URL",
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    otel_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_environment: str = Field(default="production", alias="OTEL_ENVIRONMENT")

    synthetic_account_id: str | None = Field(default=None, alias="SYNTHETIC_ACCOUNT_ID")

    # SNS topic ARN used by the escalation node. Seeded into state by
    # ``onboard_account`` so every entry point (CLI, FastAPI, harness)
    # publishes to the right topic without each call site re-injecting it.
    sns_topic_arn: str | None = Field(default=None, alias="SNS_TOPIC_ARN")

    boto_max_attempts: int = Field(default=10, alias="BOTO_MAX_ATTEMPTS")
    boto_retry_mode: str = Field(default="adaptive", alias="BOTO_RETRY_MODE")

    # Demo override: lower the stale-key threshold (in days) so synthetic
    # env runs surface SEC-003 without waiting 90 days for IAM CreateDate
    # to age. Set to 0 to flag any active key. Unset → use STALE_KEY_DAYS.
    stale_key_days_override: int | None = Field(
        default=None, alias="CHANDRA_STALE_KEY_DAYS_OVERRIDE"
    )

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid LOG_LEVEL: {v}")
        return v

    @field_validator("boto_retry_mode")
    @classmethod
    def _validate_retry_mode(cls, v: str) -> str:
        v = v.lower()
        if v not in {"legacy", "standard", "adaptive"}:
            raise ValueError(f"invalid BOTO_RETRY_MODE: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide ``Settings`` instance."""
    return Settings()


settings = get_settings()