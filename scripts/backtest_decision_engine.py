"""Backtest the full Signal -> Decision pipeline over the historical dataset.

Unlike `backtest_extreme_close.py`, this drives ticks through the same
`DecisionEngine` that runs in production — including TTC gates, persistence
floor, spot guard, EXIT on confidence drop, FLIP on 50% cross, and price caps.

For each (signal + decision) tick we simulate execution:
  - ENTER  → open position at decision.limit_price_cents (taker yes_ask/no_ask)
  - EXIT   → close at decision.close_limit_price_cents (taker yes_bid/no_bid)
  - FLIP   → close old + open new on the same tick
  - market end (no exit fired) → settle at resolution (1.00 or 0.00)

PnL accounting follows Kalshi mechanics: entry pays fee = ceil(0.07*P*(1-P)),
no fee on close or settlement.

Output: per-reason counts, side-correct rate, equity-style PnL totals, and a
list of the largest losers/winners for spot-check.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.decision import DecisionAction, DecisionConfig, DecisionEngine  # noqa: E402
from app.application.signals import SignalConfig, SignalEngine  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain.entities import Market, MarketPhase, Side, Tick  # noqa: E402
from backtest.engine import close_time_from_ticker, kalshi_fee_dollars  # noqa: E402


def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


@dataclass
class SimTrade:
    ticker: str
    entered_at: str
    closed_at: str
    side: str
    entry_price: float
    exit_price: float
    fee: float
    pnl: float
    closure: str  # "settled_win" | "settled_loss" | "exit" | "flip_close"
    enter_reason: str
    close_reason: str


def _phase_from_window(when: datetime, open_t: datetime, close_t: datetime) -> MarketPhase:
    total = (close_t - open_t).total_seconds()
    if total <= 0:
        return MarketPhase.MIDDLE
    ratio = (when - open_t).total_seconds() / total
    if ratio < 0.34:
        return MarketPhase.EARLY
    if ratio < 0.67:
        return MarketPhase.MIDDLE
    return MarketPhase.LATE


def _spot_at(when: datetime, spot_times: list[datetime],
             spot_prices: list[float]) -> float | None:
    if not spot_times:
        return None
    idx = bisect_right(spot_times, when) - 1
    if idx < 0:
        return None
    return spot_prices[idx]


def _tick_from_row(row: sqlite3.Row) -> Tick:
    return Tick(
        ticker=row["ticker"],
        captured_at=datetime.fromisoformat(row["captured_at"]),
        yes_bid=row["yes_bid"],
        yes_ask=row["yes_ask"],
        no_bid=row["no_bid"],
        no_ask=row["no_ask"],
        last_price=row["last_price"],
        volume=row["volume"],
    )


def run(db_path: str, signal_cfg: SignalConfig,
        decision_cfg: DecisionConfig) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    resolutions = {r["ticker"]: r["result"] for r in
                   conn.execute("SELECT ticker, result FROM market_resolutions")}

    spot_rows = conn.execute(
        "SELECT captured_at, price FROM spot_ticks ORDER BY captured_at"
    ).fetchall()
    spot_times = [datetime.fromisoformat(r["captured_at"]).replace(tzinfo=None)
                  for r in spot_rows]
    spot_prices = [float(r["price"]) for r in spot_rows]

    tickers = [r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM ticks ORDER BY ticker"
    )]

    sig_engine = SignalEngine(signal_cfg)
    dec_engine = DecisionEngine(decision_cfg)

    trades: list[SimTrade] = []
    reason_counts: dict[str, int] = defaultdict(int)
    action_counts: dict[str, int] = defaultdict(int)
    skips_by_reason: dict[str, int] = defaultdict(int)

    for ticker in tickers:
        close_t_naive = close_time_from_ticker(ticker)
        if close_t_naive is None or resolutions.get(ticker) not in ("yes", "no"):
            continue
        # Decision engine compares against tz-aware tick.captured_at; align.
        close_t_aware = close_t_naive.replace(tzinfo=timezone.utc)
        open_t_aware = close_t_aware - timedelta(minutes=15)
        market = Market(
            ticker=ticker, title="",
            open_time=open_t_aware, close_time=close_t_aware,
            yes_bid=None, yes_ask=None, no_bid=None, no_ask=None,
            last_price=None, status="resolved",
        )
        close_t = close_t_naive  # keep naive copy for ttc arithmetic below

        sig_engine.reset()
        dec_engine.reset()

        # state per market
        pos_side: Side | None = None
        pos_entry_price: float | None = None
        pos_fee: float | None = None
        pos_entered_at: datetime | None = None
        pos_enter_reason: str | None = None

        last_tick_row = None
        for row in conn.execute(
            "SELECT * FROM ticks WHERE ticker=? ORDER BY captured_at ASC", (ticker,)
        ):
            last_tick_row = row
            tick = _tick_from_row(row)
            when = tick.captured_at.replace(tzinfo=None)
            ttc = (close_t - when).total_seconds()
            if ttc < 0:
                continue
            phase = _phase_from_window(when, close_t - timedelta(minutes=15), close_t)
            spot = _spot_at(when, spot_times, spot_prices)

            signal = sig_engine.evaluate(tick, phase, ttc_seconds=ttc)
            decision = dec_engine.evaluate(
                tick, market, signal, ttc,
                spot_price=spot, spot_captured_at=tick.captured_at,
            )
            action_counts[decision.action.value] += 1
            if decision.action == DecisionAction.SKIP:
                skips_by_reason[decision.reason] += 1
                continue
            reason_counts[decision.reason] += 1

            if (decision.action == DecisionAction.ENTER
                    and pos_side is None
                    and decision.side is not None
                    and decision.limit_price_cents is not None):
                pos_side = decision.side
                pos_entry_price = decision.limit_price_cents / 100.0
                pos_fee = kalshi_fee_dollars(pos_entry_price, 1)
                pos_entered_at = tick.captured_at
                pos_enter_reason = decision.reason

            elif (decision.action == DecisionAction.EXIT
                    and pos_side is not None
                    and decision.close_limit_price_cents is not None
                    and pos_entry_price is not None
                    and pos_fee is not None
                    and pos_entered_at is not None):
                exit_price = decision.close_limit_price_cents / 100.0
                pnl = exit_price - pos_entry_price - pos_fee
                trades.append(SimTrade(
                    ticker=ticker,
                    entered_at=pos_entered_at.isoformat(),
                    closed_at=tick.captured_at.isoformat(),
                    side=pos_side.value,
                    entry_price=round(pos_entry_price, 4),
                    exit_price=round(exit_price, 4),
                    fee=round(pos_fee, 4),
                    pnl=round(pnl, 4),
                    closure="exit",
                    enter_reason=pos_enter_reason or "",
                    close_reason=decision.reason,
                ))
                pos_side = pos_entry_price = pos_fee = pos_entered_at = None
                pos_enter_reason = None

            elif (decision.action == DecisionAction.FLIP
                    and pos_side is not None
                    and decision.close_limit_price_cents is not None
                    and decision.side is not None
                    and decision.limit_price_cents is not None
                    and pos_entry_price is not None
                    and pos_fee is not None
                    and pos_entered_at is not None):
                # Close the old leg.
                exit_price = decision.close_limit_price_cents / 100.0
                pnl = exit_price - pos_entry_price - pos_fee
                trades.append(SimTrade(
                    ticker=ticker,
                    entered_at=pos_entered_at.isoformat(),
                    closed_at=tick.captured_at.isoformat(),
                    side=pos_side.value,
                    entry_price=round(pos_entry_price, 4),
                    exit_price=round(exit_price, 4),
                    fee=round(pos_fee, 4),
                    pnl=round(pnl, 4),
                    closure="flip_close",
                    enter_reason=pos_enter_reason or "",
                    close_reason=decision.reason,
                ))
                # Open the new leg on the same tick.
                pos_side = decision.side
                pos_entry_price = decision.limit_price_cents / 100.0
                pos_fee = kalshi_fee_dollars(pos_entry_price, 1)
                pos_entered_at = tick.captured_at
                pos_enter_reason = decision.reason

        # End-of-market: if a position is still open, settle at resolution.
        if pos_side is not None and pos_entry_price is not None and pos_fee is not None:
            res = resolutions[ticker]
            won = (pos_side.value == res)
            payoff = 1.0 if won else 0.0
            pnl = payoff - pos_entry_price - pos_fee
            closed_at_iso = (last_tick_row["captured_at"] if last_tick_row
                             else (pos_entered_at.isoformat() if pos_entered_at else ""))
            trades.append(SimTrade(
                ticker=ticker,
                entered_at=pos_entered_at.isoformat() if pos_entered_at else "",
                closed_at=closed_at_iso,
                side=pos_side.value,
                entry_price=round(pos_entry_price, 4),
                exit_price=payoff,
                fee=round(pos_fee, 4),
                pnl=round(pnl, 4),
                closure="settled_win" if won else "settled_loss",
                enter_reason=pos_enter_reason or "",
                close_reason=f"resolution={res}",
            ))

    conn.close()
    return {
        "trades": trades,
        "action_counts": dict(action_counts),
        "enter_reasons": dict(reason_counts),
        "skip_reasons": dict(skips_by_reason),
    }


def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.db_path}\n")

    # Use the production defaults straight from settings — this is what
    # would actually run if we flipped ENABLE_REAL_ORDERS=true.
    signal_cfg = SignalConfig(
        explosion_delta=settings.prob_explosion_delta,
        plateau_threshold=settings.prob_plateau_threshold,
        plateau_seconds=settings.prob_plateau_seconds,
        extreme_close_prob=settings.extreme_close_prob,
        extreme_close_ttc_seconds=settings.extreme_close_ttc_seconds,
        extreme_close_persistence_seconds=settings.extreme_close_persistence_seconds,
    )
    decision_cfg = DecisionConfig(
        enabled=True,
        entry_confidence_floor=settings.entry_confidence_floor,
        entry_persistence_seconds=settings.entry_persistence_seconds,
        entry_ttc_seconds=settings.entry_ttc_seconds,
        late_cross_ttc_seconds=settings.late_cross_ttc_seconds,
        flip_cross_ttc_seconds=settings.flip_cross_ttc_seconds,
        extreme_close_ttc_seconds=settings.extreme_close_ttc_seconds,
        max_entry_price_180s_cents=settings.max_entry_price_180s_cents,
        max_entry_price_60s_cents=settings.max_entry_price_60s_cents,
        max_entry_price_30s_cents=settings.max_entry_price_30s_cents,
        spot_guard_enabled=settings.spot_guard_enabled,
        spot_guard_buffer_dollars=settings.spot_guard_buffer_dollars,
        spot_guard_momentum_seconds=settings.spot_guard_momentum_seconds,
        spot_guard_momentum_dollars=settings.spot_guard_momentum_dollars,
    )

    result = run(settings.db_path, signal_cfg, decision_cfg)
    trades = result["trades"]

    print("=== Action counts (entire dataset, includes warming/skip noise) ===")
    for action, n in sorted(result["action_counts"].items(),
                            key=lambda kv: -kv[1]):
        print(f"  {action:<8} {n:>8}")
    print()

    print("=== Top SKIP reasons (most common) ===")
    for reason, n in sorted(result["skip_reasons"].items(),
                            key=lambda kv: -kv[1])[:10]:
        print(f"  {n:>6}  {reason}")
    print()

    print("=== ENTER/EXIT/FLIP reasons ===")
    for reason, n in sorted(result["enter_reasons"].items(),
                            key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {reason}")
    print()

    if not trades:
        print("No closed trades — DecisionEngine is too restrictive on this dataset.")
        print("Likely culprits: max_entry_price_* caps too tight, spot guard too "
              "aggressive, or persistence floor never satisfied.")
        return 0

    n = len(trades)
    side_correct = sum(1 for t in trades if t.closure in ("settled_win",) or t.pnl > 0)
    pos_pnl = sum(1 for t in trades if t.pnl > 0)
    zero_pnl = sum(1 for t in trades if t.pnl == 0)
    neg_pnl = sum(1 for t in trades if t.pnl < 0)
    total_pnl = sum(t.pnl for t in trades)
    avg_entry = sum(t.entry_price for t in trades) / n
    lo, hi = wilson_ci(pos_pnl, n)

    print(f"=== Settled trades: {n} ===")
    print(f"  pnl > 0:   {pos_pnl} ({pos_pnl/n*100:.1f}%)  "
          f"Wilson CI: [{lo*100:.1f}%, {hi*100:.1f}%]")
    print(f"  pnl = 0:   {zero_pnl}")
    print(f"  pnl < 0:   {neg_pnl}")
    print(f"  avg entry: ${avg_entry:.3f}")
    print(f"  avg PnL:   ${total_pnl/n:+.4f}")
    print(f"  total PnL: ${total_pnl:+.2f}")
    print()

    by_closure: dict[str, list[SimTrade]] = defaultdict(list)
    for t in trades:
        by_closure[t.closure].append(t)
    print("=== PnL by closure type ===")
    for closure, ts in sorted(by_closure.items()):
        win_ct = sum(1 for t in ts if t.pnl > 0)
        avg = sum(t.pnl for t in ts) / len(ts) if ts else 0.0
        total = sum(t.pnl for t in ts)
        print(f"  {closure:<14} n={len(ts):>3}  win={win_ct/len(ts)*100:>5.1f}%  "
              f"avg=${avg:+.4f}  total=${total:+.2f}")
    print()

    losers = sorted([t for t in trades if t.pnl < 0], key=lambda t: t.pnl)[:5]
    if losers:
        print("=== Worst 5 losers ===")
        for t in losers:
            print(f"  {t.ticker:<28} side={t.side:<3} "
                  f"entry=${t.entry_price:.3f} exit=${t.exit_price:.3f} "
                  f"pnl=${t.pnl:+.3f} closure={t.closure}")
            print(f"    enter: {t.enter_reason}")
            print(f"    close: {t.close_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
