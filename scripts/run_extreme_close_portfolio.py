"""Capital-aware simulation of the EXTREME_CLOSE strategy.

Answers three operational questions:
  1) With patrimony compounding (each PnL updates capital for the next trade),
     does the strategy show a meaningful equity curve?
  2) Do the existing risk controls (cooldown, daily cap, kill-switch) make
     things better or worse?
  3) Can splitting capital into "blocks" reduce variance (e.g. only $10 at risk
     per day, fixed contracts, no scaling)?
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from backtest.portfolio_sim import SimParams, run_simulation  # noqa: E402


COMMON = dict(
    explosion_delta=0.99,
    plateau_threshold=0.99,
    plateau_seconds=99999,
    extreme_close_prob=0.95,
    extreme_close_ttc_seconds=60.0,
    extreme_close_persistence_seconds=60.0,
    only_signal_kind="extreme_close",
    min_phase=None,  # extreme_close handles its own TTC gate; don't double-filter
    contrarian=False,
    initial_capital_dollars=100.0,
    fee_rate=0.07,
)


REGIMES = [
    # (label, overrides)
    ("no-risk-mgmt (raw)",       dict(cooldown_seconds=0, max_trades_per_day=9999,
                                       kill_after_consecutive_losses=9999,
                                       sizing_mode="fixed", fixed_contracts=1)),
    ("default risk controls",    dict(cooldown_seconds=300, max_trades_per_day=6,
                                       kill_after_consecutive_losses=5,
                                       sizing_mode="fixed", fixed_contracts=1)),
    ("block A: $10/day cap",     dict(cooldown_seconds=0, max_trades_per_day=10,
                                       kill_after_consecutive_losses=9999,
                                       sizing_mode="fixed", fixed_contracts=1)),
    ("block B: 2 contracts",     dict(cooldown_seconds=300, max_trades_per_day=6,
                                       kill_after_consecutive_losses=5,
                                       sizing_mode="fixed", fixed_contracts=2)),
    ("block C: 1% fraction",     dict(cooldown_seconds=300, max_trades_per_day=6,
                                       kill_after_consecutive_losses=5,
                                       sizing_mode="fraction", capital_fraction=0.01)),
    ("block D: 5% fraction",     dict(cooldown_seconds=300, max_trades_per_day=6,
                                       kill_after_consecutive_losses=5,
                                       sizing_mode="fraction", capital_fraction=0.05)),
]


def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.db_path}")
    print(f"Strategy: EXTREME_CLOSE prob>=0.95, ttc<=60s, persistence>=60s")
    print(f"Initial capital: $100\n")

    print(f"{'regime':<26} {'n':>4} {'win%':>6} {'final':>9} {'pnl':>9} "
          f"{'pct':>7} {'maxDD':>7} {'killed':>10}")
    print("-" * 82)

    for label, overrides in REGIMES:
        params = SimParams(**{**COMMON, **overrides})
        r = run_simulation(settings.db_path, params)
        killed = r.killed_at[:10] if r.killed_at else "—"
        win_pct = f"{r.win_rate*100:.1f}%" if r.win_rate is not None else "n/a"
        print(f"{label:<26} {r.trades_total:>4} {win_pct:>6} "
              f"${r.final_capital:>7.2f} ${r.total_pnl:>+7.2f} "
              f"{r.pct_return:>+6.2f}% {r.max_drawdown_pct:>6.2f}% "
              f"{killed:>10}")

    print()
    print("Note: 'win%' here = pnl>0 (NOT side-correct). At ~$0.99 entries,")
    print("most correct trades land at pnl=$0 (exactly breakeven after fees).")
    print("Side-correct rate is ~100% on this gate but the fee structure")
    print("makes the strategy near-zero EV regardless of risk regime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
