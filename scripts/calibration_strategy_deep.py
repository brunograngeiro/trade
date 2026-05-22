import math
import re
import sqlite3
import statistics
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta

DB = "data/trade2.sqlite3"
TICKER_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
TTC_BUCKETS = [
    ("0-30s", 0, 30), ("30-60s", 30, 60), ("60-120s", 60, 120),
    ("2-3m", 120, 180), ("3-5m", 180, 300), ("5-8m", 300, 480),
    ("8-12m", 480, 720), ("12-15m", 720, 900),
]


def close_t(ticker):
    match = TICKER_RE.match(ticker)
    if not match:
        return None
    yy, mon, dd, hh, mm = match.groups()
    return datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), int(mm)) + timedelta(hours=4)


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def yes_mid(row):
    if row["yes_bid"] is not None and row["yes_ask"] is not None:
        return (row["yes_bid"] + row["yes_ask"]) / 2
    return row["last_price"]


def conf_side(prob):
    return ("yes", prob) if prob >= 0.5 else ("no", 1 - prob)


def bucket_ttc(ttc):
    for label, lo, hi in TTC_BUCKETS:
        if lo <= ttc < hi:
            return label
    return None


def fee(price, count=1, rate=0.07):
    p = max(0.01, min(0.99, price))
    return math.ceil(rate * count * p * (1 - p) * 100) / 100


def value_at_or_before(ts, arr_t, arr_v):
    idx = bisect_left(arr_t, ts) - 1
    return None if idx < 0 else arr_v[idx]


def value_after(ts, arr_t, arr_v):
    idx = bisect_left(arr_t, ts)
    return None if idx >= len(arr_t) else arr_v[idx]


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
res = {
    r["ticker"]: r["result"]
    for r in conn.execute(
        "SELECT ticker, result FROM market_resolutions WHERE result IN ('yes', 'no')"
    )
}
by = defaultdict(list)
for row in conn.execute(
    """SELECT ticker, captured_at, yes_bid, yes_ask, no_bid, no_ask, last_price
       FROM ticks ORDER BY ticker, captured_at"""
):
    if row["ticker"] not in res:
        continue
    ct = close_t(row["ticker"])
    prob = yes_mid(row)
    if ct is None or prob is None:
        continue
    captured = parse(row["captured_at"])
    ttc = (ct - captured).total_seconds()
    if not 0 <= ttc <= 900:
        continue
    by[row["ticker"]].append({
        "t": captured,
        "ttc": ttc,
        "p": max(0, min(1, prob)),
        "yes_ask": row["yes_ask"],
        "no_ask": row["no_ask"],
        "yes_bid": row["yes_bid"],
        "no_bid": row["no_bid"],
    })

print("DATA markets", len(by), "resolved", len(res))

print("\n=== 1) PRICE MAX BY TTC / CONFIDENCE ===")
for conf_min in (0.60, 0.70, 0.80, 0.90, 0.95):
    print(f"\nconf>={conf_min*100:.0f}% first entry per market/bucket")
    print("bucket       n win%  avg_entry avg_pnl total   best_max_price_by_avgpnl")
    for label, lo, hi in TTC_BUCKETS:
        trades = []
        for ticker, samples in by.items():
            for x in samples:
                if not lo <= x["ttc"] < hi:
                    continue
                side, confidence = conf_side(x["p"])
                if confidence < conf_min:
                    continue
                ask = x["yes_ask"] if side == "yes" else x["no_ask"]
                if ask is None:
                    continue
                won = side == res[ticker]
                pnl = (1 if won else 0) - ask - fee(ask, 1)
                trades.append((ask, won, pnl))
                break
        if not trades:
            continue
        wins = sum(1 for _, won, _ in trades if won)
        avg_entry = sum(ask for ask, _, _ in trades) / len(trades)
        avg_pnl = sum(pnl for *_, pnl in trades) / len(trades)
        total = sum(pnl for *_, pnl in trades)
        best = None
        for maxc in range(40, 100):
            sub = [trade for trade in trades if trade[0] * 100 <= maxc]
            if len(sub) < 20:
                continue
            ap = sum(pnl for *_, pnl in sub) / len(sub)
            wr = sum(1 for _, won, _ in sub if won) / len(sub)
            if best is None or ap > best[1]:
                best = (maxc, ap, len(sub), wr)
        bests = "none" if best is None else (
            f"{best[0]}c apnl={best[1]:+.3f} n={best[2]} win={best[3]*100:.1f}%"
        )
        print(
            f"{label:<10} {len(trades):>4} {wins/len(trades)*100:>5.1f}% "
            f"${avg_entry:>5.3f} {avg_pnl:>+7.3f} {total:>+7.2f}  {bests}"
        )

