"""Dry-run scanner for non-15m Kalshi markets.

This intentionally records observations only. It does not emit orders.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.infrastructure.coinbase.client import CoinbaseClient
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


log = logging.getLogger(__name__)


@dataclass
class MarketScannerHealth:
    started_at: datetime | None = None
    last_iteration_at: datetime | None = None
    iterations: int = 0
    snapshots_saved: int = 0
    last_error: str | None = None
    last_series: str | None = None

    def snapshot(self) -> dict:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None
        return {
            "started_at": iso(self.started_at),
            "last_iteration_at": iso(self.last_iteration_at),
            "iterations": self.iterations,
            "snapshots_saved": self.snapshots_saved,
            "last_error": self.last_error,
            "last_series": self.last_series,
        }


class MarketScanner:
    def __init__(self, settings: Settings, client: KalshiClient, db: Database,
                 coinbase: CoinbaseClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.coinbase = coinbase
        self.health = MarketScannerHealth()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self.health.started_at = datetime.now(timezone.utc)
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("MarketScanner started series=%s poll=%.1fs",
                 self.settings.dry_run_market_series,
                 self.settings.dry_run_market_poll_seconds)
        while not self._stop.is_set():
            self.health.iterations += 1
            self.health.last_iteration_at = datetime.now(timezone.utc)
            try:
                saved = await self.scan_once()
                self.health.snapshots_saved += saved
                self.health.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.health.last_error = repr(exc)
                log.exception("MarketScanner iteration failed")

            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.dry_run_market_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass
        log.info("MarketScanner stopped")

    async def scan_once(self) -> int:
        now = datetime.now(timezone.utc)
        spot = None
        if self.coinbase is not None:
            spot_tick = await self.coinbase.get_ticker()
            spot = spot_tick.price if spot_tick else None

        saved = 0
        for series in _series_list(self.settings.dry_run_market_series):
            self.health.last_series = series
            markets = await self._markets_for_series(series)
            markets = _select_markets_near_spot(
                markets, spot,
                limit=self.settings.dry_run_market_max_snapshots_per_series,
            )
            for market in markets:
                snapshot = _snapshot_from_market(
                    market, series, now, spot,
                    max_ttc_hours=self.settings.dry_run_market_max_ttc_hours,
                )
                if snapshot is None:
                    continue
                self.db.save_market_snapshot(snapshot)
                saved += 1
        log.info("MarketScanner saved %d dry-run snapshots", saved)
        return saved

    async def _markets_for_series(self, series: str) -> list[dict[str, Any]]:
        payload = await self.client.get_markets(
            series_ticker=series,
            status="open",
            limit=self.settings.dry_run_market_limit_per_series,
        )
        if not payload.get("ok", True):
            log.warning("MarketScanner failed series=%s payload=%s", series, payload)
            return []
        return payload.get("markets") or []


def _series_list(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def _select_markets_near_spot(markets: list[dict[str, Any]], spot: float | None,
                              limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(markets) <= limit:
        return markets
    if spot is None:
        return markets[:limit]

    def score(market: dict[str, Any]) -> tuple[float, float]:
        strike = _strike_from_ticker(str(market.get("ticker") or ""))
        distance = abs(strike - spot) if strike is not None else float("inf")
        mid = _mid_price(market)
        price_distance = abs((mid if mid is not None else 0.5) - 0.5)
        return (distance, price_distance)

    return sorted(markets, key=score)[:limit]


def _strike_from_ticker(ticker: str) -> float | None:
    match = re.search(r"[-](?:B|T)(\d+(?:\.\d+)?)$", ticker)
    if not match:
        return None
    return _to_float(match.group(1))


def _mid_price(market: dict[str, Any]) -> float | None:
    bid = _to_float(market.get("yes_bid_dollars"))
    ask = _to_float(market.get("yes_ask_dollars"))
    if bid is None or ask is None:
        return _to_float(market.get("last_price_dollars"))
    return (bid + ask) / 2.0


def _snapshot_from_market(market: dict[str, Any], series: str, now: datetime,
                          spot_price: float | None,
                          max_ttc_hours: float) -> dict | None:
    close_time = _parse_dt(market.get("close_time"))
    if close_time is None:
        return None
    ttc = (close_time - now).total_seconds()
    if ttc < 0 or ttc > max_ttc_hours * 3600:
        return None

    return {
        "captured_at": now.isoformat(),
        "series_ticker": series,
        "event_ticker": market.get("event_ticker"),
        "ticker": market["ticker"],
        "title": market.get("title"),
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "yes_bid": _to_float(market.get("yes_bid_dollars")),
        "yes_ask": _to_float(market.get("yes_ask_dollars")),
        "no_bid": _to_float(market.get("no_bid_dollars")),
        "no_ask": _to_float(market.get("no_ask_dollars")),
        "last_price": _to_float(market.get("last_price_dollars")),
        "volume": _to_int(market.get("volume")),
        "liquidity": _to_int(market.get("liquidity")),
        "spot_price": spot_price,
        "ttc_seconds": ttc,
        "source": "dry_run_scanner",
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _to_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
