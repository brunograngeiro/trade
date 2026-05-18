"""Tick collector: polls the active market on a fixed cadence.

Production resilience:
  - per-iteration `asyncio.wait_for` with `collector_http_timeout_seconds`
  - exponential backoff on consecutive failures (base..max)
  - heartbeat counters exposed for monitoring
  - never raises out of the loop (any exception is logged + counted)
  - graceful stop via Event
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.config import Settings
from app.domain.entities import Market, MarketPhase, Signal, SignalKind, Tick
from app.infrastructure.coinbase.client import CoinbaseClient, SpotTick
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient
from app.infrastructure.kalshi.mapper import market_from_payload, tick_from_market
from app.application.market_discovery import current_market
from app.application.signals import SignalConfig, SignalEngine


log = logging.getLogger(__name__)


@dataclass
class CollectorHealth:
    started_at: datetime | None = None
    last_iteration_at: datetime | None = None
    last_tick_at: datetime | None = None
    last_signal_at: datetime | None = None
    iterations: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    current_ticker: str | None = None
    current_phase: str | None = None
    backoff_seconds: float = 0.0

    def snapshot(self) -> dict:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None
        return {
            "started_at": iso(self.started_at),
            "last_iteration_at": iso(self.last_iteration_at),
            "last_tick_at": iso(self.last_tick_at),
            "last_signal_at": iso(self.last_signal_at),
            "iterations": self.iterations,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "current_ticker": self.current_ticker,
            "current_phase": self.current_phase,
            "backoff_seconds": self.backoff_seconds,
        }


class Collector:
    """Background loop that ingests ticks and computes signals."""

    def __init__(self, settings: Settings, client: KalshiClient, db: Database,
                 on_tick: Callable[[Tick, Market, Signal], Awaitable[None]] | None = None,
                 coinbase: CoinbaseClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.on_tick = on_tick
        self.coinbase = coinbase
        self.engine = SignalEngine(SignalConfig(
            explosion_delta=settings.prob_explosion_delta,
            plateau_threshold=settings.prob_plateau_threshold,
            plateau_seconds=settings.prob_plateau_seconds,
        ))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._current_ticker: str | None = None
        self.last_tick: Tick | None = None
        self.last_market: Market | None = None
        self.last_signal: Signal | None = None
        self.last_spot: SpotTick | None = None
        self.health = CollectorHealth()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self.health.started_at = datetime.now(timezone.utc)
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("Collector started — poll=%.1fs phase_filter=%s",
                 self.settings.collector_poll_seconds,
                 self.settings.strategy_enter_only_in_phase or "any")
        while not self._stop.is_set():
            self.health.iterations += 1
            self.health.last_iteration_at = datetime.now(timezone.utc)
            try:
                await asyncio.wait_for(
                    self._tick_once(),
                    timeout=self.settings.collector_http_timeout_seconds,
                )
                self.health.successes += 1
                self.health.consecutive_failures = 0
                self.health.last_error = None
                self.health.backoff_seconds = 0.0
            except asyncio.TimeoutError:
                self._record_failure("timeout")
            except Exception as exc:  # noqa: BLE001
                self._record_failure(repr(exc))

            sleep = self._next_sleep()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep)
            except asyncio.TimeoutError:
                pass

            if self.health.consecutive_failures >= self.settings.collector_max_consecutive_failures:
                log.error("Collector exceeded max consecutive failures (%d) — pausing 5min.",
                          self.health.consecutive_failures)
                self.health.backoff_seconds = 300.0
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=300.0)
                except asyncio.TimeoutError:
                    pass
                self.health.consecutive_failures = 0
        log.info("Collector stopped")

    def _record_failure(self, reason: str) -> None:
        self.health.failures += 1
        self.health.consecutive_failures += 1
        self.health.last_error = reason
        log.warning("Collector iteration failed (%d in a row): %s",
                    self.health.consecutive_failures, reason)

    def _next_sleep(self) -> float:
        if self.health.consecutive_failures == 0:
            return self.settings.collector_poll_seconds
        backoff = min(
            self.settings.collector_backoff_max_seconds,
            self.settings.collector_backoff_base_seconds * (2 ** (self.health.consecutive_failures - 1)),
        )
        self.health.backoff_seconds = backoff
        return backoff

    async def _tick_once(self) -> None:
        market = await current_market(self.client, self.settings.kalshi_series_ticker)
        if market is None:
            return
        if market.ticker != self._current_ticker:
            log.info("New market: %s (%s..%s)",
                     market.ticker, market.open_time.isoformat(), market.close_time.isoformat())
            self.engine.reset()
            self._current_ticker = market.ticker

        latest = await self.client.get_market(market.ticker)
        if latest.get("market"):
            market = market_from_payload(latest["market"])

        tick = tick_from_market(market)
        self.db.save_tick(tick)
        now = datetime.now(timezone.utc)
        phase = market.phase_at(now)

        if self.coinbase is not None:
            try:
                spot = await self.coinbase.get_ticker()
                if spot:
                    self.db.save_spot_tick(spot.product, spot.captured_at, spot.price,
                                           spot.bid, spot.ask, spot.volume_24h)
                    self.last_spot = spot
            except Exception:  # noqa: BLE001
                log.exception("Coinbase fetch failed (non-fatal)")

        signal = self.engine.evaluate(tick, phase)

        # Apply phase filter: signal is only "actionable" if phase matches
        actionable = True
        only_phase = self.settings.strategy_enter_only_in_phase
        if only_phase and phase.value != only_phase:
            actionable = False

        if signal.kind != SignalKind.NONE and actionable:
            self.db.save_signal(signal)
            self.health.last_signal_at = now

        self.last_tick = tick
        self.last_market = market
        self.last_signal = signal
        self.health.last_tick_at = now
        self.health.current_ticker = market.ticker
        self.health.current_phase = phase.value

        if self.on_tick:
            await self.on_tick(tick, market, signal)
