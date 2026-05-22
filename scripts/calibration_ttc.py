"""Calibration of Kalshi-implied probability conditional on time-to-close.

Question: at each second-to-close, how well does the mid price predict the
actual market outcome? Output:
  - Brier score per TTC bucket (lower = more accurate)
  - Reliability table (prob bucket x TTC bucket -> observed YES rate)
  - Edge-frontier: smallest TTC at which prob >= 0.X has CI lower bound > X
  - Reversal stats: how often does a 60%/40% cross resolve in the *later* side

No state is mutated. Pure read from data/trade2.sqlite3.
"""

from __future__ import annotations

import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


TICKER_TIME_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def close_time_from_ticker(ticker: str) -> datetime | None:
    """Parse KXBTC15M-26MAY161745-45 -> close datetime in UTC.

    The ticker timestamp is in US Eastern Time (Kalshi convention). For our
    dataset (May 2026, fully in EDT) the offset to UTC is +4h.
    """
    m = TICKER_TIME_RE.match(ticker)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    et = datetime(
        year=2000 + int(yy), month=MONTHS[mon], day=int(dd),
        hour=int(hh), minute=int(mm), second=0,
    )
    return et + timedelta(hours=4)  # EDT -> UTC


def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def yes_mid(yes_bid, yes_ask, last_price):
    if yes_bid is not None and yes_ask is not None:
        return (yes_bid + yes_ask) / 2.0
    return last_price


# TTC buckets (seconds remaining until close)
TTC_BUCKETS = [
    ("0-30s",   0,   30),
    ("30-60s",  30,  60),
    ("60-120s", 60,  120),
    ("2-3min",  120, 180),
    ("3-5min",  180, 300),
    ("5-8min",  300, 480),
    ("8-12min", 480, 720),
    ("12-15m",  720, 900),
]

# Probability buckets (yes_mid)
PROB_BUCKETS = [
    ("0-10",  0.00, 0.10),
    ("10-20", 0.10, 0.20),
    ("20-30", 0.20, 0.30),
    ("30-40", 0.30, 0.40),
    ("40-50", 0.40, 0.50),
    ("50-60", 0.50, 0.60),
    ("60-70", 0.60, 0.70),
    ("70-80", 0.70, 0.80),
    ("80-90", 0.80, 0.90),
    ("90-100", 0.90, 1.001),
]


def bucket_index(value: float, buckets: list[tuple[str, float, float]]) -> int | None:
    for i, (_, lo, hi) in enumerate(buckets):
        if lo <= value < hi:
            return i
    return None


