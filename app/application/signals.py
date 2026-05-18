"""Signal engine — detects probability explosions and plateaus."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.entities import MarketPhase, Side, Signal, SignalKind, Tick


@dataclass
class SignalConfig:
    explosion_delta: float = 0.15
    plateau_threshold: float = 0.60
    plateau_seconds: int = 120
    history_size: int = 1200
    explosion_window_seconds: int = 60


class SignalEngine:
    """Stateful signal detector. Feed ticks in chronological order."""

    def __init__(self, config: SignalConfig) -> None:
        self.config = config
        self._history: deque[Tick] = deque(maxlen=config.history_size)
        self._plateau_start: datetime | None = None
        self._plateau_side: Side | None = None

    def reset(self) -> None:
        self._history.clear()
        self._plateau_start = None
        self._plateau_side = None

    def evaluate(self, tick: Tick, phase: MarketPhase) -> Signal:
        """Append tick, update plateau state, emit best signal."""
        self._history.append(tick)
        prob = tick.yes_mid

        if prob is None:
            return self._none_signal(tick, phase, 0.0, 0.0, "no_probability")

        explosion = self._detect_explosion(tick, phase, prob)
        if explosion.kind == SignalKind.EXPLOSION:
            return explosion

        plateau = self._detect_plateau(tick, phase, prob)
        return plateau

    def _detect_explosion(self, tick: Tick, phase: MarketPhase, prob: float) -> Signal:
        window_start = tick.captured_at - timedelta(seconds=self.config.explosion_window_seconds)
        baseline: float | None = None
        for past in self._history:
            if past.captured_at <= window_start:
                baseline = past.yes_mid
            else:
                break
        if baseline is None and self._history:
            first = self._history[0]
            baseline = first.yes_mid

        if baseline is None:
            return self._none_signal(tick, phase, prob, 0.0, "no_baseline")

        delta = prob - baseline
        if abs(delta) < self.config.explosion_delta:
            return self._none_signal(tick, phase, prob, delta, "below_explosion_delta")

        side = Side.YES if delta > 0 else Side.NO
        return Signal(
            ticker=tick.ticker,
            captured_at=tick.captured_at,
            kind=SignalKind.EXPLOSION,
            side=side,
            phase=phase,
            probability=prob,
            delta=delta,
            notes=f"delta={delta:+.3f} in {self.config.explosion_window_seconds}s",
        )

    def _detect_plateau(self, tick: Tick, phase: MarketPhase, prob: float) -> Signal:
        side: Side | None = None
        if prob >= self.config.plateau_threshold:
            side = Side.YES
        elif prob <= 1.0 - self.config.plateau_threshold:
            side = Side.NO

        if side is None:
            self._plateau_start = None
            self._plateau_side = None
            return self._none_signal(tick, phase, prob, 0.0, "no_plateau")

        if self._plateau_side != side:
            self._plateau_start = tick.captured_at
            self._plateau_side = side
            return self._none_signal(tick, phase, prob, 0.0, "plateau_starting")

        elapsed = (tick.captured_at - self._plateau_start).total_seconds()
        if elapsed < self.config.plateau_seconds:
            return self._none_signal(
                tick, phase, prob, 0.0,
                f"plateau_warming_{int(elapsed)}s",
            )

        signed_prob = prob if side == Side.YES else 1.0 - prob
        return Signal(
            ticker=tick.ticker,
            captured_at=tick.captured_at,
            kind=SignalKind.PLATEAU,
            side=side,
            phase=phase,
            probability=prob,
            delta=signed_prob - self.config.plateau_threshold,
            notes=f"sustained_{int(elapsed)}s>={self.config.plateau_threshold:.2f}",
        )

    def _none_signal(self, tick: Tick, phase: MarketPhase, prob: float,
                     delta: float, notes: str) -> Signal:
        return Signal(
            ticker=tick.ticker,
            captured_at=tick.captured_at,
            kind=SignalKind.NONE,
            side=Side.YES,
            phase=phase,
            probability=prob,
            delta=delta,
            notes=notes,
        )
