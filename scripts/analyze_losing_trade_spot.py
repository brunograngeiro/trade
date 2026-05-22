import sqlite3
from bisect import bisect_left
from datetime import datetime, timedelta

DB = "data/trade2.sqlite3"
TICKER = "KXBTC15M-26MAY181530-30"
ENTRY = "2026-05-18T19:27:01.730879+00:00"
CROSS = "2026-05-18T19:28:56.226715+00:00"
CLOSE = datetime(2026, 5, 18, 19, 30, 0)


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
spot = [
    (parse(row["captured_at"]), float(row["price"]), row["bid"], row["ask"])
    for row in conn.execute(
        "SELECT captured_at,price,bid,ask FROM spot_ticks "
        "WHERE product='BTC-USD' ORDER BY captured_at"
    )
]
spot_times = [t for t, *_ in spot]


def nearest(ts):
    idx = bisect_left(spot_times, ts)
    candidates = []
    if idx < len(spot):
        candidates.append(spot[idx])
    if idx > 0:
        candidates.append(spot[idx - 1])
    return min(candidates, key=lambda x: abs((x[0] - ts).total_seconds()))


def before(ts):
    idx = bisect_left(spot_times, ts) - 1
    return spot[idx] if idx >= 0 else None


for label, ts in [("entry", parse(ENTRY)), ("cross_yes_58", parse(CROSS)), ("close", CLOSE)]:
    item = nearest(ts)
    print(
        label, ts, "nearest_spot_time", item[0],
        "lag_s", (item[0] - ts).total_seconds(),
        "price", item[1], "bid", item[2], "ask", item[3],
    )
    for sec in [-180, -120, -60, -30, -10, 0, 10, 30, 60, 120]:
        point = before(ts + timedelta(seconds=sec))
        if point:
            print(" ", sec, "s", point[0], point[1])

entry = parse(ENTRY)
base = before(entry)
print("\nspot deltas before entry:")
for sec in [5, 10, 30, 60, 120, 180]:
    old = before(entry - timedelta(seconds=sec))
    if old and base:
        print(
            sec, "s", old[1], "->", base[1],
            "delta_pct", (base[1] / old[1] - 1) * 100,
            "usd", base[1] - old[1],
        )

print("\nspot deltas after entry:")
for sec in [30, 60, 90, 115, 120, 150, 180]:
    future = before(entry + timedelta(seconds=sec))
    if future and base:
        print(
            sec, "s", base[1], "->", future[1],
            "delta_pct", (future[1] / base[1] - 1) * 100,
            "usd", future[1] - base[1],
        )

rows = [
    (
        parse(row["captured_at"]),
        (row["yes_bid"] + row["yes_ask"]) / 2
        if row["yes_bid"] is not None and row["yes_ask"] is not None
        else row["last_price"],
    )
    for row in conn.execute(
        "SELECT captured_at,yes_bid,yes_ask,last_price "
        "FROM ticks WHERE ticker=? ORDER BY captured_at",
        (TICKER,),
    )
]
row_times = [t for t, _ in rows]


def kbefore(ts):
    idx = bisect_left(row_times, ts) - 1
    return rows[idx] if idx >= 0 else None


print("\nkalshi p around entry/future:")
for sec in [-180, -120, -60, -30, 0, 30, 60, 90, 115, 120, 150, 180]:
    item = kbefore(entry + timedelta(seconds=sec))
    if item:
        print(sec, "s", item[0], item[1])

conn.close()
