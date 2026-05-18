"""Periodic Kalshi balance/positions snapshot service.

Used both inline (e.g. after a trade) and as a long-running loop that records
equity history for the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


log = logging.getLogger(__name__)


async def take_snapshot(client: KalshiClient, db: Database) -> dict:
    balance = await client.get_balance()
    positions = await client.get_positions(limit=100)
    orders = await client.get_orders(limit=100)

    balance_cents = balance.get("balance") or 0
    portfolio_value = balance.get("portfolio_value")
    open_positions = sum(
        1 for p in positions.get("market_positions", [])
        if abs(float(p.get("position_fp", 0))) > 0.001
    )
    resting = sum(1 for o in orders.get("orders", []) if o.get("status") == "resting")

    db.save_balance_snapshot(
        datetime.now(timezone.utc),
        balance_cents=int(balance_cents),
        portfolio_value_cents=int(portfolio_value) if portfolio_value is not None else None,
        open_positions_count=open_positions,
        resting_orders_count=resting,
        raw={"balance": balance, "positions_count": open_positions, "resting_count": resting},
    )
    log.info("snapshot balance=%dc positions=%d resting=%d",
             balance_cents, open_positions, resting)
    return {
        "balance_cents": balance_cents,
        "open_positions_count": open_positions,
        "resting_orders_count": resting,
    }


async def run_loop(settings: Settings, interval_seconds: int = 60) -> None:
    db = Database(settings.db_path)
    async with KalshiClient(settings) as client:
        while True:
            try:
                await take_snapshot(client, db)
            except Exception:  # noqa: BLE001
                log.exception("snapshot iteration failed")
            await asyncio.sleep(interval_seconds)
