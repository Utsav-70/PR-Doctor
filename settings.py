"""Application configuration.

Single source of truth for every environment variable. Nothing outside this module
reads os.environ. Validation happens at import time, so a misconfiguration is a boot
failure rather than a surprise mid-review.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- core ---------------------------------------------------------------
    PRGUARD_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # --- GitHub App ---------------------------------------------------------
    GITHUB_APP_ID: str
    GITHUB_APP_PRIVATE_KEY: str
    GITHUB_WEBHOOK_SECRET: str
    GITHUB_API_URL: str = "https://api.github.com"
    GITHUB_TIMEOUT_SECONDS: float = 20.0

    # --- datastores ---------------------------------------------------------
    DATABASE_URL: str = "postgresql+psycopg://prguard:prguard@postgres:5432/prguard"
    REDIS_URL: str = "redis://redis:6379/0"
    REDIS_RESULT_URL: str = "redis://redis:6379/1"

    # --- Celery -------------------------------------------------------------
    CELERY_TASK_SOFT_TIME_LIMIT: int = 900
    CELERY_TASK_TIME_LIMIT: int = 1200

    # --- LLM ----------------------------------------------------------------
    ANTHROPIC_API_KEY: str | None = None
    LLM_MODEL: str = "claude-opus-5"
    LLM_EFFORT: Effort = "high"
    LLM_MAX_TOKENS: int = 16000
    LLM_TIMEOUT_SECONDS: float = 600.0
    LLM_MAX_RETRIES: int = 2

    # --- budgets ------------------------------------------------------------
    MAX_CHANGED_LINES: int = 3000
    MAX_CHANGED_FILES: int = 50
    MAX_FILE_BYTES: int = 200_000
    MAX_CONTEXT_CHARS: int = 240_000
    # Cap on the diff persisted to reviews.raw_diff. Kept well above the review budget
    # so a normal PR is stored whole, and low enough that a pathological diff cannot
    # bloat the table.
    MAX_DIFF_STORE_BYTES: int = 1_000_000

    # --- review behaviour ---------------------------------------------------
    SKIP_DRAFT_PRS: bool = True
    SKIP_BOT_AUTHORS: bool = True
    IGNORE_PATHS: str = (
        "**/migrations/**,**/vendor/**,**/node_modules/**,*.lock,*.min.js,*.min.css,*.map,*.svg"
    )

    @property
    def ignore_globs(self) -> list[str]:
        return [p.strip() for p in self.IGNORE_PATHS.split(",") if p.strip()]

    @property
    def github_private_key(self) -> str:
        """PEM with escaped newlines restored.

        Environment variables cannot hold literal newlines, so the key is stored with
        `\\n` escapes and unescaped here.
        """
        return self.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")

    @field_validator("GITHUB_APP_PRIVATE_KEY")
    @classmethod
    def _looks_like_pem(cls, v: str) -> str:
        if "PRIVATE KEY" not in v:
            raise ValueError("GITHUB_APP_PRIVATE_KEY does not look like a PEM key")
        return v

    @field_validator("LLM_MAX_TOKENS")
    @classmethod
    def _streaming_ceiling(cls, v: int) -> int:
        # Above ~16k a non-streaming request risks an SDK HTTP timeout. This slice
        # does not stream, so refuse to boot with a budget that cannot complete.
        if v > 16000:
            raise ValueError("LLM_MAX_TOKENS > 16000 requires streaming; not supported yet")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
