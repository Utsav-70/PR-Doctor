"""Application configuration.

Single source of truth for every environment variable. Nothing outside this module
reads os.environ. Validation happens at import time, so a misconfiguration is a boot
failure rather than a surprise mid-review.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Imported rather than redeclared: a severity list that drifts from the one the model is
# prompted against would silently stop matching. `agent.schemas` is pure type
# declarations with no imports of its own, so this creates no cycle.
from agent.schemas import Severity

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Provider = Literal["anthropic", "gemini"]


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
    # LLM_MODEL is provider-specific: a Claude id when LLM_PROVIDER=anthropic, a Gemini
    # id when LLM_PROVIDER=gemini. Switching provider without switching model is the
    # obvious mistake, so _provider_has_key below refuses to boot on it.
    LLM_PROVIDER: Provider = "anthropic"
    ANTHROPIC_API_KEY: str | None = None
    GEMINI_API_KEY: str | None = None
    LLM_MODEL: str = "claude-opus-5"
    LLM_EFFORT: Effort = "high"
    LLM_MAX_TOKENS: int = 16000
    LLM_TIMEOUT_SECONDS: float = 600.0
    LLM_MAX_RETRIES: int = 2

    # --- Phase 5 budgets ------------------------------------------------------
    # Enforced as the calls happen, not tallied afterwards. A runaway agent that is
    # only noticed at the end has already spent the money.
    MAX_LLM_CALLS_PER_REVIEW: int = 25
    MAX_COST_PER_REVIEW_USD: float = 1.50
    MAX_TOOL_ITERATIONS: int = 12

    # Run the whole pipeline, record everything, post nothing. The only safe way to
    # evaluate a prompt or threshold change against live traffic.
    #
    # Default ON. Posting is the one irreversible, developer-visible action in this
    # system, and the phase doc is explicit: run every new installation in dry-run for
    # its first week and read what it would have said. Turning this off is a decision
    # someone should make on purpose.
    DRY_RUN: bool = True

    # --- Phase 6 multi-agent --------------------------------------------------
    # Hard filter applied after the planner. Narrow this to run a subset; the planner can
    # skip an enabled agent but can never run a disabled one.
    ENABLED_AGENTS: str = "bug,security,performance"
    # Off skips the routing call and runs every enabled agent. Worth turning off while
    # tuning the specialist prompts, so a skipped agent is never mistaken for a bad one.
    ENABLE_PLANNER: bool = True
    # One agent cannot flood the judge's input.
    MAX_FINDINGS_PER_AGENT: int = 20

    @property
    def enabled_agents(self) -> list[str]:
        return [a.strip() for a in self.ENABLED_AGENTS.split(",") if a.strip()]

    # --- Phase 7 verification -------------------------------------------------
    # Verification multiplies calls: N findings times two calls each, before the judge
    # has run. Without caps it is the most expensive stage in the pipeline.
    #
    # Findings below this severity are recorded but never posted, so verifying them buys
    # nothing.
    SKIP_VERIFY_BELOW_SEVERITY: Severity = "MEDIUM"
    MAX_VERIFICATION_CHECKS_PER_REVIEW: int = 24
    MAX_CHECKS_PER_BATCH: int = 4

    # --- Phases 8 and 9: what reaches a developer -----------------------------
    # Thresholds are the difference between a bot people read and one they mute.
    CONFIDENCE_THRESHOLD: float = 0.85
    # Unverified findings clear a higher bar. A finding nothing could corroborate is not
    # worth the trust it risks unless the reviewer was very sure and the stakes are high.
    UNVERIFIED_CONFIDENCE_MARGIN: float = 0.10
    MIN_SEVERITY_TO_POST: Severity = "MEDIUM"
    # Generous: a good review posts two to five comments. Reviews that routinely hit this
    # mean the threshold is too low — a signal to act on, not a volume to accept.
    MAX_COMMENTS_PER_REVIEW: int = 15

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

    @model_validator(mode="after")
    def _provider_has_key(self) -> Settings:
        """Fail at boot rather than at the end of an expensive pipeline.

        The Anthropic SDK can fall back to a local auth profile, so an absent key is
        survivable there. google-genai has no such fallback.
        """
        if self.LLM_PROVIDER == "gemini":
            if not self.GEMINI_API_KEY:
                raise ValueError("LLM_PROVIDER=gemini requires GEMINI_API_KEY")
            if self.LLM_MODEL.startswith("claude"):
                raise ValueError(f"LLM_PROVIDER=gemini but LLM_MODEL={self.LLM_MODEL!r}")
        if self.LLM_PROVIDER == "anthropic" and self.LLM_MODEL.startswith("gemini"):
            raise ValueError(f"LLM_PROVIDER=anthropic but LLM_MODEL={self.LLM_MODEL!r}")
        return self

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