def main() -> int:
    settings = get_settings()
    db = settings.db_path
    print(f"DB: {db}\n")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    resolutions = {r["ticker"]: r["result"] for r in
                   conn.execute("SELECT ticker, result FROM market_resolutions")}

    # Stream ticks once; aggregate into buckets.
    # cell[(ttc_idx, prob_idx)] = [count, yes_wins, brier_sum]
    cell = defaultdict(lambda: [0, 0, 0.0])
    ttc_only = defaultdict(lambda: [0, 0, 0.0])  # per TTC: total brier
    ticker_count = 0
    tick_count = 0
    skipped_no_close = 0
    skipped_no_prob = 0
    skipped_no_res = 0

    # Track reversal events per market
    # market_state[ticker] = {"high60_at": ts, "low40_at": ts, "phase60": phase, ...}
    market_high = {}  # ticker -> earliest captured_at with yes_mid>=0.60
    market_low = {}   # ticker -> earliest captured_at with yes_mid<=0.40
    market_first_60_ttc = {}  # TTC when first crossed >=0.60
    market_first_40_ttc = {}  # TTC when first crossed <=0.40

    last_ticker = None
    for row in conn.execute(
        "SELECT ticker, captured_at, yes_bid, yes_ask, no_bid, no_ask, "
        "last_price FROM ticks ORDER BY ticker, captured_at"
    ):
        ticker = row["ticker"]
        if ticker != last_ticker:
            ticker_count += 1
            last_ticker = ticker
        tick_count += 1

        result = resolutions.get(ticker)
        if result not in ("yes", "no"):
            skipped_no_res += 1
            continue

        close_t = close_time_from_ticker(ticker)
        if close_t is None:
            skipped_no_close += 1
            continue

        prob = yes_mid(row["yes_bid"], row["yes_ask"], row["last_price"])
        if prob is None:
            skipped_no_prob += 1
            continue
        prob = max(0.0, min(1.0, prob))

        captured = datetime.fromisoformat(row["captured_at"]).replace(tzinfo=None)
        ttc = (close_t - captured).total_seconds()
        if ttc < 0 or ttc > 900:
            continue

        ttc_idx = bucket_index(ttc, TTC_BUCKETS)
        prob_idx = bucket_index(prob, PROB_BUCKETS)
        if ttc_idx is None or prob_idx is None:
            continue

        won = 1 if result == "yes" else 0
        brier = (prob - won) ** 2

        cell[(ttc_idx, prob_idx)][0] += 1
        cell[(ttc_idx, prob_idx)][1] += won
        cell[(ttc_idx, prob_idx)][2] += brier

        ttc_only[ttc_idx][0] += 1
        ttc_only[ttc_idx][1] += won
        ttc_only[ttc_idx][2] += brier

        if prob >= 0.60 and ticker not in market_first_60_ttc:
            market_first_60_ttc[ticker] = ttc
        if prob <= 0.40 and ticker not in market_first_40_ttc:
            market_first_40_ttc[ticker] = ttc

    conn.close()

    print(f"Markets: {ticker_count}, ticks: {tick_count}")
    print(f"  skipped: no_close={skipped_no_close} no_prob={skipped_no_prob} "
          f"no_res={skipped_no_res}\n")

    # === Section 1: Brier score per TTC bucket ===
    print("=" * 72)
    print("SECTION 1 — Brier score per TTC bucket (lower = more predictive)")
    print("=" * 72)
    print(f"{'TTC bucket':<10} {'n':>8} {'YES%':>7} {'Brier':>8} {'Naive 0.5':>10}")
    print("-" * 50)
    for idx, (label, _, _) in enumerate(TTC_BUCKETS):
        n, wins, brier_sum = ttc_only[idx]
        if n == 0:
            continue
        avg_brier = brier_sum / n
        yes_pct = wins / n
        naive_brier = 0.25  # always-0.5 strategy
        print(f"{label:<10} {n:>8} {yes_pct*100:>6.1f}% "
              f"{avg_brier:>7.4f}  {naive_brier:.4f}")

    print()
    # === Section 2: Reliability table ===
    print("=" * 72)
    print("SECTION 2 — Reliability: rows=TTC, cols=prob bucket -> observed YES%")
    print("=" * 72)
    header = f"{'TTC':<10}" + "".join(f"{lbl:>9}" for lbl, _, _ in PROB_BUCKETS)
    print(header)
    print("-" * len(header))
    for tidx, (tlabel, _, _) in enumerate(TTC_BUCKETS):
        line = f"{tlabel:<10}"
        for pidx, _ in enumerate(PROB_BUCKETS):
            n, wins, _ = cell[(tidx, pidx)]
            if n < 5:
                line += f"{'·':>9}"
            else:
                p = wins / n
                line += f"{p*100:>7.0f}%({n})"[-9:].rjust(9)
        print(line)
    print()
    print("Cell legend: observed YES% (n). '·' = n<5.")
    print()

    # === Section 3: Calibration gap ===
    print("=" * 72)
    print("SECTION 3 — Calibration gap: |observed - prob_midpoint| per TTC")
    print("=" * 72)
    print(f"{'TTC':<10}  Worst-bucket calibration gaps (prob -> observed, n)")
    print("-" * 72)
    for tidx, (tlabel, _, _) in enumerate(TTC_BUCKETS):
        gaps = []
        for pidx, (plabel, plo, phi) in enumerate(PROB_BUCKETS):
            n, wins, _ = cell[(tidx, pidx)]
            if n < 20:
                continue
            mid = (plo + min(phi, 1.0)) / 2
            obs = wins / n
            gaps.append((plabel, mid, obs, n, abs(obs - mid)))
        gaps.sort(key=lambda x: -x[4])
        snippet = ", ".join(
            f"{lbl}:{mid:.2f}->{obs:.2f}(n={n})"
            for lbl, mid, obs, n, _ in gaps[:3]
        )
        print(f"{tlabel:<10}  {snippet}")
    print()

    # === Section 4: Edge-frontier ===
    print("=" * 72)
    print("SECTION 4 — Edge-frontier: at each TTC, where prob has CI_lo > breakeven")
    print("=" * 72)
    print("(For prob bucket P, breakeven_win_rate ~ midpoint + fee_pct(midpoint))")
    print("(fee ~ 0.07*P*(1-P), so breakeven_win = midpoint + 0.07*mid*(1-mid))")
    print("-" * 72)
    print(f"{'TTC':<10} {'prob_bucket':<10} {'n':>5} {'obs%':>6} "
          f"{'CI_lo':>6} {'need':>6} {'edge?':>6}")
    print("-" * 60)
    for tidx, (tlabel, _, _) in enumerate(TTC_BUCKETS):
        for pidx, (plabel, plo, phi) in enumerate(PROB_BUCKETS):
            n, wins, _ = cell[(tidx, pidx)]
            if n < 30:
                continue
            mid = (plo + min(phi, 1.0)) / 2
            obs = wins / n
            ci_lo, _ = wilson_ci(wins, n)
            fee = 0.07 * mid * (1 - mid)
            need = mid + fee  # rough breakeven win rate
            edge = "YES" if ci_lo > need else ""
            if obs > mid + 0.05 or edge:  # only show interesting cells
                print(f"{tlabel:<10} {plabel:<10} {n:>5} {obs*100:>5.1f}% "
                      f"{ci_lo*100:>5.1f}% {need*100:>5.1f}% {edge:>6}")
    print()

    # === Section 5: Reversal analysis ===
    print("=" * 72)
    print("SECTION 5 — Reversal analysis (markets that hit BOTH 60% and 40%)")
    print("=" * 72)
    crossed_both = set(market_first_60_ttc.keys()) & set(market_first_40_ttc.keys())
    print(f"Markets that touched both 60%+ and 40%-: {len(crossed_both)}")

    yes_first_then_no = 0
    no_first_then_yes = 0
    yes_first_settled_yes = 0
    yes_first_settled_no = 0
    no_first_settled_yes = 0
    no_first_settled_no = 0
    for tkr in crossed_both:
        ttc_60 = market_first_60_ttc[tkr]
        ttc_40 = market_first_40_ttc[tkr]
        result = resolutions.get(tkr)
        if result not in ("yes", "no"):
            continue
        # ttc decreases over time; earlier event = larger ttc
        if ttc_60 > ttc_40:  # crossed 60 first, then later crossed 40
            yes_first_then_no += 1
            if result == "yes":
                yes_first_settled_yes += 1
            else:
                yes_first_settled_no += 1
        else:  # crossed 40 first, then 60
            no_first_then_yes += 1
            if result == "yes":
                no_first_settled_yes += 1
            else:
                no_first_settled_no += 1

    print()
    print(f"60% first, then 40%: {yes_first_then_no} markets "
          f"-> settled YES={yes_first_settled_yes}, NO={yes_first_settled_no}")
    if yes_first_then_no > 0:
        rate = yes_first_settled_no / yes_first_then_no
        print(f"  After dropping from 60% to 40%, the market settled NO {rate*100:.1f}% of the time")
        print(f"  -> If holding YES from 60%, exit at 40% saves ~{rate*100:.0f}% × $0.60 loss avoidance")
    print()
    print(f"40% first, then 60%: {no_first_then_yes} markets "
          f"-> settled YES={no_first_settled_yes}, NO={no_first_settled_no}")
    if no_first_then_yes > 0:
        rate = no_first_settled_yes / no_first_then_yes
        print(f"  After rising from 40% to 60%, the market settled YES {rate*100:.1f}% of the time")
    print()

    # When did the reversal happen? Bucket the LATER crossing by TTC.
    rev_by_ttc = defaultdict(lambda: [0, 0])  # ttc_label -> [n, settled_in_new_dir]
    for tkr in crossed_both:
        ttc_60 = market_first_60_ttc[tkr]
        ttc_40 = market_first_40_ttc[tkr]
        result = resolutions.get(tkr)
        if result not in ("yes", "no"):
            continue
        if ttc_60 > ttc_40:  # 60 first, 40 later — "new direction" = NO
            later_ttc = ttc_40
            new_dir_won = (result == "no")
        else:
            later_ttc = ttc_60
            new_dir_won = (result == "yes")
        idx = bucket_index(later_ttc, TTC_BUCKETS)
        if idx is None:
            continue
        rev_by_ttc[TTC_BUCKETS[idx][0]][0] += 1
        if new_dir_won:
            rev_by_ttc[TTC_BUCKETS[idx][0]][1] += 1

    print("Reversal hit rate by TTC of the LATER crossing (does the reversal stick?):")
    print(f"{'TTC of reversal':<18} {'n':>5} {'new_dir_won%':>13}")
    for label, _, _ in TTC_BUCKETS:
        n, won = rev_by_ttc[label]
        if n < 5:
            continue
        print(f"{label:<18} {n:>5} {won/n*100:>12.1f}%")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
