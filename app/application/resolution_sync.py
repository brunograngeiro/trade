"""Periodically syncs market_resolutions for closed BTC 15min markets.

For every market we have ticks for but no resolution, query Kalshi /markets/{ticker}.
If status in {settled, finalized, determined} and a result is present, store it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from app.config import Settings
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


log = logging.getLogger(__name__)


SETTLED_STATUSES = {"settled", "finalized", "determined"}


def _tickers_needing_resolution(db_path: str, limit: int = 100) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT t.ticker
            FROM ticks t
            LEFT JOIN market_resolutions r ON r.ticker = t.ticker
            WHERE r.ticker IS NULL
            ORDER BY t.ticker DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r["ticker"] for r in rows]


def _extract_result(market: dict) -> str | None:
    """Return 'yes' | 'no' | None depending on official result."""
    result = market.get("result")
    if isinstance(result, str):
        r = result.strip().lower()
        if r == "yes":
            return "yes"
        if r == "no":
            return "no"
    # some versions expose `expiration_value` / `settlement_value`
    val = market.get("settlement_value") or market.get("expiration_value")
    if isinstance(val, str) and val.lower() in {"yes", "no"}:
        return val.lower()
    return None


async def sync_once(settings: Settings, client: KalshiClient, db: Database,
                    tickers: Iterable[str] | None = None) -> dict:
    if tickers is None:
        tickers = _tickers_needing_resolution(settings.db_path)
    tickers = list(tickers)
    if not tickers:
        return {"checked": 0, "resolved": 0}

    resolved = 0
    for ticker in tickers:
        payload = await client.get_market(ticker)
        market = payload.get("market")
        if not market:
            continue
        status = (market.get("status") or "").lower()
        if status not in SETTLED_STATUSES:
            continue
        outcome = _extract_result(market)
        if not outcome:
            continue
        db.save_resolution(ticker, outcome, market)
        resolved += 1
        log.info("Resolved %s → %s (status=%s)", ticker, outcome, status)
    return {"checked": len(tickers), "resolved": resolved}


async def run_loop(settings: Settings, interval_seconds: int = 300) -> None:
    from app.application.outcome_tracker import reconcile_outcomes
    from app.application.portfolio_snapshot import take_snapshot

    db = Database(settings.db_path)
    snapshot_every_n = 1  # piggy-back: take snapshot on each resolver tick (5 min)
    iteration = 0
    async with KalshiClient(settings) as client:
        while True:
            iteration += 1
            try:
                result = await sync_once(settings, client, db)
                log.info("resolution_sync %s", result)
            except Exception:  # noqa: BLE001
                log.exception("resolution_sync iteration failed")

            try:
                reconcile_outcomes(settings.db_path)
            except Exception:  # noqa: BLE001
                log.exception("outcome reconcile failed")

            if iteration % snapshot_every_n == 0:
                try:
                    await take_snapshot(client, db)
                except Exception:  # noqa: BLE001
                    log.exception("portfolio snapshot failed")

            await asyncio.sleep(interval_seconds)
