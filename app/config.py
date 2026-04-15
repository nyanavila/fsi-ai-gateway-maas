from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Anthropic ─────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str
    MODEL_SMALL:  str = "claude-haiku-4-5-20251001"
    MODEL_MEDIUM: str = "claude-sonnet-4-6"
    MODEL_LARGE:  str = "claude-opus-4-6"

    # ── Red Hat MaaS (LiteLLM proxy) ─────────────────────────────────────────
    # If MAAS_API_KEY is not set, gateway runs Anthropic-only (backward compat)
    MAAS_API_KEY:           Optional[str] = None
    MAAS_ENDPOINT:          str = "https://litellm-prod.apps.maas.redhatworkshops.io/v1"
    MAAS_MODEL_CLASSIFIER:  str = "granite-3-2-8b-instruct"
    MAAS_MODEL_SIMPLE:      str = "granite-3-2-8b-instruct"
    MAAS_MODEL_COMPLEX:     str = "llama-scout-17b"
    MAAS_MODEL_REASONING:   str = "deepseek-r1-distill-qwen-14b"
    MAAS_MODEL_SAFETY:      str = "Llama-Guard-3-1B"

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://redis:6379/0"

    # ── Token budgets (daily, per department) ─────────────────────────────────
    BUDGET_CX_DAILY_TOKENS:      int = 5_000_000
    BUDGET_IT_DAILY_TOKENS:      int = 1_000_000
    BUDGET_FINANCE_DAILY_TOKENS: int = 500_000
    BUDGET_DEFAULT_DAILY_TOKENS: int = 500_000

    # ── CORS ──────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # ── App ───────────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    APP_ENV:   str = "production"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
