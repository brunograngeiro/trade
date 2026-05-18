"""Find the currently active BTC 15min market on Kalshi."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.entities import Market
from app.infrastructure.kalshi.client import KalshiClient
from app.infrastructure.kalshi.mapper import market_from_payload


async def list_active_markets(client: KalshiClient, series_ticker: str) -> list[Market]:
    payload = await client.get_markets(series_ticker=series_ticker, status="open", limit=100)
    if not payload.get("ok", True):
        return []
    return [market_from_payload(m) for m in payload.get("markets", [])]


async def current_market(client: KalshiClient, series_ticker: str,
                         now: datetime | None = None) -> Market | None:
    """Pick the open market whose [open_time, close_time] contains `now`."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    markets = await list_active_markets(client, series_ticker)
    for m in markets:
        if m.open_time <= moment <= m.close_time:
            return m
    # fallback: closest upcoming market
    upcoming = [m for m in markets if m.open_time >= moment]
    if upcoming:
        return min(upcoming, key=lambda m: m.open_time)
    return markets[0] if markets else None
