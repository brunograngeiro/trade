"""Simple backtest + walk-forward over persisted ticks.

The strategy mirrors the live signal engine:
  - explosion: probability moves >= delta within explosion_window_seconds
  - plateau: probability sustained >= threshold for plateau_seconds

PnL is settled from market_resolutions (1.00 if winner, 0.00 if loser).
Entry price uses the side's ask (worst-case cross-the-book).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean
from typing import Iterable

from app.application.signals import SignalConfig, SignalEngine
from app.domain.entities import Side, SignalKind, Tick


@dataclass
class BacktestParams:
    explosion_delta: float = 0.15
    plateau_threshold: float = 0.60
    plateau_seconds: int = 120
    explosion_window_seconds: int = 60
    # Kalshi BTC fee table: fees = ceil(0.07 * C * P * (1-P)) in dollars
    fee_rate: float = 0.07
    apply_fees_both_sides: bool = True  # exit/expiry also charges
    min_phase: str | None = None  # "early"|"middle"|"late" to restrict entries
    slippage_cents: int = 0  # extra cents added to entry price


def kalshi_fee_dollars(price_dollars: float, count: int, rate: float = 0.07) -> float:
    """Kalshi tapered fee: ceil(rate * C * P * (1-P))."""
    import math
    p = max(0.01, min(0.99, price_dollars))
    raw = rate * count * p * (1 - p)
    return math.ceil(raw * 100) / 100.0


@dataclass
class Trade:
    ticker: str
    entered_at: str
    side: str
    phase: str
    entry_price: float
    resolution: str | None
    payoff: float | None
    pnl: float | None
    signal_kind: str
    notes: str


def _tick_from_row(row: sqlite3.Row | dict) -> Tick:
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


def run_backtest(db_path: str, params: BacktestParams) -> dict:
    """Run a single backtest pass over all ticks; one entry per ticker."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ticks ORDER BY ticker ASC, captured_at ASC").fetchall()
    resolutions = {r["ticker"]: r["result"]
                   for r in conn.execute("SELECT * FROM market_resolutions").fetchall()}
    markets_meta = _market_windows(conn)
    conn.close()

    by_ticker: dict[str, list[Tick]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(_tick_from_row(r))

    trades: list[Trade] = []
    engine = SignalEngine(SignalConfig(
        explosion_delta=params.explosion_delta,
        plateau_threshold=params.plateau_threshold,
        plateau_seconds=params.plateau_seconds,
        explosion_window_seconds=params.explosion_window_seconds,
    ))

    for ticker, ticks in by_ticker.items():
        engine.reset()
        window = markets_meta.get(ticker)
        for tick in ticks:
            phase = _phase_for(window, tick.captured_at)
            signal = engine.evaluate(tick, phase)
            if signal.kind == SignalKind.NONE:
                continue
            if params.min_phase and phase.value != params.min_phase:
                # phase restriction: only enter in the chosen phase
                continue
            trade = _settle(tick, signal, resolutions.get(ticker), params)
            trades.append(trade)
            break  # one trade per ticker

    return _summarize(trades, params)


def walk_forward(db_path: str, grid: Iterable[BacktestParams], folds: int = 4) -> list[dict]:
    """Walk-forward: split tickers chronologically into folds, evaluate each grid point."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tickers = [r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM ticks ORDER BY ticker ASC"
    ).fetchall()]
    conn.close()

    if not tickers or folds < 1:
        return []

    fold_size = max(1, len(tickers) // folds)
    fold_slices = [tickers[i:i + fold_size] for i in range(0, len(tickers), fold_size)][:folds]

    results: list[dict] = []
    for params in grid:
        for idx, slice_tickers in enumerate(fold_slices):
            summary = run_backtest_for_tickers(db_path, params, slice_tickers)
            summary["fold"] = idx
            summary["params"] = asdict(params)
            results.append(summary)
    return results


def run_backtest_for_tickers(db_path: str, params: BacktestParams,
                             tickers: list[str]) -> dict:
    if not tickers:
        return _summarize([], params)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join(["?"] * len(tickers))
    rows = conn.execute(
        f"SELECT * FROM ticks WHERE ticker IN ({placeholders}) ORDER BY ticker, captured_at",
        tickers,
    ).fetchall()
    resolutions = {r["ticker"]: r["result"]
                   for r in conn.execute("SELECT * FROM market_resolutions").fetchall()}
    markets_meta = _market_windows(conn)
    conn.close()

    by_ticker: dict[str, list[Tick]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(_tick_from_row(r))

    engine = SignalEngine(SignalConfig(
        explosion_delta=params.explosion_delta,
        plateau_threshold=params.plateau_threshold,
        plateau_seconds=params.plateau_seconds,
        explosion_window_seconds=params.explosion_window_seconds,
    ))

    trades: list[Trade] = []
    for ticker, ticks in by_ticker.items():
        engine.reset()
        window = markets_meta.get(ticker)
        for tick in ticks:
            phase = _phase_for(window, tick.captured_at)
            signal = engine.evaluate(tick, phase)
            if signal.kind == SignalKind.NONE:
                continue
            trades.append(_settle(tick, signal, resolutions.get(ticker), params))
            break
    return _summarize(trades, params)


def _market_windows(conn: sqlite3.Connection) -> dict[str, tuple[datetime, datetime]]:
    # Approximate window from min/max captured_at per ticker.
    rows = conn.execute(
        """SELECT ticker, MIN(captured_at) AS first, MAX(captured_at) AS last
           FROM ticks GROUP BY ticker"""
    ).fetchall()
    return {r["ticker"]: (datetime.fromisoformat(r["first"]),
                          datetime.fromisoformat(r["last"])) for r in rows}


def _phase_for(window: tuple[datetime, datetime] | None, when: datetime) -> "object":
    from app.domain.entities import MarketPhase
    if not window:
        return MarketPhase.MIDDLE
    total = (window[1] - window[0]).total_seconds()
    if total <= 0:
        return MarketPhase.MIDDLE
    ratio = (when - window[0]).total_seconds() / total
    if ratio < 0.34:
        return MarketPhase.EARLY
    if ratio < 0.67:
        return MarketPhase.MIDDLE
    return MarketPhase.LATE


def _settle(tick: Tick, signal, resolution: str | None,
            params: BacktestParams) -> Trade:
    if signal.side == Side.YES:
        entry_price = tick.yes_ask or tick.yes_mid or tick.last_price or 0.5
    else:
        entry_price = tick.no_ask or (1.0 - (tick.yes_mid or 0.5))
    entry_price = max(0.01, min(0.99, entry_price + params.slippage_cents / 100.0))

    payoff = None
    pnl = None
    fees = None
    if resolution in {"yes", "no"}:
        won = (signal.side.value == resolution)
        payoff = 1.0 if won else 0.0
        # Fee charged on entry; on settlement, the contract pays out at 1 or 0 (no exit fee).
        fees = kalshi_fee_dollars(entry_price, 1, params.fee_rate)
        pnl = payoff - entry_price - fees
    return Trade(
        ticker=tick.ticker,
        entered_at=tick.captured_at.isoformat(),
        side=signal.side.value,
        phase=signal.phase.value,
        entry_price=round(entry_price, 4),
        resolution=resolution,
        payoff=payoff,
        pnl=round(pnl, 4) if pnl is not None else None,
        signal_kind=signal.kind.value,
        notes=f"{signal.notes} fee={fees:.3f}" if fees is not None else signal.notes,
    )


def _summarize(trades: list[Trade], params: BacktestParams) -> dict:
    settled = [t for t in trades if t.pnl is not None]
    wins = [t for t in settled if (t.pnl or 0) > 0]
    return {
        "params": asdict(params),
        "trades_count": len(trades),
        "settled_count": len(settled),
        "win_rate": (len(wins) / len(settled)) if settled else None,
        "avg_pnl": mean([t.pnl for t in settled]) if settled else None,
        "total_pnl": sum(t.pnl for t in settled) if settled else 0.0,
        "trades": [asdict(t) for t in trades],
    }
