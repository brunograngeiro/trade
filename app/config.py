"""Central configuration loaded from environment / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kalshi_api_key_id: str = Field(default="", alias="KALSHI_API_KEY_ID")
    kalshi_private_key_path: str = Field(
        default=str(ROOT / "secrets" / "kalshi_api.keys"),
        alias="KALSHI_PRIVATE_KEY_PATH",
    )
    kalshi_base_url: str = Field(
        default="https://api.elections.kalshi.com/trade-api/v2",
        alias="KALSHI_BASE_URL",
    )
    kalshi_ws_url: str = Field(
        default="wss://api.elections.kalshi.com/trade-api/ws/v2",
        alias="KALSHI_WS_URL",
    )
    kalshi_series_ticker: str = Field(default="KXBTC15M", alias="KALSHI_SERIES_TICKER")

    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8020, alias="APP_PORT")
    dashboard_port: int = Field(default=8501, alias="DASHBOARD_PORT")
    db_path: str = Field(default=str(ROOT / "data" / "trade2.sqlite3"), alias="DB_PATH")

    enable_real_orders: bool = Field(default=False, alias="ENABLE_REAL_ORDERS")
    max_order_cost_cents: int = Field(default=90, alias="MAX_ORDER_COST_CENTS")
    default_order_side: str = Field(default="yes", alias="DEFAULT_ORDER_SIDE")
    default_order_count: int = Field(default=1, alias="DEFAULT_ORDER_COUNT")

    prob_explosion_delta: float = Field(default=0.20, alias="PROB_EXPLOSION_DELTA")
    prob_plateau_threshold: float = Field(default=0.99, alias="PROB_PLATEAU_THRESHOLD")
    prob_plateau_seconds: int = Field(default=99999, alias="PROB_PLATEAU_SECONDS")
    collector_poll_seconds: float = Field(default=2.0, alias="COLLECTOR_POLL_SECONDS")
    strategy_enter_only_in_phase: str = Field(default="late", alias="STRATEGY_ENTER_ONLY_IN_PHASE")
    collector_http_timeout_seconds: float = Field(default=10.0, alias="COLLECTOR_HTTP_TIMEOUT_SECONDS")
    collector_max_consecutive_failures: int = Field(default=10, alias="COLLECTOR_MAX_CONSECUTIVE_FAILURES")
    collector_backoff_base_seconds: float = Field(default=1.0, alias="COLLECTOR_BACKOFF_BASE_SECONDS")
    collector_backoff_max_seconds: float = Field(default=60.0, alias="COLLECTOR_BACKOFF_MAX_SECONDS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
