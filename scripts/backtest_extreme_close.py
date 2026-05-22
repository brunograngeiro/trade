"""Backtest the EXTREME_CLOSE signal in isolation, sweeping (prob, ttc) gates.

Uses the same SignalEngine + close_time parsing as the rest of the codebase,
so the result is directly comparable to live behavior.

Output: per-cell win rate, Wilson CI, avg PnL after fees, and a flag when CI
lower bound clears the breakeven win rate for the cell's avg entry.
"""

from __future__ import annotations

import math
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from backtest.engine import BacktestParams, kalshi_fee_dollars, run_backtest  # noqa: E402


def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.db_path}\n")

    prob_gates = [0.90, 0.92, 0.95]
    ttc_gates = [60, 120, 180, 240]
    persistence_gates = [0, 15, 30, 60, 90]

    print(f"{'prob':<5} {'ttc':<4} {'pers':<5} {'n':>4} {'win%':>6} {'CI_lo':>6} "
          f"{'avg_entry':>9} {'breakeven':>9} {'avg_pnl':>8} "
          f"{'total':>8}  edge?")
    print("-" * 86)

    best = None
    for prob_gate in prob_gates:
        for ttc_gate in ttc_gates:
            for persistence in persistence_gates:
                params = BacktestParams(
                    explosion_delta=0.99,
                    plateau_threshold=0.99,
                    plateau_seconds=99999,
                    extreme_close_prob=prob_gate,
                    extreme_close_ttc_seconds=float(ttc_gate),
                    extreme_close_persistence_seconds=float(persistence),
                    only_signal_kind="extreme_close",
                )
                result = run_backtest(settings.db_path, params)
                trades = [t for t in result["trades"] if t.get("pnl") is not None]
                n = len(trades)
                if n == 0:
                    print(f"{prob_gate:<5.2f} {ttc_gate:<4} {persistence:<5} "
                          f"{n:>4}  no trades")
                    continue
                # SIDE-CORRECT (not pnl>0). At high entries, pnl=0 is common
                # (entry+fee == payoff) but the side bet was correct.
                wins = sum(1 for t in trades if t["side"] == t["resolution"])
                lo, _hi = wilson_ci(wins, n)
                avg_entry = sum(t["entry_price"] for t in trades) / n
                avg_pnl = sum(t["pnl"] for t in trades) / n
                total = sum(t["pnl"] for t in trades)
                breakeven = avg_entry + kalshi_fee_dollars(avg_entry, 1)
                edge = "✓" if lo > breakeven else ""
                print(f"{prob_gate:<5.2f} {ttc_gate:<4} {persistence:<5} "
                      f"{n:>4} {wins/n*100:>5.1f}% {lo*100:>5.1f}% "
                      f"${avg_entry:>7.3f}  ${breakeven:>7.3f}  "
                      f"${avg_pnl:>+7.4f} ${total:>+7.2f}  {edge}")
                if n >= 30 and (best is None or avg_pnl > best["avg_pnl"]):
                    best = {"prob": prob_gate, "ttc": ttc_gate, "pers": persistence,
                            "n": n, "win": wins/n, "ci_lo": lo, "entry": avg_entry,
                            "be": breakeven, "avg_pnl": avg_pnl, "total": total}

    print()
    if best:
        print("=== Best by avg PnL (n>=30) ===")
        print(f"  prob>={best['prob']:.2f}  ttc<={best['ttc']}s  persistence>={best['pers']}s")
        print(f"  n={best['n']}, win={best['win']*100:.1f}% (CI_lo={best['ci_lo']*100:.1f}%), "
              f"entry=${best['entry']:.3f}, breakeven=${best['be']:.3f}")
        print(f"  avg PnL=${best['avg_pnl']:+.4f}, total=${best['total']:+.2f}")
        if best["ci_lo"] > best["be"]:
            print("  >>> STATISTICAL EDGE (CI_lo > breakeven) <<<")
        else:
            gap = (best["be"] - best["ci_lo"]) * 100
            print(f"  No edge: CI_lo is {gap:.1f}pp below breakeven")

    print()
    print("Notes:")
    print("  * Entry = side's ask at the first qualifying tick (worst-case fill).")
    print("  * Fee = ceil(0.07 * P * (1-P)) in cents; charged once at entry.")
    print("  * 'breakeven' = avg_entry + avg_fee; CI_lo > breakeven => stat edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
