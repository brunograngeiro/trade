"""Wide sweep with the larger 687-market dataset to find a positive-edge variant.

Tests:
  - Δ explosion grid × phase × side mode (yes / contrarian-no / both)
  - "Late only" with different late-cutoff ratios
  - Plateau (since we never validated it on the full dataset)
"""

from __future__ import annotations

import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.application.signals import SignalConfig, SignalEngine  # noqa: E402
from app.domain.entities import MarketPhase, Side, SignalKind, Tick  # noqa: E402


def kalshi_fee(price: float, count: int = 1, rate: float = 0.07) -> float:
    p = max(0.01, min(0.99, price))
    return math.ceil(rate * count * p * (1 - p) * 100) / 100.0


def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def load_dataset(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    res_map = {r["ticker"]: r["result"] for r in
               conn.execute("SELECT ticker, result FROM market_resolutions")}
    market_windows: dict[str, tuple[datetime, datetime]] = {}
    for row in conn.execute(
        "SELECT ticker, MIN(captured_at) AS s, MAX(captured_at) AS e FROM ticks GROUP BY ticker"
    ):
        market_windows[row["ticker"]] = (datetime.fromisoformat(row["s"]),
                                         datetime.fromisoformat(row["e"]))
    ticks_by_ticker: dict[str, list[Tick]] = {}
    for r in conn.execute("SELECT * FROM ticks ORDER BY ticker, captured_at"):
        ticks_by_ticker.setdefault(r["ticker"], []).append(Tick(
            ticker=r["ticker"],
            captured_at=datetime.fromisoformat(r["captured_at"]),
            yes_bid=r["yes_bid"], yes_ask=r["yes_ask"],
            no_bid=r["no_bid"], no_ask=r["no_ask"],
            last_price=r["last_price"], volume=r["volume"],
        ))
    conn.close()
    return res_map, market_windows, ticks_by_ticker


def phase_for(when: datetime, start: datetime, end: datetime,
              late_cutoff: float = 0.67) -> MarketPhase:
    total = (end - start).total_seconds()
    if total <= 0:
        return MarketPhase.MIDDLE
    ratio = (when - start).total_seconds() / total
    if ratio < 0.34:
        return MarketPhase.EARLY
    if ratio < late_cutoff:
        return MarketPhase.MIDDLE
    return MarketPhase.LATE


def evaluate(label: str, *, delta: float, phase_filter: str | None,
             contrarian: bool, late_cutoff: float, plateau_threshold: float,
             plateau_seconds: int, res_map, market_windows, ticks_by_ticker) -> dict:
    engine = SignalEngine(SignalConfig(
        explosion_delta=delta,
        plateau_threshold=plateau_threshold,
        plateau_seconds=plateau_seconds,
        explosion_window_seconds=60,
    ))
    trades = []
    for ticker, ticks in ticks_by_ticker.items():
        if ticker not in res_map:
            continue
        if ticker not in market_windows:
            continue
        start, end = market_windows[ticker]
        engine.reset()
        for tick in ticks:
            phase = phase_for(tick.captured_at, start, end, late_cutoff)
            sig = engine.evaluate(tick, phase)
            if sig.kind == SignalKind.NONE:
                continue
            if phase_filter and phase.value != phase_filter:
                continue
            side = sig.side
            if contrarian:
                side = Side.NO if side == Side.YES else Side.YES
            entry = tick.yes_ask if side == Side.YES else tick.no_ask
            entry = entry or 0.5
            fees = kalshi_fee(entry)
            payoff = 1.0 if side.value == res_map[ticker] else 0.0
            pnl = payoff - entry - fees
            trades.append((side.value, entry, pnl, payoff > 0))
            break

    n = len(trades)
    wins = sum(1 for t in trades if t[3])
    win_rate = wins / n if n else 0
    lo, hi = wilson_ci(wins, n)
    total_pnl = sum(t[2] for t in trades)
    avg_pnl = total_pnl / n if n else 0
    avg_entry = sum(t[1] for t in trades) / n if n else 0
    return {
        "label": label,
        "n": n,
        "win_rate": win_rate,
        "ci": (lo, hi),
        "avg_entry": avg_entry,
        "avg_pnl": avg_pnl,
        "total_pnl": total_pnl,
    }


def main() -> int:
    settings = get_settings()
    print(f"Loading dataset from {settings.db_path}...")
    res_map, market_windows, ticks_by_ticker = load_dataset(settings.db_path)
    print(f"  resolutions: {len(res_map)} markets")
    print(f"  ticks: {sum(len(t) for t in ticks_by_ticker.values())}")
    print()

    results: list[dict] = []

    # A. Explosion sweep × phase × normal/contrarian
    for delta in (0.05, 0.10, 0.15, 0.20, 0.25):
        for phase in (None, "early", "middle", "late"):
            for contrarian in (False, True):
                tag = f"expl Δ={delta:.2f} phase={phase or 'any':>6} {'CONTRA' if contrarian else 'follow'}"
                results.append(evaluate(
                    tag, delta=delta, phase_filter=phase, contrarian=contrarian,
                    late_cutoff=0.67, plateau_threshold=0.99, plateau_seconds=99999,
                    res_map=res_map, market_windows=market_windows,
                    ticks_by_ticker=ticks_by_ticker,
                ))

    # B. Extreme-late explosion (last 10%/20%)
    for late_cut in (0.80, 0.90):
        for delta in (0.10, 0.15, 0.20):
            for contrarian in (False, True):
                tag = f"expl Δ={delta:.2f} late≥{late_cut:.2f} {'CONTRA' if contrarian else 'follow'}"
                results.append(evaluate(
                    tag, delta=delta, phase_filter="late", contrarian=contrarian,
                    late_cutoff=late_cut, plateau_threshold=0.99, plateau_seconds=99999,
                    res_map=res_map, market_windows=market_windows,
                    ticks_by_ticker=ticks_by_ticker,
                ))

    # C. Plateau alone (no explosion)
    for thr in (0.65, 0.70, 0.75, 0.80):
        for secs in (30, 60, 120):
            for phase in ("late", None):
                for contrarian in (False, True):
                    tag = f"plat ≥{thr:.2f} {secs}s phase={phase or 'any':>4} {'CONTRA' if contrarian else 'follow'}"
                    results.append(evaluate(
                        tag, delta=0.99, phase_filter=phase, contrarian=contrarian,
                        late_cutoff=0.67, plateau_threshold=thr, plateau_seconds=secs,
                        res_map=res_map, market_windows=market_windows,
                        ticks_by_ticker=ticks_by_ticker,
                    ))

    # Sort by avg_pnl (desc), require n >= 20
    eligible = [r for r in results if r["n"] >= 20]
    eligible.sort(key=lambda r: r["avg_pnl"], reverse=True)

    print("=== Top 25 variants (n>=20, sorted by avg PnL) ===")
    print(f"{'label':<60} {'n':>4} {'win%':>6} {'CI_lo':>6} "
          f"{'avg_entry':>10} {'avg_pnl':>8} {'total_pnl':>10}")
    print("-" * 110)
    for r in eligible[:25]:
        lo, hi = r["ci"]
        be = r["avg_entry"] + kalshi_fee(r["avg_entry"])
        mark = "✓" if lo > be else " "
        print(f"{r['label']:<60} {r['n']:>4} {r['win_rate']*100:>5.1f}% "
              f"{lo*100:>5.1f}% ${r['avg_entry']:>8.3f}  "
              f"${r['avg_pnl']:>+7.4f} ${r['total_pnl']:>+8.2f}  {mark}")

    print()
    print("=== Bottom 5 (worst) ===")
    for r in eligible[-5:]:
        print(f"{r['label']:<60} {r['n']:>4} {r['win_rate']*100:>5.1f}% "
              f"${r['avg_pnl']:>+7.4f}")

    # Recommend the best variant that has CI lower bound > breakeven
    print()
    print("=== Recommendation ===")
    winners = [r for r in eligible
               if r["ci"][0] > (r["avg_entry"] + kalshi_fee(r["avg_entry"]))]
    if winners:
        best = winners[0]
        print(f"  WINNER: {best['label']}")
        print(f"    n={best['n']}, win={best['win_rate']*100:.1f}% "
              f"(CI lo={best['ci'][0]*100:.1f}%), entry=${best['avg_entry']:.3f}, "
              f"avg PnL=${best['avg_pnl']:+.4f}, total=${best['total_pnl']:+.2f}")
    else:
        print("  NO VARIANT has lower-CI win rate above breakeven.")
        print("  → Auto-trading on this dataset is NOT justified by the data.")
        if eligible:
            best = eligible[0]
            print(f"  Best avg-PnL (not stat-significant): {best['label']} "
                  f"avg PnL=${best['avg_pnl']:+.4f}, n={best['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
