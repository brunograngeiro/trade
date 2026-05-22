"""Read-only analytics helpers for the dashboard analyst tab."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


TICKER_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def readonly_query(db_path: str, sql: str, limit: int = 300) -> dict:
    cleaned = sql.strip()
    lowered = cleaned.lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("only_select_queries_are_allowed")
    if ";" in cleaned.rstrip(";"):
        raise ValueError("multiple_statements_are_not_allowed")

    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM ({cleaned.rstrip(';')}) LIMIT ?", (limit,)).fetchall()
        return {"columns": rows[0].keys() if rows else [], "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def final_minute_probability_stats(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    resolutions = {
        r["ticker"]: r["result"]
        for r in conn.execute(
            "SELECT ticker, result FROM market_resolutions WHERE result IN ('yes', 'no')"
        )
    }
    buckets = {
        "45-60s": [],
        "30-45s": [],
        "15-30s": [],
        "0-15s": [],
    }
    by_ticker: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in conn.execute(
        """SELECT ticker, captured_at, yes_bid, yes_ask, last_price
           FROM ticks
           WHERE ticker LIKE 'KXBTC15M-%'
           ORDER BY ticker, captured_at"""
    ):
        ticker = row["ticker"]
        if ticker not in resolutions:
            continue
        close_time = _close_time_from_ticker(ticker)
        prob = _yes_mid(row)
        if close_time is None or prob is None:
            continue
        captured = datetime.fromisoformat(row["captured_at"]).replace(tzinfo=None)
        ttc = (close_time - captured).total_seconds()
        if 0 <= ttc <= 60:
            by_ticker[ticker].append((ttc, prob))

    for ticker, samples in by_ticker.items():
        result = resolutions[ticker]
        for ttc, prob in samples:
            label = _bucket(ttc)
            if label is None:
                continue
            side = "yes" if prob >= 0.5 else "no"
            buckets[label].append({
                "prob": prob,
                "confidence": max(prob, 1 - prob),
                "correct": 1 if side == result else 0,
                "cross_zone": 1 if 0.45 <= prob <= 0.55 else 0,
            })

    rows = []
    for label, samples in buckets.items():
        n = len(samples)
        rows.append({
            "bucket": label,
            "ticks": n,
            "avg_yes_prob": _avg([s["prob"] for s in samples]),
            "avg_confidence": _avg([s["confidence"] for s in samples]),
            "direction_accuracy": _avg([s["correct"] for s in samples]),
            "pct_near_50": _avg([s["cross_zone"] for s in samples]),
        })
    return {"markets": len(by_ticker), "rows": rows}


def _close_time_from_ticker(ticker: str) -> datetime | None:
    match = TICKER_RE.match(ticker)
    if not match:
        return None
    yy, mon, dd, hh, mm = match.groups()
    et = datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), int(mm))
    return et + timedelta(hours=4)


def _yes_mid(row: sqlite3.Row) -> float | None:
    if row["yes_bid"] is not None and row["yes_ask"] is not None:
        return (row["yes_bid"] + row["yes_ask"]) / 2
    return row["last_price"]


def _bucket(ttc: float) -> str | None:
    if 45 <= ttc <= 60:
        return "45-60s"
    if 30 <= ttc < 45:
        return "30-45s"
    if 15 <= ttc < 30:
        return "15-30s"
    if 0 <= ttc < 15:
        return "0-15s"
    return None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None
