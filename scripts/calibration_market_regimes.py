import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta

DB = "data/trade2.sqlite3"
TICKER_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def close_t(ticker):
    match = TICKER_RE.match(ticker)
    if not match:
        return None
    yy, mon, dd, hh, mm = match.groups()
    return datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), int(mm)) + timedelta(hours=4)


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def mid(row):
    if row["yes_bid"] is not None and row["yes_ask"] is not None:
        return (row["yes_bid"] + row["yes_ask"]) / 2
    return row["last_price"]


def side(prob):
    if prob > 0.5:
        return "yes"
    if prob < 0.5:
        return "no"
    return None


def conf_side(prob):
    return side(prob), max(prob, 1 - prob)


def regime(prob):
    conf = max(prob, 1 - prob)
    if 0.40 <= prob <= 0.60:
        return "40-60"
    if conf < 0.70:
        return "60-70"
    if conf < 0.80:
        return "70-80"
    if conf < 0.90:
        return "80-90"
    if conf < 0.95:
        return "90-95"
    if conf < 0.99:
        return "95-99"
    return ">=99"


def bucket(ttc):
    if 720 <= ttc <= 900:
        return "12-15m"
    if 480 <= ttc < 720:
        return "8-12m"
    if 300 <= ttc < 480:
        return "5-8m"
    if 180 <= ttc < 300:
        return "3-5m"
    if 120 <= ttc < 180:
        return "2-3m"
    if 60 <= ttc < 120:
        return "60-120s"
    if 30 <= ttc < 60:
        return "30-60s"
    if 0 <= ttc < 30:
        return "0-30s"
    return None


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
res = {
    r["ticker"]: r["result"]
    for r in conn.execute("SELECT ticker, result FROM market_resolutions WHERE result IN ('yes','no')")
}
by = defaultdict(list)
for row in conn.execute(
    "SELECT ticker,captured_at,yes_bid,yes_ask,last_price FROM ticks ORDER BY ticker,captured_at"
):
    if row["ticker"] not in res:
        continue
    ct = close_t(row["ticker"])
    prob = mid(row)
    if ct is None or prob is None:
        continue
    captured = parse(row["captured_at"])
    ttc = (ct - captured).total_seconds()
    if 0 <= ttc <= 900:
        by[row["ticker"]].append((ttc, max(0, min(1, prob))))

print("markets", len(by))

print("\n=== A) Predictive power by first observation in each bucket ===")
print("bucket regime n direction_win% reversal_to_opposite%")
stats = defaultdict(lambda: [0, 0, 0])
for ticker, samples in by.items():
    seen_bucket = set()
    for ttc, prob in samples:
        b = bucket(ttc)
        if not b or b in seen_bucket:
            continue
        seen_bucket.add(b)
        s, conf = conf_side(prob)
        if s is None:
            continue
        r = regime(prob)
        key = (b, r)
        stats[key][0] += 1
        if s == res[ticker]:
            stats[key][1] += 1
        else:
            stats[key][2] += 1
for b in ("12-15m", "8-12m", "5-8m", "3-5m", "2-3m", "60-120s", "30-60s", "0-30s"):
    for r in ("40-60", "60-70", "70-80", "80-90", "90-95", "95-99", ">=99"):
        n, ok, rev = stats[(b, r)]
        if n >= 10:
            print(f"{b:<8} {r:<6} {n:>4} {ok/n*100:>6.1f}% {rev/n*100:>6.1f}%")

print("\n=== B) Early apparent resolution that later closed opposite ===")
print("window threshold n closed_opposite% touched_opposite_40_60% touched_opposite_50%")
for window_name, lo, hi in (("12-15m", 720, 900), ("8-12m", 480, 720), ("5-8m", 300, 480), ("3-5m", 180, 300)):
    for threshold in (0.70, 0.80, 0.90, 0.95, 0.99):
        n = closed_opp = touched_opp_4060 = touched_opp_50 = 0
        for ticker, samples in by.items():
            event = None
            for ttc, prob in samples:
                if lo <= ttc < hi:
                    s, conf = conf_side(prob)
                    if s and conf >= threshold:
                        event = (ttc, prob, s)
                        break
            if not event:
                continue
            n += 1
            event_ttc, _prob, event_side = event
            opp = "no" if event_side == "yes" else "yes"
            later = [(ttc, p) for ttc, p in samples if ttc < event_ttc]
            if res[ticker] == opp:
                closed_opp += 1
            if any(side(p) == opp for _, p in later):
                touched_opp_50 += 1
            if any((p <= 0.40 if opp == "no" else p >= 0.60) for _, p in later):
                touched_opp_4060 += 1
        if n >= 10:
            print(
                f"{window_name:<7} {threshold:<9.2f} {n:>4} "
                f"{closed_opp/n*100:>6.1f}% {touched_opp_4060/n*100:>6.1f}% "
                f"{touched_opp_50/n*100:>6.1f}%"
            )

print("\n=== C) Last 50-cross timing ===")
cross_bucket = Counter()
cross_ok = Counter()
cross_total = 0
for ticker, samples in by.items():
    last_side = None
    last_cross = None
    last_cross_side = None
    for ttc, prob in samples:
        s = side(prob)
        if s is None:
            continue
        if last_side and s != last_side:
            last_cross = ttc
            last_cross_side = s
        last_side = s
    if last_cross is not None:
        b = bucket(last_cross)
        cross_total += 1
        cross_bucket[b] += 1
        if last_cross_side == res[ticker]:
            cross_ok[b] += 1
print("markets_with_cross", cross_total)
for b in ("12-15m", "8-12m", "5-8m", "3-5m", "2-3m", "60-120s", "30-60s", "0-30s"):
    n = cross_bucket[b]
    if n:
        print(f"{b:<8} n={n:>4} share={n/cross_total*100:>5.1f}% new_side_won={cross_ok[b]/n*100:>5.1f}%")

print("\n=== D) How much useful final state appears only after 3m ===")
initial_final_pairs = Counter()
for ticker, samples in by.items():
    at3 = None
    final = samples[-1] if samples else None
    for ttc, prob in samples:
        if ttc <= 180:
            at3 = (ttc, prob)
            break
    if at3 and final:
        initial_final_pairs[(regime(at3[1]), regime(final[1]))] += 1
print("regime_at_3m -> final_regime counts")
for (r1, r2), n in initial_final_pairs.most_common(30):
    print(f"{r1:<6} -> {r2:<6} {n:>4}")

conn.close()
