"""Match successful real orders against market resolutions to compute realized PnL.

Strategy:
  - For each row in `orders` where ok=1 and dry_run=0
  - If a row exists in `market_resolutions` for ticker → settle
  - PnL = (1.0 if won else 0.0) - entry_price - fees
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timezone

from app.config import Settings


log = logging.getLogger(__name__)


def kalshi_fee_dollars(price_dollars: float, count: int, rate: float = 0.07) -> float:
    p = max(0.01, min(0.99, price_dollars))
    raw = rate * count * p * (1 - p)
    return math.ceil(raw * 100) / 100.0


def reconcile_outcomes(db_path: str, fee_rate: float = 0.07) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT o.id, o.ticker, o.side, o.limit_price_cents, o.count,
                  r.result AS resolution
           FROM orders o
           LEFT JOIN market_resolutions r ON r.ticker = o.ticker
           WHERE o.dry_run = 0 AND o.ok = 1"""
    ).fetchall()

    updated = 0
    for r in rows:
        entry_price = r["limit_price_cents"] / 100.0
        fees = kalshi_fee_dollars(entry_price, r["count"], fee_rate)
        resolution = r["resolution"]
        pnl = None
        if resolution in {"yes", "no"}:
            won = (r["side"] == resolution)
            payoff = 1.0 if won else 0.0
            pnl = round((payoff - entry_price) * r["count"] - fees, 4)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO trade_outcomes
               (order_id, ticker, side, entry_price_cents, count,
                resolution, realized_pnl_dollars, fees_paid_dollars, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["id"], r["ticker"], r["side"], r["limit_price_cents"], r["count"],
             resolution, pnl, round(fees, 4), now),
        )
        updated += 1
    conn.commit()
    conn.close()
    log.info("reconcile_outcomes processed %d real orders", updated)
    return {"processed": updated}
