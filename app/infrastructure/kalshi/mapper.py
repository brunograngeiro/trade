"""Map Kalshi API payloads into domain entities."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.entities import Market, Tick


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def market_from_payload(data: dict) -> Market:
    """Kalshi v2 market payload → Market entity. v2 returns `*_dollars` strings."""
    return Market(
        ticker=data["ticker"],
        title=data.get("title", ""),
        open_time=_parse_dt(data.get("open_time")),
        close_time=_parse_dt(data.get("close_time")),
        yes_bid=_to_float(data.get("yes_bid_dollars")),
        yes_ask=_to_float(data.get("yes_ask_dollars")),
        no_bid=_to_float(data.get("no_bid_dollars")),
        no_ask=_to_float(data.get("no_ask_dollars")),
        last_price=_to_float(data.get("last_price_dollars")),
        status=data.get("status", "unknown"),
    )


def tick_from_market(market: Market, *, captured_at: datetime | None = None,
                     volume: int | None = None) -> Tick:
    return Tick(
        ticker=market.ticker,
        captured_at=captured_at or datetime.now(timezone.utc),
        yes_bid=market.yes_bid,
        yes_ask=market.yes_ask,
        no_bid=market.no_bid,
        no_ask=market.no_ask,
        last_price=market.last_price,
        volume=volume,
    )
