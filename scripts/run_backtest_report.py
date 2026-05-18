"""Run backtest sweeps over historical data and print a digest."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from backtest.engine import BacktestParams, run_backtest  # noqa: E402


def fmt(x, suffix=""):
    return f"{x:.3f}{suffix}" if isinstance(x, (int, float)) and x is not None else "—"


def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.db_path}\n")

    explosion_grid = [0.10, 0.15, 0.20, 0.25]
    plateau_grid = [(0.60, 60), (0.60, 120), (0.65, 90), (0.70, 60), (0.75, 60)]
    phases = [None, "early", "middle", "late"]

    print("=== Explosion sweep (delta × phase) — fee-aware ===")
    header = f"{'delta':>6} {'phase':>7} {'trades':>7} {'settled':>8} {'win%':>6} " \
             f"{'avg_pnl':>8} {'total':>8}"
    print(header)
    print("-" * len(header))
    for delta in explosion_grid:
        for phase in phases:
            params = BacktestParams(
                explosion_delta=delta,
                explosion_window_seconds=60,
                plateau_threshold=0.99,  # disable plateau
                plateau_seconds=99999,
                min_phase=phase,
            )
            r = run_backtest(settings.db_path, params)
            wr = (r["win_rate"] * 100) if r["win_rate"] is not None else None
            print(f"{delta:>6.2f} {(phase or 'any'):>7} {r['trades_count']:>7} "
                  f"{r['settled_count']:>8} {fmt(wr):>6} "
                  f"{fmt(r['avg_pnl']):>8} {fmt(r['total_pnl']):>8}")

    print("\n=== Plateau sweep (threshold × seconds × phase) ===")
    print(header)
    print("-" * len(header))
    for (thr, sec) in plateau_grid:
        for phase in phases:
            params = BacktestParams(
                explosion_delta=0.99,  # disable explosion
                plateau_threshold=thr,
                plateau_seconds=sec,
                min_phase=phase,
            )
            r = run_backtest(settings.db_path, params)
            wr = (r["win_rate"] * 100) if r["win_rate"] is not None else None
            print(f"{thr:>6.2f} {(phase or 'any'):>7} {r['trades_count']:>7} "
                  f"{r['settled_count']:>8} {fmt(wr):>6} "
                  f"{fmt(r['avg_pnl']):>8} {fmt(r['total_pnl']):>8} "
                  f"(t={sec}s)")

    print("\n=== Combined default vs no-fee comparison ===")
    base = BacktestParams()
    r_with = run_backtest(settings.db_path, base)
    no_fee = BacktestParams(fee_rate=0.0)
    r_no = run_backtest(settings.db_path, no_fee)
    print(f"  With fees   : trades={r_with['trades_count']} settled={r_with['settled_count']} "
          f"win%={fmt((r_with['win_rate'] or 0)*100)} total={fmt(r_with['total_pnl'])}")
    print(f"  Without fees: trades={r_no['trades_count']} settled={r_no['settled_count']} "
          f"win%={fmt((r_no['win_rate'] or 0)*100)} total={fmt(r_no['total_pnl'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
