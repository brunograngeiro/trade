"""Capital-aware backtest simulator — answers "what would my balance be?".

Differences from `backtest/engine.run_backtest`:
  - Tracks **running capital** trade by trade (compounding)
  - **Position sizing**: fixed contracts OR percentage of capital
  - **Cooldown** between trades (prevents over-trading same window)
  - **Max drawdown** tracking
  - **Daily trade cap** + kill-switch on consecutive losses
  - Outputs **equity curve** time series
  - Skips trades that would exceed available capital
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from app.application.signals import SignalConfig, SignalEngine
from app.domain.entities import MarketPhase, Side, SignalKind, Tick


def kalshi_fee(price: float, count: int = 1, rate: float = 0.07) -> float:
    p = max(0.01, min(0.99, price))
    return math.ceil(rate * count * p * (1 - p) * 100) / 100.0


@dataclass
class SimParams:
    # signal
    explosion_delta: float = 0.20
    explosion_window_seconds: int = 60
    plateau_threshold: float = 0.99
    plateau_seconds: int = 99999
    min_phase: str | None = "late"
    contrarian: bool = False  # if True, fade the signal side

    # capital management
    initial_capital_dollars: float = 100.0
    sizing_mode: str = "fixed"  # "fixed" | "fraction" | "kelly_fraction"
    fixed_contracts: int = 1
    capital_fraction: float = 0.05  # used when sizing_mode="fraction"
    max_contracts_per_trade: int = 50

    # risk controls
    cooldown_seconds: int = 300       # min seconds between trades
    max_trades_per_day: int = 6
    kill_after_consecutive_losses: int = 5
    fee_rate: float = 0.07


@dataclass
class SimTrade:
    ticker: str
    entered_at: str
    side: str
    phase: str
    entry_price: float
    count: int
    cost: float
    fees: float
    resolution: str | None
    payoff: float | None
    pnl: float | None
    capital_before: float
    capital_after: float
    notes: str


@dataclass
class SimResult:
    params: dict
    initial_capital: float
    final_capital: float
    total_pnl: float
    pct_return: float
    trades_total: int
    trades_settled: int
    wins: int
    losses: int
    win_rate: float | None
    max_drawdown_pct: float
    sharpe_proxy: float | None
    killed_at: str | None
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)


def _phase_for(when: datetime, start: datetime, end: datetime) -> MarketPhase:
    total = (end - start).total_seconds()
    if total <= 0:
        return MarketPhase.MIDDLE
    ratio = (when - start).total_seconds() / total
    if ratio < 0.34:
        return MarketPhase.EARLY
    if ratio < 0.67:
        return MarketPhase.MIDDLE
    return MarketPhase.LATE


def _size_contracts(params: SimParams, capital: float, entry_price: float,
                    win_rate_estimate: float = 0.5) -> int:
    """Decide # of contracts based on sizing mode."""
    if params.sizing_mode == "fixed":
        n = params.fixed_contracts
    elif params.sizing_mode == "fraction":
        budget = capital * params.capital_fraction
        n = int(budget // entry_price)
    elif params.sizing_mode == "kelly_fraction":
        # half-Kelly assuming win_rate_estimate; binary 1.00/0.00 payoff
        b = (1 - entry_price) / entry_price if entry_price > 0 else 1
        p = win_rate_estimate
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        f = max(0.0, min(0.10, kelly / 2))  # cap at 10% with safety
        budget = capital * f
        n = int(budget // entry_price)
    else:
        n = params.fixed_contracts
    n = max(0, min(params.max_contracts_per_trade, n))
    return n


def run_simulation(db_path: str, params: SimParams) -> SimResult:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    res_map = {r["ticker"]: r["result"] for r in
               conn.execute("SELECT ticker, result FROM market_resolutions")}
    market_windows = {}
    for row in conn.execute(
        "SELECT ticker, MIN(captured_at) AS s, MAX(captured_at) AS e FROM ticks GROUP BY ticker"
    ):
        market_windows[row["ticker"]] = (datetime.fromisoformat(row["s"]),
                                         datetime.fromisoformat(row["e"]))

    # collect candidate trades chronologically across all markets
    candidates: list[tuple[datetime, str, Tick, MarketPhase, Side]] = []
    engine = SignalEngine(SignalConfig(
        explosion_delta=params.explosion_delta,
        plateau_threshold=params.plateau_threshold,
        plateau_seconds=params.plateau_seconds,
        explosion_window_seconds=params.explosion_window_seconds,
    ))

    for row in conn.execute(
        "SELECT DISTINCT ticker FROM ticks ORDER BY ticker"
    ):
        ticker = row["ticker"]
        if ticker not in res_map or ticker not in market_windows:
            continue
        start, end = market_windows[ticker]
        engine.reset()
        for r in conn.execute(
            "SELECT * FROM ticks WHERE ticker=? ORDER BY captured_at ASC", (ticker,)
        ):
            t = datetime.fromisoformat(r["captured_at"])
            phase = _phase_for(t, start, end)
            tick = Tick(ticker=ticker, captured_at=t,
                        yes_bid=r["yes_bid"], yes_ask=r["yes_ask"],
                        no_bid=r["no_bid"], no_ask=r["no_ask"],
                        last_price=r["last_price"], volume=r["volume"])
            sig = engine.evaluate(tick, phase)
            if sig.kind == SignalKind.NONE:
                continue
            if params.min_phase and phase.value != params.min_phase:
                continue
            side = sig.side
            if params.contrarian:
                side = Side.NO if side == Side.YES else Side.YES
            candidates.append((t, ticker, tick, phase, side))
            break  # one trade attempt per ticker

    conn.close()

    candidates.sort(key=lambda x: x[0])

    capital = params.initial_capital_dollars
    peak = capital
    max_dd = 0.0
    last_trade_at: datetime | None = None
    trades_today: dict[str, int] = defaultdict(int)
    consecutive_losses = 0
    killed_at: str | None = None
    equity_curve: list[tuple[str, float]] = [(candidates[0][0].isoformat() if candidates else
                                              datetime.utcnow().isoformat(), capital)]
    daily_returns: list[float] = []
    sim_trades: list[SimTrade] = []
    wins = losses = settled = 0

    # use historical fee-aware win-rate as Kelly prior (rough)
    win_rate_prior = 0.5

    for when, ticker, tick, phase, side in candidates:
        if killed_at:
            break
        # cooldown
        if last_trade_at and (when - last_trade_at).total_seconds() < params.cooldown_seconds:
            continue
        # daily cap
        day = when.date().isoformat()
        if trades_today[day] >= params.max_trades_per_day:
            continue

        # pricing
        entry_price = tick.yes_ask if side == Side.YES else tick.no_ask
        entry_price = entry_price or 0.5
        n = _size_contracts(params, capital, entry_price, win_rate_prior)
        if n < 1:
            continue
        fees = kalshi_fee(entry_price, n, params.fee_rate)
        cost = entry_price * n + fees
        if cost > capital:
            continue

        capital_before = capital
        capital -= cost  # spend now
        resolution = res_map.get(ticker)
        payoff = None
        pnl = None
        if resolution in {"yes", "no"}:
            won = side.value == resolution
            payoff = n * (1.0 if won else 0.0)
            capital += payoff
            pnl = round(payoff - cost, 4)
            settled += 1
            if pnl > 0:
                wins += 1
                consecutive_losses = 0
            else:
                losses += 1
                consecutive_losses += 1
                if consecutive_losses >= params.kill_after_consecutive_losses:
                    killed_at = when.isoformat()

        # update equity tracking
        peak = max(peak, capital)
        dd = (peak - capital) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        equity_curve.append((when.isoformat(), round(capital, 4)))
        if pnl is not None:
            daily_returns.append(pnl / capital_before if capital_before > 0 else 0)

        last_trade_at = when
        trades_today[day] += 1

        sim_trades.append(SimTrade(
            ticker=ticker,
            entered_at=when.isoformat(),
            side=side.value,
            phase=phase.value,
            entry_price=round(entry_price, 4),
            count=n,
            cost=round(cost, 4),
            fees=round(fees, 4),
            resolution=resolution,
            payoff=round(payoff, 4) if payoff is not None else None,
            pnl=pnl,
            capital_before=round(capital_before, 4),
            capital_after=round(capital, 4),
            notes=f"phase={phase.value} side={side.value}",
        ))

    sharpe = None
    if len(daily_returns) > 5:
        mu = sum(daily_returns) / len(daily_returns)
        var = sum((r - mu) ** 2 for r in daily_returns) / len(daily_returns)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mu / sd * math.sqrt(252)) if sd > 0 else None

    return SimResult(
        params=asdict(params),
        initial_capital=round(params.initial_capital_dollars, 4),
        final_capital=round(capital, 4),
        total_pnl=round(capital - params.initial_capital_dollars, 4),
        pct_return=round((capital - params.initial_capital_dollars) / params.initial_capital_dollars * 100, 3),
        trades_total=len(sim_trades),
        trades_settled=settled,
        wins=wins,
        losses=losses,
        win_rate=round(wins / settled, 4) if settled else None,
        max_drawdown_pct=round(max_dd * 100, 3),
        sharpe_proxy=round(sharpe, 3) if sharpe is not None else None,
        killed_at=killed_at,
        equity_curve=equity_curve,
        trades=[asdict(t) for t in sim_trades],
    )
