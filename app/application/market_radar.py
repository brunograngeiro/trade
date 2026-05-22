"""Daily paginated radar for liquid near-deadline Kalshi markets."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RadarConfig:
    pages: int
    page_limit: int
    top_n: int
    min_volume: int
    min_liquidity: int
    max_spread_cents: int
    max_ttc_days: float
    focus_ttc_days: float
    min_probability_cents: int
    max_probability_cents: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "RadarConfig":
        return cls(
            pages=settings.market_radar_pages,
            page_limit=settings.market_radar_page_limit,
            top_n=settings.market_radar_top_n,
            min_volume=settings.market_radar_min_volume,
            min_liquidity=settings.market_radar_min_liquidity,
            max_spread_cents=settings.market_radar_max_spread_cents,
            max_ttc_days=settings.market_radar_max_ttc_days,
            focus_ttc_days=settings.market_radar_focus_ttc_days,
            min_probability_cents=settings.market_radar_min_probability_cents,
            max_probability_cents=settings.market_radar_max_probability_cents,
        )


async def run_market_radar(settings: Settings, client: KalshiClient,
                           db: Database, config: RadarConfig | None = None) -> dict:
    cfg = config or RadarConfig.from_settings(settings)
    if not settings.market_radar_enabled:
        return {"enabled": False, "saved": 0}

    captured_at = datetime.now(timezone.utc)
    scan_id = captured_at.strftime("%Y%m%dT%H%M%SZ")
    cursor = None
    scanned = 0
    candidates: list[dict] = []

    for page in range(max(1, cfg.pages)):
        payload = await client.get_markets(
            status="open",
            limit=cfg.page_limit,
            cursor=cursor,
        )
        markets = payload.get("markets") or []
        scanned += len(markets)
        for market in markets:
            candidate = _candidate(market, cfg, captured_at)
            if candidate is not None:
                candidates.append(candidate)
        cursor = payload.get("cursor")
        if not cursor or not markets:
            break

    candidates.sort(key=lambda r: r["score"], reverse=True)
    selected = candidates[:cfg.top_n]
    for idx, row in enumerate(selected, start=1):
        row["scan_id"] = scan_id
        row["captured_at"] = captured_at.isoformat()
        row["rank"] = idx

    db.save_market_radar_candidates(selected)
    log.info("market_radar scanned=%d candidates=%d saved=%d scan_id=%s",
             scanned, len(candidates), len(selected), scan_id)
    return {
        "enabled": True,
        "scan_id": scan_id,
        "scanned": scanned,
        "candidates": len(candidates),
        "saved": len(selected),
    }


def _candidate(market: dict[str, Any], cfg: RadarConfig,
               now: datetime) -> dict | None:
    yes_bid = _to_float(market.get("yes_bid_dollars"))
    yes_ask = _to_float(market.get("yes_ask_dollars"))
    no_bid = _to_float(market.get("no_bid_dollars"))
    no_ask = _to_float(market.get("no_ask_dollars"))
    if None in {yes_bid, yes_ask, no_bid, no_ask}:
        return None

    yes_mid = (yes_bid + yes_ask) / 2.0
    yes_cents = yes_mid * 100
    if yes_cents < cfg.min_probability_cents or yes_cents > cfg.max_probability_cents:
        return None

    spread_cents = max(0.0, (yes_ask - yes_bid) * 100)
    if spread_cents > cfg.max_spread_cents:
        return None

    volume = _to_int(market.get("volume_fp") or market.get("volume")) or 0
    liquidity = _to_int(market.get("liquidity_dollars") or market.get("liquidity")) or 0
    if volume < cfg.min_volume and liquidity < cfg.min_liquidity:
        return None

    close_time = _parse_dt(market.get("close_time"))
    if close_time is None:
        return None
    ttc_seconds = (close_time - now).total_seconds()
    if ttc_seconds < 0 or ttc_seconds > cfg.max_ttc_days * 86400:
        return None

    score = _score(
        volume=volume,
        liquidity=liquidity,
        spread_cents=spread_cents,
        yes_mid=yes_mid,
        ttc_seconds=ttc_seconds,
        focus_ttc_days=cfg.focus_ttc_days,
    )
    return {
        "score": round(score, 4),
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "series_ticker": market.get("series_ticker"),
        "title": market.get("title"),
        "category": market.get("category"),
        "close_time": market.get("close_time"),
        "ttc_seconds": ttc_seconds,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": yes_mid,
        "spread_cents": spread_cents,
        "volume": volume,
        "liquidity": liquidity,
        "open_interest": _to_int(market.get("open_interest_fp") or market.get("open_interest")),
        "status": market.get("status"),
        "raw": json.dumps(market, separators=(",", ":")),
    }


def _score(volume: int, liquidity: int, spread_cents: float, yes_mid: float,
           ttc_seconds: float, focus_ttc_days: float) -> float:
    volume_score = math.log10(max(1, volume) + 1) * 20
    liquidity_score = math.log10(max(1, liquidity) + 1) * 10
    deadline_ratio = min(1.0, max(0.0, ttc_seconds / max(1.0, focus_ttc_days * 86400)))
    deadline_score = (1.0 - deadline_ratio) * 35
    probability_score = (1.0 - min(1.0, abs(yes_mid - 0.5) / 0.45)) * 20
    spread_penalty = spread_cents * 2.5
    return volume_score + liquidity_score + deadline_score + probability_score - spread_penalty


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
