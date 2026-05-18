"""Statistical audit of trade2 historical data.

Answers: do we have enough data and signal quality to start auto-trading?
"""

from __future__ import annotations

import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.application.signals import SignalConfig, SignalEngine  # noqa: E402
from app.domain.entities import MarketPhase, Side, SignalKind, Tick  # noqa: E402
from backtest.engine import kalshi_fee_dollars  # noqa: E402


def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for win rate. Robust on small samples."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def main() -> int:
    settings = get_settings()
    db_path = settings.db_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"=== Data audit — {db_path} ===\n")

    total_ticks = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    distinct_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM ticks").fetchone()[0]
    earliest = conn.execute("SELECT MIN(captured_at) FROM ticks").fetchone()[0]
    latest = conn.execute("SELECT MAX(captured_at) FROM ticks").fetchone()[0]
    print(f"Ticks      : {total_ticks:,} across {distinct_tickers} markets")
    print(f"Range      : {earliest} → {latest}")
    try:
        spot_count = conn.execute("SELECT COUNT(*) FROM spot_ticks").fetchone()[0]
        print(f"Spot ticks : {spot_count:,}")
    except sqlite3.OperationalError:
        print("Spot ticks : table missing (will be created on next service restart)")
    resolutions = conn.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0]
    yes_n = conn.execute("SELECT COUNT(*) FROM market_resolutions WHERE result='yes'").fetchone()[0]
    print(f"Resolutions: {resolutions}  (YES={yes_n}, NO={resolutions-yes_n})")
    print()

    # Ticks per market and per phase (using min/max of each market window)
    market_windows = {}
    for row in conn.execute(
        "SELECT ticker, MIN(captured_at) AS s, MAX(captured_at) AS e, COUNT(*) AS n FROM ticks GROUP BY ticker"
    ):
        market_windows[row["ticker"]] = (datetime.fromisoformat(row["s"]),
                                         datetime.fromisoformat(row["e"]),
                                         row["n"])

    phase_counts = Counter()
    for ticker, (start, end, n) in market_windows.items():
        total = (end - start).total_seconds()
        if total <= 0:
            continue
        for row in conn.execute(
            "SELECT captured_at FROM ticks WHERE ticker=?", (ticker,)
        ):
            t = datetime.fromisoformat(row["captured_at"])
            ratio = (t - start).total_seconds() / total
            if ratio < 0.34:
                phase_counts["early"] += 1
            elif ratio < 0.67:
                phase_counts["middle"] += 1
            else:
                phase_counts["late"] += 1
    total_phase = sum(phase_counts.values())
    print("Tick distribution by phase:")
    for ph in ("early", "middle", "late"):
        n = phase_counts.get(ph, 0)
        pct = (n / total_phase * 100) if total_phase else 0
        print(f"  {ph:>6}: {n:>7,} ({pct:5.1f}%)")
    print()

    # Simulate late-phase explosion strategy on resolved markets
    res_map = {r["ticker"]: r["result"] for r in
               conn.execute("SELECT ticker, result FROM market_resolutions")}
    delta = settings.prob_explosion_delta
    engine = SignalEngine(SignalConfig(
        explosion_delta=delta,
        plateau_threshold=0.99,
        plateau_seconds=99999,
    ))

    trades = []
    for ticker, (start, end, n) in market_windows.items():
        if ticker not in res_map:
            continue
        total = (end - start).total_seconds()
        if total <= 0:
            continue
        engine.reset()
        rows = conn.execute(
            "SELECT * FROM ticks WHERE ticker=? ORDER BY captured_at ASC", (ticker,)
        ).fetchall()
        for r in rows:
            t = datetime.fromisoformat(r["captured_at"])
            ratio = (t - start).total_seconds() / total
            phase = MarketPhase.LATE if ratio >= 0.67 else (
                MarketPhase.MIDDLE if ratio >= 0.34 else MarketPhase.EARLY
            )
            tick = Tick(ticker=ticker, captured_at=t,
                        yes_bid=r["yes_bid"], yes_ask=r["yes_ask"],
                        no_bid=r["no_bid"], no_ask=r["no_ask"],
                        last_price=r["last_price"], volume=r["volume"])
            sig = engine.evaluate(tick, phase)
            if sig.kind != SignalKind.EXPLOSION:
                continue
            if phase != MarketPhase.LATE:
                continue
            # entry
            entry = tick.yes_ask if sig.side == Side.YES else tick.no_ask
            entry = entry or 0.5
            fees = kalshi_fee_dollars(entry, 1)
            payoff = 1.0 if sig.side.value == res_map[ticker] else 0.0
            pnl = payoff - entry - fees
            trades.append({"ticker": ticker, "side": sig.side.value,
                           "entry": entry, "pnl": pnl, "win": pnl > 0})
            break

    wins = sum(1 for t in trades if t["win"])
    n = len(trades)
    win_rate = wins / n if n else 0
    lo, hi = wilson_ci(wins, n) if n else (0, 0)
    total_pnl = sum(t["pnl"] for t in trades)
    avg_pnl = total_pnl / n if n else 0
    avg_entry = sum(t["entry"] for t in trades) / n if n else 0

    print("=== Strategy: late-explosion-only ===")
    print(f"Δ={delta:.2f}, phase=late, fee=ceil(0.07·C·P·(1-P))")
    print(f"  Settled trades : {n}")
    print(f"  Win rate       : {win_rate*100:.1f}% (95% CI [{lo*100:.1f}%, {hi*100:.1f}%])")
    print(f"  Avg entry      : ${avg_entry:.3f}")
    print(f"  Avg PnL/trade  : ${avg_pnl:+.4f}")
    print(f"  Total PnL      : ${total_pnl:+.3f}")
    print()

    # Breakeven analysis
    if n > 0:
        be = avg_entry + kalshi_fee_dollars(avg_entry, 1)
        print(f"  Avg breakeven win rate needed: {be*100:.1f}%")
        print(f"  Lower-CI win rate vs breakeven: "
              f"{lo*100:.1f}% {'✓ ABOVE' if lo > be else '✗ BELOW'} {be*100:.1f}%")
        print()

    # Recommendation
    print("=== Recommendation ===")
    if n < 20:
        print("  NOT ENOUGH SAMPLES (n<20). Keep collecting; do NOT enable auto-trading.")
    elif lo < (avg_entry + 0.02):
        print("  BORDERLINE. CI lower bound near breakeven; enable only with tight risk caps.")
    else:
        print("  STATISTICALLY SUPPORTED. Auto-trading viable with current 90¢ cap.")
        print(f"  Expected per-trade after fees ≈ ${avg_pnl:+.3f}")
        print("  At ~2-4 late-explosion signals/day → "
              f"~${avg_pnl*3*30:.2f} EV/month before slippage.")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
