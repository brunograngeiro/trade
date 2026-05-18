"""Migrate Kalshi ticks + resolutions from trade/data/trade.sqlite3 → trade2.

Old schema (ticks):
  source, market_id, stream_id, timestamp, probability_yes, probability_no, bid, ask

trade2 schema (ticks):
  ticker, captured_at, yes_bid, yes_ask, no_bid, no_ask, last_price, volume

Idempotent via UNIQUE index on (ticker, captured_at) + INSERT OR IGNORE.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.infrastructure.db.sqlite import Database  # noqa: E402


def migrate(src: str, dst: str, batch: int = 10000) -> dict:
    Database(dst)  # ensure schema
    with sqlite3.connect(dst) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_ticks_ticker_time "
            "ON ticks(ticker, captured_at)"
        )

    src_conn = sqlite3.connect(src)
    src_conn.row_factory = sqlite3.Row
    dst_conn = sqlite3.connect(dst, isolation_level=None)
    dst_conn.execute("PRAGMA synchronous=OFF")
    dst_conn.execute("PRAGMA journal_mode=WAL")

    inserted_ticks = 0
    inserted_resolutions = 0

    total = src_conn.execute(
        "SELECT COUNT(*) FROM ticks WHERE source='kalshi' AND probability_yes IS NOT NULL"
    ).fetchone()[0]
    print(f"Source: {src}\nTarget: {dst}\nFound {total} Kalshi ticks\n")

    cursor = src_conn.execute("""
        SELECT market_id, timestamp, probability_yes, bid, ask
        FROM ticks
        WHERE source='kalshi' AND probability_yes IS NOT NULL
        ORDER BY market_id, timestamp
    """)

    before = dst_conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]

    dst_conn.execute("BEGIN")
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        payload = []
        for r in rows:
            ticker = r["market_id"]
            ts = r["timestamp"]
            try:
                captured = datetime.fromisoformat(ts).isoformat()
            except (TypeError, ValueError):
                captured = ts
            yes_mid = float(r["probability_yes"])
            yes_bid = float(r["bid"]) if r["bid"] is not None else max(0.01, yes_mid - 0.01)
            yes_ask = float(r["ask"]) if r["ask"] is not None else min(0.99, yes_mid + 0.01)
            no_bid = max(0.01, 1.0 - yes_ask)
            no_ask = min(0.99, 1.0 - yes_bid)
            payload.append((
                ticker, captured, yes_bid, yes_ask, no_bid, no_ask, yes_mid, None,
            ))
        dst_conn.executemany(
            """INSERT OR IGNORE INTO ticks
               (ticker, captured_at, yes_bid, yes_ask, no_bid, no_ask, last_price, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        print(f"  ...processed batch of {len(payload)}")
    dst_conn.execute("COMMIT")

    after = dst_conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    inserted_ticks = after - before

    # Resolutions
    res_rows = src_conn.execute(
        """SELECT market_id, official_outcome_yes, official_outcome
           FROM market_resolutions
           WHERE source='kalshi' AND official_outcome_yes IS NOT NULL"""
    ).fetchall()
    print(f"\nFound {len(res_rows)} Kalshi resolutions")
    for r in res_rows:
        ticker = r["market_id"]
        outcome = "yes" if r["official_outcome_yes"] else "no"
        dst_conn.execute(
            """INSERT OR REPLACE INTO market_resolutions (ticker, resolved_at, result, raw)
               VALUES (?, ?, ?, ?)""",
            (ticker, datetime.utcnow().isoformat(), outcome,
             r["official_outcome"] or ""),
        )
        inserted_resolutions += 1

    src_conn.close()
    dst_conn.close()

    return {
        "inserted_ticks": inserted_ticks,
        "inserted_resolutions": inserted_resolutions,
        "total_ticks_now": after,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/home/ubuntu/trade/data/trade.sqlite3")
    ap.add_argument("--dst", default=None)
    args = ap.parse_args()

    settings = get_settings()
    dst = args.dst or settings.db_path
    result = migrate(args.src, dst)
    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
