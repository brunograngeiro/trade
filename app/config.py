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
    strategy_real_order_cap: int = Field(default=0, alias="STRATEGY_REAL_ORDER_CAP")
    strategy_real_order_cap_since: str = Field(default="", alias="STRATEGY_REAL_ORDER_CAP_SINCE")
    risk_manager_enabled: bool = Field(default=True, alias="RISK_MANAGER_ENABLED")
    risk_max_balance_fraction: float = Field(default=0.50, alias="RISK_MAX_BALANCE_FRACTION")
    risk_max_daily_trades: int = Field(default=10, alias="RISK_MAX_DAILY_TRADES")
    risk_max_daily_loss_dollars: float = Field(default=6.0, alias="RISK_MAX_DAILY_LOSS_DOLLARS")
    risk_max_consecutive_losses: int = Field(default=3, alias="RISK_MAX_CONSECUTIVE_LOSSES")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    xai_api_key: str = Field(default="", alias="XAI_API_KEY")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    prob_explosion_delta: float = Field(default=0.20, alias="PROB_EXPLOSION_DELTA")
    prob_plateau_threshold: float = Field(default=0.99, alias="PROB_PLATEAU_THRESHOLD")
    prob_plateau_seconds: int = Field(default=99999, alias="PROB_PLATEAU_SECONDS")
    extreme_close_prob: float = Field(default=0.90, alias="EXTREME_CLOSE_PROB")
    extreme_close_ttc_seconds: float = Field(default=180.0, alias="EXTREME_CLOSE_TTC_SECONDS")
    extreme_close_persistence_seconds: float = Field(default=30.0, alias="EXTREME_CLOSE_PERSISTENCE_SECONDS")
    strategy_decisions_enabled: bool = Field(default=True, alias="STRATEGY_DECISIONS_ENABLED")
    entry_confidence_floor: float = Field(default=0.60, alias="ENTRY_CONFIDENCE_FLOOR")
    entry_persistence_seconds: float = Field(default=20.0, alias="ENTRY_PERSISTENCE_SECONDS")
    entry_ttc_seconds: float = Field(default=180.0, alias="ENTRY_TTC_SECONDS")
    late_cross_ttc_seconds: float = Field(default=180.0, alias="LATE_CROSS_TTC_SECONDS")
    late_cross_confirmation_seconds: float = Field(default=10.0, alias="LATE_CROSS_CONFIRMATION_SECONDS")
    late_cross_strong_confidence: float = Field(default=0.70, alias="LATE_CROSS_STRONG_CONFIDENCE")
    late_cross_final_ttc_seconds: float = Field(default=60.0, alias="LATE_CROSS_FINAL_TTC_SECONDS")
    late_cross_final_confidence: float = Field(default=0.85, alias="LATE_CROSS_FINAL_CONFIDENCE")
    flip_cross_ttc_seconds: float = Field(default=180.0, alias="FLIP_CROSS_TTC_SECONDS")
    flip_enabled: bool = Field(default=False, alias="FLIP_ENABLED")
    exit_confirmation_seconds: float = Field(default=10.0, alias="EXIT_CONFIRMATION_SECONDS")
    min_exit_ttc_seconds: float = Field(default=5.0, alias="MIN_EXIT_TTC_SECONDS")
    max_entry_price_180s_cents: int = Field(default=72, alias="MAX_ENTRY_PRICE_180S_CENTS")
    max_entry_price_60s_cents: int = Field(default=92, alias="MAX_ENTRY_PRICE_60S_CENTS")
    max_entry_price_30s_cents: int = Field(default=97, alias="MAX_ENTRY_PRICE_30S_CENTS")
    strategy_exit_slippage_cents: int = Field(default=3, alias="STRATEGY_EXIT_SLIPPAGE_CENTS")
    spot_guard_enabled: bool = Field(default=True, alias="SPOT_GUARD_ENABLED")
    spot_guard_buffer_dollars: float = Field(default=5.0, alias="SPOT_GUARD_BUFFER_DOLLARS")
    spot_guard_momentum_seconds: float = Field(default=60.0, alias="SPOT_GUARD_MOMENTUM_SECONDS")
    spot_guard_momentum_dollars: float = Field(default=15.0, alias="SPOT_GUARD_MOMENTUM_DOLLARS")
    collector_poll_seconds: float = Field(default=2.0, alias="COLLECTOR_POLL_SECONDS")
    strategy_enter_only_in_phase: str = Field(default="late", alias="STRATEGY_ENTER_ONLY_IN_PHASE")
    dry_run_market_scanner_enabled: bool = Field(default=True, alias="DRY_RUN_MARKET_SCANNER_ENABLED")
    dry_run_market_series: str = Field(default="KXBTCD,KXBTC", alias="DRY_RUN_MARKET_SERIES")
    dry_run_market_poll_seconds: float = Field(default=60.0, alias="DRY_RUN_MARKET_POLL_SECONDS")
    dry_run_market_limit_per_series: int = Field(default=200, alias="DRY_RUN_MARKET_LIMIT_PER_SERIES")
    dry_run_market_max_snapshots_per_series: int = Field(default=24, alias="DRY_RUN_MARKET_MAX_SNAPSHOTS_PER_SERIES")
    dry_run_market_max_ttc_hours: float = Field(default=30.0, alias="DRY_RUN_MARKET_MAX_TTC_HOURS")
    market_radar_enabled: bool = Field(default=True, alias="MARKET_RADAR_ENABLED")
    market_radar_pages: int = Field(default=50, alias="MARKET_RADAR_PAGES")
    market_radar_page_limit: int = Field(default=1000, alias="MARKET_RADAR_PAGE_LIMIT")
    market_radar_top_n: int = Field(default=150, alias="MARKET_RADAR_TOP_N")
    market_radar_min_volume: int = Field(default=100, alias="MARKET_RADAR_MIN_VOLUME")
    market_radar_min_liquidity: int = Field(default=100, alias="MARKET_RADAR_MIN_LIQUIDITY")
    market_radar_max_spread_cents: int = Field(default=15, alias="MARKET_RADAR_MAX_SPREAD_CENTS")
    market_radar_max_ttc_days: float = Field(default=7.0, alias="MARKET_RADAR_MAX_TTC_DAYS")
    market_radar_focus_ttc_days: float = Field(default=1.0, alias="MARKET_RADAR_FOCUS_TTC_DAYS")
    market_radar_min_probability_cents: int = Field(default=5, alias="MARKET_RADAR_MIN_PROBABILITY_CENTS")
    market_radar_max_probability_cents: int = Field(default=95, alias="MARKET_RADAR_MAX_PROBABILITY_CENTS")
    collector_http_timeout_seconds: float = Field(default=10.0, alias="COLLECTOR_HTTP_TIMEOUT_SECONDS")
    collector_max_consecutive_failures: int = Field(default=10, alias="COLLECTOR_MAX_CONSECUTIVE_FAILURES")
    collector_backoff_base_seconds: float = Field(default=1.0, alias="COLLECTOR_BACKOFF_BASE_SECONDS")
    collector_backoff_max_seconds: float = Field(default=60.0, alias="COLLECTOR_BACKOFF_MAX_SECONDS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
