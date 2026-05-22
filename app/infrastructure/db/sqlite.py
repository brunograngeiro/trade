"""Lightweight SQLite persistence with auto-migrating schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.application.decision import StrategyDecision
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

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    probability REAL,
    confidence REAL,
    ttc_seconds REAL,
    limit_price_cents INTEGER,
    reason TEXT NOT NULL,
    signal_kind TEXT NOT NULL,
    crossed_50 INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_time
    ON strategy_decisions(captured_at);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_ticker_time
    ON strategy_decisions(ticker, captured_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    event_ticker TEXT,
    ticker TEXT NOT NULL,
    title TEXT,
    open_time TEXT,
    close_time TEXT,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume INTEGER,
    liquidity INTEGER,
    spot_price REAL,
    ttc_seconds REAL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_series_time
    ON market_snapshots(series_ticker, captured_at);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_ticker_time
    ON market_snapshots(ticker, captured_at);

CREATE TABLE IF NOT EXISTS market_radar_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    series_ticker TEXT,
    title TEXT,
    category TEXT,
    event_title TEXT,
    event_sub_title TEXT,
    market_title TEXT,
    slug TEXT,
    close_time TEXT,
    ttc_seconds REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    yes_mid REAL,
    spread_cents REAL,
    volume INTEGER,
    liquidity INTEGER,
    open_interest INTEGER,
    status TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_radar_scan_rank
    ON market_radar_candidates(scan_id, rank);
CREATE INDEX IF NOT EXISTS idx_market_radar_ticker_time
    ON market_radar_candidates(ticker, captured_at);

CREATE TABLE IF NOT EXISTS analyst_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT,
    content TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyst_messages_conversation
    ON analyst_messages(conversation_id, created_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(market_radar_candidates)")
        }
        for name, column_type in {
            "event_title": "TEXT",
            "event_sub_title": "TEXT",
            "market_title": "TEXT",
            "slug": "TEXT",
        }.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE market_radar_candidates ADD COLUMN {name} {column_type}"
                )

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

    # ---------- Strategy decisions ----------

    def save_strategy_decision(self, decision: StrategyDecision) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO strategy_decisions
                   (ticker, captured_at, action, side, probability, confidence,
                    ttc_seconds, limit_price_cents, reason, signal_kind, crossed_50)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.ticker,
                    decision.captured_at.isoformat(),
                    decision.action.value,
                    decision.side.value if decision.side else None,
                    decision.probability,
                    decision.confidence,
                    decision.ttc_seconds,
                    decision.limit_price_cents,
                    decision.reason,
                    decision.signal_kind,
                    1 if decision.crossed_50 else 0,
                ),
            )

    def recent_strategy_decisions(self, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM strategy_decisions
                   ORDER BY captured_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Generic market dry-run snapshots ----------

    def save_market_snapshot(self, snapshot: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO market_snapshots
                   (captured_at, series_ticker, event_ticker, ticker, title,
                    open_time, close_time, yes_bid, yes_ask, no_bid, no_ask,
                    last_price, volume, liquidity, spot_price, ttc_seconds, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot["captured_at"],
                    snapshot["series_ticker"],
                    snapshot.get("event_ticker"),
                    snapshot["ticker"],
                    snapshot.get("title"),
                    snapshot.get("open_time"),
                    snapshot.get("close_time"),
                    snapshot.get("yes_bid"),
                    snapshot.get("yes_ask"),
                    snapshot.get("no_bid"),
                    snapshot.get("no_ask"),
                    snapshot.get("last_price"),
                    snapshot.get("volume"),
                    snapshot.get("liquidity"),
                    snapshot.get("spot_price"),
                    snapshot.get("ttc_seconds"),
                    snapshot.get("source", "dry_run_scanner"),
                ),
            )

    def recent_market_snapshots(self, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM market_snapshots
                   ORDER BY captured_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Market radar ----------

    def save_market_radar_candidates(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO market_radar_candidates
                   (scan_id, captured_at, rank, score, ticker, event_ticker,
                    series_ticker, title, category, event_title, event_sub_title,
                    market_title, slug, close_time, ttc_seconds,
                    yes_bid, yes_ask, no_bid, no_ask, yes_mid, spread_cents,
                    volume, liquidity, open_interest, status, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        r["scan_id"],
                        r["captured_at"],
                        r["rank"],
                        r["score"],
                        r["ticker"],
                        r.get("event_ticker"),
                        r.get("series_ticker"),
                        r.get("title"),
                        r.get("category"),
                        r.get("event_title"),
                        r.get("event_sub_title"),
                        r.get("market_title"),
                        r.get("slug"),
                        r.get("close_time"),
                        r.get("ttc_seconds"),
                        r.get("yes_bid"),
                        r.get("yes_ask"),
                        r.get("no_bid"),
                        r.get("no_ask"),
                        r.get("yes_mid"),
                        r.get("spread_cents"),
                        r.get("volume"),
                        r.get("liquidity"),
                        r.get("open_interest"),
                        r.get("status"),
                        r.get("raw"),
                    )
                    for r in rows
                ],
            )

    def recent_market_radar(self, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            latest = conn.execute(
                "SELECT scan_id FROM market_radar_candidates ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                return []
            rows = conn.execute(
                """SELECT * FROM market_radar_candidates
                   WHERE scan_id = ?
                   ORDER BY rank ASC LIMIT ?""",
                (latest["scan_id"], limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def market_radar_history(self, limit: int = 1000) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM market_radar_candidates
                   ORDER BY captured_at DESC, rank ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Analyst ----------

    def save_analyst_message(self, conversation_id: str, role: str, content: str,
                             provider: str | None = None,
                             metadata: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO analyst_messages
                   (conversation_id, created_at, role, provider, content, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (conversation_id, datetime.now(timezone.utc).isoformat(), role,
                 provider, content, metadata),
            )

    def analyst_messages(self, conversation_id: str, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM analyst_messages
                   WHERE conversation_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        return [dict(r) for r in rows][::-1]

    def recent_analyst_conversations(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT conversation_id, MAX(created_at) AS updated_at,
                          COUNT(*) AS messages,
                          substr(MAX(CASE WHEN role = 'user' THEN content END), 1, 120) AS title
                   FROM analyst_messages
                   GROUP BY conversation_id
                   ORDER BY updated_at DESC LIMIT ?""",
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

    def count_real_orders_since(self, submitted_at: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM orders
                   WHERE dry_run = 0
                     AND ok = 1
                     AND action = 'buy'
                     AND (
                        json_extract(raw, '$.order.status') IN ('executed', 'filled')
                        OR CAST(COALESCE(json_extract(raw, '$.order.fill_count_fp'), '0') AS REAL) > 0
                        OR id IN (
                            SELECT order_id FROM trade_outcomes
                            WHERE entry_price_cents IS NOT NULL
                        )
                     )
                     AND submitted_at >= ?""",
                (submitted_at,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def has_open_real_trade(self, ticker: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT 1
                   FROM trade_outcomes t
                   JOIN orders o ON o.id = t.order_id
                   WHERE o.dry_run = 0
                     AND o.ok = 1
                     AND o.action = 'buy'
                     AND t.entry_price_cents IS NOT NULL
                     AND t.realized_pnl_dollars IS NULL
                     AND t.ticker = ?
                   LIMIT 1""",
                (ticker,),
            ).fetchone()
        return row is not None

    def risk_summary(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as conn:
            open_rows = conn.execute(
                """SELECT t.order_id, t.ticker, t.side, t.entry_price_cents, t.count,
                          o.submitted_at
                   FROM trade_outcomes t
                   JOIN orders o ON o.id = t.order_id
                   WHERE o.dry_run = 0
                     AND o.ok = 1
                     AND o.action = 'buy'
                     AND t.entry_price_cents IS NOT NULL
                     AND t.realized_pnl_dollars IS NULL
                   ORDER BY o.submitted_at DESC"""
            ).fetchall()
            daily = conn.execute(
                """SELECT COUNT(*) AS trades,
                          COALESCE(SUM(COALESCE(t.realized_pnl_dollars, 0)), 0) AS pnl
                   FROM orders o
                   LEFT JOIN trade_outcomes t ON t.order_id = o.id
                   WHERE o.dry_run = 0
                     AND o.ok = 1
                     AND o.action = 'buy'
                     AND t.entry_price_cents IS NOT NULL
                     AND substr(o.submitted_at, 1, 10) = ?""",
                (today,),
            ).fetchone()
            outcomes = conn.execute(
                """SELECT t.realized_pnl_dollars
                   FROM trade_outcomes t
                   JOIN orders o ON o.id = t.order_id
                   WHERE o.dry_run = 0
                     AND o.ok = 1
                     AND o.action = 'buy'
                     AND t.realized_pnl_dollars IS NOT NULL
                   ORDER BY o.submitted_at DESC
                   LIMIT 100"""
            ).fetchall()

        loss_streak = 0
        for row in outcomes:
            if float(row["realized_pnl_dollars"]) < 0:
                loss_streak += 1
            else:
                break

        return {
            "open_risk_cents": int(sum(
                int(r["entry_price_cents"] or 0) * int(r["count"] or 0)
                for r in open_rows
            )),
            "open_trades": [dict(r) for r in open_rows],
            "trades_today": int(daily["trades"] if daily else 0),
            "daily_realized_pnl_dollars": round(float(daily["pnl"] if daily else 0.0), 4),
            "consecutive_losses": loss_streak,
        }

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
                          t.entry_price_cents, t.resolution,
                          t.realized_pnl_dollars, t.fees_paid_dollars
                   FROM orders o
                   LEFT JOIN trade_outcomes t ON t.order_id = o.id
                   WHERE o.dry_run = 0 AND o.ok = 1 AND o.action = 'buy'
                   ORDER BY o.submitted_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
