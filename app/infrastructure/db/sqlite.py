"""Lightweight SQLite persistence with auto-migrating schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.domain.entities import OrderResult, Signal, Tick


SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ticks_ticker_time ON ticks(ticker, captured_at);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    side TEXT NOT NULL,
    phase TEXT NOT NULL,
    probability REAL NOT NULL,
    delta REAL NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker_time ON signals(ticker, captured_at);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    count INTEGER NOT NULL,
    limit_price_cents INTEGER NOT NULL,
    client_order_id TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS market_resolutions (
    ticker TEXT PRIMARY KEY,
    resolved_at TEXT NOT NULL,
    result TEXT NOT NULL,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS spot_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    price REAL NOT NULL,
    bid REAL,
    ask REAL,
    volume_24h REAL
);
CREATE INDEX IF NOT EXISTS idx_spot_product_time ON spot_ticks(product, captured_at);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    portfolio_value_cents INTEGER,
    open_positions_count INTEGER,
    resting_orders_count INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_balance_time ON balance_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    order_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price_cents INTEGER NOT NULL,
    count INTEGER NOT NULL,
    resolution TEXT,
    realized_pnl_dollars REAL,
    fees_paid_dollars REAL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---------- Ticks ----------

    def save_tick(self, tick: Tick) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO ticks (ticker, captured_at, yes_bid, yes_ask, no_bid,
                   no_ask, last_price, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (tick.ticker, tick.captured_at.isoformat(), tick.yes_bid, tick.yes_ask,
                 tick.no_bid, tick.no_ask, tick.last_price, tick.volume),
            )

    def ticks_for(self, ticker: str, limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM ticks WHERE ticker = ? ORDER BY captured_at DESC LIMIT ?""",
                (ticker, limit),
            ).fetchall()
        return [dict(r) for r in rows][::-1]

    def all_ticks(self, ticker: str | None = None) -> Iterable[dict]:
        with self.connect() as conn:
            if ticker:
                rows = conn.execute(
                    "SELECT * FROM ticks WHERE ticker = ? ORDER BY captured_at ASC",
                    (ticker,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ticks ORDER BY ticker ASC, captured_at ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Signals ----------

    def save_signal(self, signal: Signal) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO signals (ticker, captured_at, kind, side, phase,
                   probability, delta, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal.ticker, signal.captured_at.isoformat(), signal.kind.value,
                 signal.side.value, signal.phase.value, signal.probability,
                 signal.delta, signal.notes),
            )

    def recent_signals(self, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY captured_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Orders ----------

    def save_order(self, result: OrderResult) -> None:
        req = result.request
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO orders (submitted_at, ticker, side, action, count,
                   limit_price_cents, client_order_id, dry_run, ok, error, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result.submitted_at.isoformat(), req.ticker, req.side.value, req.action,
                 req.count, req.limit_price_cents, req.client_order_id,
                 1 if req.dry_run else 0, 1 if result.ok else 0,
                 result.error, json.dumps(result.raw)),
            )

    def recent_orders(self, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY submitted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Resolutions ----------

    def save_resolution(self, ticker: str, result: str, raw: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO market_resolutions (ticker, resolved_at, result, raw)
                   VALUES (?, ?, ?, ?)""",
                (ticker, datetime.now(timezone.utc).isoformat(), result,
                 json.dumps(raw) if raw else None),
            )

    def resolution_for(self, ticker: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_resolutions WHERE ticker = ?", (ticker,),
            ).fetchone()
        return dict(row) if row else None

    # ---------- Spot ticks ----------

    def save_spot_tick(self, product: str, captured_at: datetime, price: float,
                       bid: float | None, ask: float | None,
                       volume_24h: float | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO spot_ticks (product, captured_at, price, bid, ask, volume_24h)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (product, captured_at.isoformat(), price, bid, ask, volume_24h),
            )

    def recent_spot_ticks(self, product: str = "BTC-USD", limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM spot_ticks WHERE product = ?
                   ORDER BY captured_at DESC LIMIT ?""",
                (product, limit),
            ).fetchall()
        return [dict(r) for r in rows][::-1]

    # ---------- Balance snapshots ----------

    def save_balance_snapshot(self, captured_at: datetime, balance_cents: int,
                              portfolio_value_cents: int | None,
                              open_positions_count: int | None,
                              resting_orders_count: int | None,
                              raw: dict | None = None) -> None:
        import json as _json
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO balance_snapshots (captured_at, balance_cents,
                   portfolio_value_cents, open_positions_count, resting_orders_count, raw)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (captured_at.isoformat(), balance_cents, portfolio_value_cents,
                 open_positions_count, resting_orders_count,
                 _json.dumps(raw) if raw else None),
            )

    def balance_history(self, limit: int = 5000) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM balance_snapshots ORDER BY captured_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Trade outcomes ----------

    def upsert_trade_outcome(self, order_id: int, ticker: str, side: str,
                             entry_price_cents: int, count: int,
                             resolution: str | None,
                             realized_pnl_dollars: float | None,
                             fees_paid_dollars: float | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO trade_outcomes
                   (order_id, ticker, side, entry_price_cents, count,
                    resolution, realized_pnl_dollars, fees_paid_dollars, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, ticker, side, entry_price_cents, count, resolution,
                 realized_pnl_dollars, fees_paid_dollars,
                 datetime.now(timezone.utc).isoformat()),
            )

    def trade_outcomes(self, limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT o.id, o.submitted_at, o.ticker, o.side, o.count,
                          o.limit_price_cents, o.dry_run, o.ok,
                          t.resolution, t.realized_pnl_dollars, t.fees_paid_dollars
                   FROM orders o
                   LEFT JOIN trade_outcomes t ON t.order_id = o.id
                   WHERE o.dry_run = 0 AND o.ok = 1
                   ORDER BY o.submitted_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
