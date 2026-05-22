"""Match successful real orders against fills/resolutions to compute realized PnL.

Strategy:
  - Prefer Kalshi fills when available: actual fill price, actual fee, and
    sell exits matched FIFO against earlier buys.
  - Fallback to local buy orders + market resolution when fills are absent.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.config import Settings


log = logging.getLogger(__name__)


def kalshi_fee_dollars(price_dollars: float, count: int, rate: float = 0.07) -> float:
    p = max(0.01, min(0.99, price_dollars))
    raw = rate * count * p * (1 - p)
    return math.ceil(raw * 100) / 100.0


def reconcile_outcomes(db_path: str, fee_rate: float = 0.07,
                       fills: list[dict[str, Any]] | None = None) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """DELETE FROM trade_outcomes
           WHERE order_id IN (
               SELECT id FROM orders
               WHERE dry_run != 0 OR ok != 1 OR action != 'buy'
           )"""
    )
    rows = conn.execute(
        """SELECT o.id, o.ticker, o.side, o.action, o.limit_price_cents, o.count, o.raw,
                  r.result AS resolution
           FROM orders o
           LEFT JOIN market_resolutions r ON r.ticker = o.ticker
           WHERE o.dry_run = 0 AND o.ok = 1"""
    ).fetchall()

    if fills:
        updated = _reconcile_from_fills(conn, rows, fills, fee_rate)
        conn.commit()
        conn.close()
        log.info("reconcile_outcomes processed %d filled real buy orders", updated)
        return {"processed": updated, "source": "fills"}

    buy_rows = [r for r in rows if r["action"] == "buy"]
    updated = 0
    for r in buy_rows:
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
    log.info("reconcile_outcomes processed %d real buy orders", updated)
    return {"processed": updated, "source": "orders"}


def _reconcile_from_fills(conn: sqlite3.Connection, rows: list[sqlite3.Row],
                          fills: list[dict[str, Any]], fee_rate: float) -> int:
    orders_by_external_id: dict[str, sqlite3.Row] = {}
    for row in rows:
        external_id = _external_order_id(row["raw"])
        if external_id:
            orders_by_external_id[external_id] = row

    stats: dict[int, dict[str, Any]] = {}
    open_lots: dict[tuple[str, str], list[dict[str, float]]] = {}

    for fill in sorted(fills, key=_fill_sort_key):
        order = orders_by_external_id.get(str(fill.get("order_id") or ""))
        if order is None:
            continue

        count = _as_float(fill.get("count_fp") or fill.get("count"), 0.0)
        if count <= 0:
            continue

        side = str(order["side"]).lower()
        price = _fill_price(fill, side)
        if price is None:
            price = order["limit_price_cents"] / 100.0
        fee = _as_float(fill.get("fee_cost"), kalshi_fee_dollars(price, int(math.ceil(count)), fee_rate))
        key = (order["ticker"], side)

        if order["action"] == "buy":
            stat = stats.setdefault(order["id"], {
                "ticker": order["ticker"],
                "side": side,
                "entry_total": 0.0,
                "count": 0.0,
                "fees": 0.0,
                "realized": 0.0,
                "remaining": 0.0,
                "remaining_fee": 0.0,
                "resolution": order["resolution"],
            })
            stat["entry_total"] += price * count
            stat["count"] += count
            stat["fees"] += fee
            stat["remaining"] += count
            stat["remaining_fee"] += fee
            open_lots.setdefault(key, []).append({
                "order_id": float(order["id"]),
                "price": price,
                "remaining": count,
                "remaining_fee": fee,
            })
            continue

        if order["action"] != "sell":
            continue

        remaining_sell = count
        sell_fee_remaining = fee
        lots = open_lots.get(key, [])
        while remaining_sell > 0 and lots:
            lot = lots[0]
            matched = min(remaining_sell, lot["remaining"])
            sell_fee_part = sell_fee_remaining * (matched / remaining_sell) if remaining_sell else 0.0
            buy_fee_part = lot["remaining_fee"] * (matched / lot["remaining"]) if lot["remaining"] else 0.0
            buy_order_id = int(lot["order_id"])
            stat = stats.get(buy_order_id)
            if stat is not None:
                stat["realized"] += (price - lot["price"]) * matched - sell_fee_part - buy_fee_part
                stat["remaining"] -= matched
                stat["remaining_fee"] -= buy_fee_part

            lot["remaining"] -= matched
            lot["remaining_fee"] -= buy_fee_part
            remaining_sell -= matched
            sell_fee_remaining -= sell_fee_part
            if lot["remaining"] <= 1e-9:
                lots.pop(0)

    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for order_id, stat in stats.items():
        if stat["count"] <= 0:
            continue

        avg_entry = stat["entry_total"] / stat["count"]
        pnl = stat["realized"]
        resolution = stat["resolution"]
        if stat["remaining"] > 1e-9:
            if resolution not in {"yes", "no"}:
                realized_pnl = None if abs(pnl) <= 1e-9 else round(pnl, 4)
            else:
                payoff = 1.0 if stat["side"] == resolution else 0.0
                pnl += (payoff - avg_entry) * stat["remaining"] - stat["remaining_fee"]
                realized_pnl = round(pnl, 4)
        else:
            realized_pnl = round(pnl, 4)

        conn.execute(
            """INSERT OR REPLACE INTO trade_outcomes
               (order_id, ticker, side, entry_price_cents, count,
                resolution, realized_pnl_dollars, fees_paid_dollars, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, stat["ticker"], stat["side"], int(round(avg_entry * 100)),
             int(round(stat["count"])), resolution, realized_pnl,
             round(stat["fees"], 4), now),
        )
        updated += 1
    return updated


def _external_order_id(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    order = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(order, dict):
        return None
    value = order.get("order_id")
    return str(value) if value else None


def _fill_sort_key(fill: dict[str, Any]) -> tuple[int, str]:
    return (int(_as_float(fill.get("ts"), 0.0)), str(fill.get("created_time") or ""))


def _fill_price(fill: dict[str, Any], side: str) -> float | None:
    key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    value = _as_float(fill.get(key), -1.0)
    return value if value >= 0 else None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
