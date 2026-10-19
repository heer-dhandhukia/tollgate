"""Runtime settings, read from the environment (and .env when present)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tollgate:tollgate@localhost:5432/tollgate"
    mockpay_url: str = "http://localhost:8100"
    mockpay_webhook_secret: str = "change-me-local-only"


@lru_cache
def get_settings() -> Settings:
    return Settings()