print("\n=== 2) 50 CROSS WITH CONFIRMATION ===")
print("window confirm_to hold_s n win% avg_entry avg_pnl total")
for window in (30, 60, 120, 180, 300):
    for target in (0.55, 0.60, 0.65):
        for hold in (0, 5, 10, 20):
            trades = []
            for ticker, samples in by.items():
                sub = [x for x in samples if x["ttc"] <= window]
                prev = None
                for idx, x in enumerate(sub):
                    side = "yes" if x["p"] > 0.5 else "no" if x["p"] < 0.5 else None
                    if side is None:
                        continue
                    if prev and side != prev:
                        entry = None
                        for j in range(idx, len(sub)):
                            y = sub[j]
                            s2 = "yes" if y["p"] > 0.5 else "no" if y["p"] < 0.5 else None
                            if s2 != side:
                                break
                            confidence = y["p"] if side == "yes" else 1 - y["p"]
                            if confidence < target:
                                continue
                            if hold == 0:
                                entry = y
                                break
                            end_time = y["t"] + timedelta(seconds=hold)
                            ok = False
                            for z in sub[j:]:
                                if z["t"] > end_time:
                                    ok = True
                                    break
                                s3 = "yes" if z["p"] > 0.5 else "no" if z["p"] < 0.5 else None
                                if s3 != side:
                                    break
                            if ok:
                                entry = y
                                break
                        if entry:
                            ask = entry["yes_ask"] if side == "yes" else entry["no_ask"]
                            if ask is not None:
                                won = side == res[ticker]
                                pnl = (1 if won else 0) - ask - fee(ask, 1)
                                trades.append((ask, won, pnl))
                            break
                    prev = side
            if len(trades) >= 10:
                wins = sum(1 for _, won, _ in trades if won)
                avg_entry = sum(ask for ask, _, _ in trades) / len(trades)
                avg_pnl = sum(pnl for *_, pnl in trades) / len(trades)
                total = sum(pnl for *_, pnl in trades)
                print(
                    f"{window:<6} {target:<10.2f} {hold:<6} {len(trades):>3} "
                    f"{wins/len(trades)*100:>5.1f}% ${avg_entry:>5.3f} "
                    f"{avg_pnl:>+7.3f} {total:>+7.2f}"
                )

print("\n=== 3) REVERSAL 60<->40 LATER SIDE ===")
print("later_ttc n new_side_win% avg_entry avg_pnl total")
rev_by = defaultdict(list)
for ticker, samples in by.items():
    first60 = first40 = None
    for x in samples:
        if x["p"] >= 0.60 and first60 is None:
            first60 = x
        if x["p"] <= 0.40 and first40 is None:
            first40 = x
    if not first60 or not first40:
        continue
    if first60["ttc"] > first40["ttc"]:
        later, side = first40, "no"
    else:
        later, side = first60, "yes"
    label = bucket_ttc(later["ttc"])
    ask = later["yes_ask"] if side == "yes" else later["no_ask"]
    if label and ask is not None:
        won = side == res[ticker]
        pnl = (1 if won else 0) - ask - fee(ask, 1)
        rev_by[label].append((ask, won, pnl))
