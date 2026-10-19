from pydantic_settings import BaseSettings, SettingsConfigDict


class MockpaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCKPAY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tollgate:tollgate@localhost:5432/mockpay"
    webhook_url: str = ""
    webhook_secret: str = "change-me-local-only"
    webhook_max_attempts: int = 3

    # A refund settles (pending -> processed/failed) this long after creation.
    settle_delay_ms: int = 200
    # How often the background sweeper settles due refunds. 0 disables it (tests settle manually).
    settle_poll_ms: int = 50

    # Random faults, drawn from a seeded RNG so a run is reproducible.
    fault_seed: int = 42
    p_latency: float = 0.0
    latency_ms: int = 0
    p_error_before_commit: float = 0.0
    p_error_after_commit: float = 0.0
    p_timeout_after_commit: float = 0.0
    timeout_hang_ms: int = 5000
    p_duplicate_webhook: float = 0.0
    duplicate_webhook_copies: int = 2
    p_out_of_order_webhooks: float = 0.0
    p_refund_fails: float = 0.0
