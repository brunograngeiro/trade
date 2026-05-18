"""Compare several strategy variants under realistic capital management."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from backtest.portfolio_sim import SimParams, run_simulation  # noqa: E402


VARIANTS = [
    ("late Δ=0.20 follow (DEFAULT)",
     SimParams(explosion_delta=0.20, min_phase="late", contrarian=False)),
    ("late Δ=0.10 follow",
     SimParams(explosion_delta=0.10, min_phase="late", contrarian=False)),
    ("late Δ=0.25 follow",
     SimParams(explosion_delta=0.25, min_phase="late", contrarian=False)),
    ("early Δ=0.20 CONTRA (fade)",
     SimParams(explosion_delta=0.20, min_phase="early", contrarian=True)),
    ("any Δ=0.10 CONTRA",
     SimParams(explosion_delta=0.10, min_phase=None, contrarian=True)),
    ("plateau ≥0.80 120s late",
     SimParams(explosion_delta=0.99, plateau_threshold=0.80,
               plateau_seconds=120, min_phase="late", contrarian=False)),
    ("late Δ=0.20 follow + fraction sizing (5%)",
     SimParams(explosion_delta=0.20, min_phase="late",
               sizing_mode="fraction", capital_fraction=0.05)),
]


def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.db_path}")
    print(f"{'Variant':<46} {'Final':>9} {'Δ%':>7} {'Trades':>7} "
          f"{'Win%':>6} {'MaxDD%':>7} {'Sharpe':>7} {'Killed':>14}")
    print("-" * 112)
    for label, params in VARIANTS:
        r = run_simulation(settings.db_path, params)
        killed = (r.killed_at[:16] if r.killed_at else "—")
        wr = f"{r.win_rate*100:.1f}" if r.win_rate is not None else "—"
        sh = f"{r.sharpe_proxy}" if r.sharpe_proxy is not None else "—"
        print(f"{label:<46} ${r.final_capital:>7.2f} "
              f"{r.pct_return:>+6.2f}% {r.trades_total:>7} "
              f"{wr:>5}% {r.max_drawdown_pct:>6.2f}% {sh:>7} {killed:>14}")
    print()
    print("All start from $100 initial capital, 1ct fixed sizing, "
          "cooldown=5min, daily cap=6, kill after 5 losses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