for label, _, _ in TTC_BUCKETS:
    trades = rev_by[label]
    if trades:
        wins = sum(1 for _, won, _ in trades if won)
        avg_entry = sum(ask for ask, _, _ in trades) / len(trades)
        avg_pnl = sum(pnl for *_, pnl in trades) / len(trades)
        total = sum(pnl for *_, pnl in trades)
        print(
            f"{label:<10} {len(trades):>3} {wins/len(trades)*100:>6.1f}% "
            f"${avg_entry:>5.3f} {avg_pnl:>+7.3f} {total:>+7.2f}"
        )

print("\n=== 4) SPREAD / TRADABILITY (ttc<=180 conf>=60 first per market) ===")
spread_bins = [("<=1c", 0, 1), ("1-2c", 1, 2), ("2-5c", 2, 5), (">5c", 5, 999)]
spread_stats = defaultdict(list)
for ticker, samples in by.items():
    for x in samples:
        if x["ttc"] > 180:
            continue
        side, confidence = conf_side(x["p"])
        if confidence < 0.60:
            continue
        ask = x["yes_ask"] if side == "yes" else x["no_ask"]
        bid = x["yes_bid"] if side == "yes" else x["no_bid"]
        if ask is None or bid is None:
            continue
        spread = (ask - bid) * 100
        for label, lo, hi in spread_bins:
            if (lo == 0 and spread <= hi) or (lo < spread <= hi):
                won = side == res[ticker]
                pnl = (1 if won else 0) - ask - fee(ask, 1)
                spread_stats[label].append((ask, won, pnl))
                break
        break
for label, _, _ in spread_bins:
    trades = spread_stats[label]
    if trades:
        wins = sum(1 for _, won, _ in trades if won)
        avg_entry = sum(ask for ask, _, _ in trades) / len(trades)
        avg_pnl = sum(pnl for *_, pnl in trades) / len(trades)
        total = sum(pnl for *_, pnl in trades)
        print(
            f"{label:<6} n={len(trades):>4} win={wins/len(trades)*100:>5.1f}% "
            f"entry=${avg_entry:.3f} avg_pnl={avg_pnl:+.3f} total={total:+.2f}"
        )

print("\n=== 5) SPOT LEAD/LAG -> KALSHI FUTURE DELTA ===")
spot_rows = [
    (parse(r["captured_at"]), float(r["price"]))
    for r in conn.execute(
        "SELECT captured_at, price FROM spot_ticks WHERE product='BTC-USD' ORDER BY captured_at"
    )
]
spot_t = [t for t, _ in spot_rows]
spot_p = [p for _, p in spot_rows]
k_rows = []
for samples in by.values():
    for x in samples:
        k_rows.append((x["t"], x["p"]))
k_rows.sort()
k_t = [t for t, _ in k_rows]
k_p = [p for _, p in k_rows]
for past in (5, 10, 30, 60):
    for future in (5, 10, 30):
        vals = []
        for t, prob in k_rows:
            if not spot_t or t < spot_t[0] + timedelta(seconds=past):
                continue
            if t > spot_t[-1] - timedelta(seconds=future):
                continue
            s0 = value_at_or_before(t - timedelta(seconds=past), spot_t, spot_p)
            s1 = value_at_or_before(t, spot_t, spot_p)
            kf = value_after(t + timedelta(seconds=future), k_t, k_p)
            if s0 is None or s1 is None or kf is None:
                continue
            spot_delta = (s1 / s0 - 1) * 100
            kalshi_delta = kf - prob
            vals.append((spot_delta, kalshi_delta))
        for thresh in (0.03, 0.05, 0.08, 0.12):
            up = [kd for sd, kd in vals if sd >= thresh]
            down = [kd for sd, kd in vals if sd <= -thresh]
            if len(up) >= 30 or len(down) >= 30:
                up_mean = statistics.mean(up) if up else 0
                down_mean = statistics.mean(down) if down else 0
                print(
                    f"past{past}s->future{future}s thresh{thresh:.2f}% "
                    f"up_n={len(up)} up_kDelta={up_mean:+.4f} "
                    f"down_n={len(down)} down_kDelta={down_mean:+.4f}"
                )
conn.close()
