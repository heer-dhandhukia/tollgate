"""Runtime settings, read from the environment (and .env when present)."""

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tollgate:tollgate@localhost:5432/tollgate"
    mockpay_url: str = "http://localhost:8100"
    mockpay_webhook_secret: str = "change-me-local-only"
    provider_timeout_s: float = 5.0

    # LLM
    tollgate_agent_model: str = "groq/llama-3.3-70b-versatile"
    llm_concurrency: int = 2
    llm_max_retries: int = 6
    llm_backoff_cap_s: float = 60.0
    llm_cache_dir: Path = REPO_ROOT / "cache" / "llm"
    models_config: Path = REPO_ROOT / "config" / "models.yaml"

    # Policy
    policy_file: Path = REPO_ROOT / "config" / "policy.v1.yaml"

    # Eval
    eval_pg_admin_url: str = "postgresql+asyncpg://tollgate:tollgate@localhost:5432/postgres"
    eval_today: date = date(2026, 10, 1)
    results_dir: Path = REPO_ROOT / "results"
    scenarios_dir: Path = REPO_ROOT / "scenarios"


@lru_cache
def get_settings() -> Settings:
    return Settings()
