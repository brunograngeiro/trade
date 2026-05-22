import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

DB = "data/trade2.sqlite3"
TICKER = "KXBTC15M-26MAY181530-30"
TICKER_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def close_t(ticker):
    match = TICKER_RE.match(ticker)
    yy, mon, dd, hh, mm = match.groups()
    return datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), int(mm)) + timedelta(hours=4)


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def mid(row):
    if row["yes_bid"] is not None and row["yes_ask"] is not None:
        return (row["yes_bid"] + row["yes_ask"]) / 2
    return row["last_price"]


def side(prob):
    return "yes" if prob > 0.5 else "no" if prob < 0.5 else "tie"


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
ct = close_t(TICKER)
print("MARKET", TICKER, "close_utc", ct)
row = conn.execute("SELECT * FROM market_resolutions WHERE ticker=?", (TICKER,)).fetchone()
print("resolution", dict(row or {}))

print("\norders:")
for row in conn.execute(
    """SELECT id,submitted_at,side,action,count,limit_price_cents,dry_run,ok,raw
       FROM orders WHERE ticker=? ORDER BY submitted_at""",
    (TICKER,),
):
    raw = json.loads(row["raw"]) if row["raw"] else {}
    order = raw.get("order", {})
    item = dict(row)
    item.update({
        "status": order.get("status"),
        "fill_count": order.get("fill_count_fp"),
        "remaining_count": order.get("remaining_count_fp"),
        "taker_fee": order.get("taker_fees_dollars"),
        "maker_fee": order.get("maker_fees_dollars"),
    })
    print(item)

print("\noutcomes:")
for row in conn.execute("SELECT * FROM trade_outcomes WHERE ticker=?", (TICKER,)):
    print(dict(row))

print("\ndecisions around market:")
for row in conn.execute(
    """SELECT captured_at,action,side,probability,confidence,ttc_seconds,
              limit_price_cents,reason,signal_kind,crossed_50
       FROM strategy_decisions WHERE ticker=? ORDER BY captured_at""",
    (TICKER,),
):
    print(dict(row))

print("\nkey tick timeline last 4min or changes >=5pp:")
rows = list(conn.execute(
    """SELECT captured_at,yes_bid,yes_ask,no_bid,no_ask,last_price
       FROM ticks WHERE ticker=? ORDER BY captured_at""",
    (TICKER,),
))
prev = None
for row in rows:
    prob = mid(row)
    if prob is None:
        continue
    captured = parse(row["captured_at"])
    ttc = (ct - captured).total_seconds()
    show = ttc <= 240
    if prev is None or abs(prob - prev) >= 0.05 or side(prob) != side(prev):
        show = True
    if show:
        print(
            f"{row['captured_at']} ttc={ttc:6.1f}s p={prob:.3f} side={side(prob)} "
            f"yes_bid={row['yes_bid']} yes_ask={row['yes_ask']} "
            f"no_bid={row['no_bid']} no_ask={row['no_ask']} last={row['last_price']}"
        )
    prev = prob

print("\nspot around orders:")
for row in conn.execute(
    """SELECT captured_at,price,bid,ask FROM spot_ticks
       WHERE product='BTC-USD' AND captured_at BETWEEN ? AND ?
       ORDER BY captured_at""",
    ((ct - timedelta(minutes=4)).isoformat(), (ct + timedelta(seconds=30)).isoformat()),
):
    print(dict(row))

print("\n=== false spike stats ===")
res = {
    row["ticker"]: row["result"]
    for row in conn.execute("SELECT ticker,result FROM market_resolutions WHERE result IN ('yes','no')")
}
by = defaultdict(list)
for row in conn.execute(
    "SELECT ticker,captured_at,yes_bid,yes_ask,last_price FROM ticks ORDER BY ticker,captured_at"
):
    if row["ticker"] not in res or not TICKER_RE.match(row["ticker"]):
        continue
    prob = mid(row)
    if prob is None:
        continue
    close = close_t(row["ticker"])
    captured = parse(row["captured_at"])
    ttc = (close - captured).total_seconds()
    if 0 <= ttc <= 900:
        by[row["ticker"]].append((ttc, prob))

for window in (900, 600, 300, 180, 120, 60):
    for threshold in (0.60, 0.70, 0.80, 0.90):
        n = closed_opposite = crossed_back = opposite_same_threshold = 0
        for ticker, samples in by.items():
            event = None
            for ttc, prob in samples:
                if ttc <= window and max(prob, 1 - prob) >= threshold:
                    event = (ttc, prob, side(prob))
                    break
            if not event:
                continue
            n += 1
            event_ttc, _event_prob, event_side = event
            later = [(ttc, prob) for ttc, prob in samples if ttc < event_ttc]
            if res[ticker] != event_side:
                closed_opposite += 1
            if any(side(prob) != event_side and side(prob) != "tie" for _ttc, prob in later):
                crossed_back += 1
            if any(
                (prob <= 1 - threshold if event_side == "yes" else prob >= threshold)
                for _ttc, prob in later
            ):
                opposite_same_threshold += 1
        if n >= 30:
            print(
                f"window<={window:>3}s thresh>={threshold:.2f} n={n:>4} "
                f"closed_opposite={closed_opposite/n*100:>5.1f}% "
                f"crossed_back50={crossed_back/n*100:>5.1f}% "
                f"opposite_same_thresh={opposite_same_threshold/n*100:>5.1f}%"
            )

conn.close()
