"""Signal engine — detects probability explosions, plateaus, and extreme-close events.

Signal kinds (and the regimes they target — see scripts/calibration_ttc.py for evidence):

* EXPLOSION
    |Δp| ≥ explosion_delta in `explosion_window_seconds`. Momentum/news arrival.
    Calibration shows this is informative late, noisy early.

* PLATEAU
    Probability sustained ≥ plateau_threshold for `plateau_seconds`. Consensus.
    Without a TTC gate this fires too soon — see EXTREME_CLOSE for the calibrated
    version.

* EXTREME_CLOSE
    Probability at an extreme (≥ extreme_close_prob or its mirror) with little
    time left (TTC ≤ extreme_close_ttc_seconds). Empirically the only zone where
    the implied probability is statistically well-calibrated after Kalshi fees:
    p ≥ 0.90 with TTC ≤ 180s wins ~97% (CI_lo ~95%) on n>3k trades. Below 90% or
    further from close, fees eat the edge.
"""

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
    # Extreme-close: probability is well-calibrated only near deadline AND
    # only when the extreme is *persistent* (first-crossing entries get hit by
    # momentary spikes that reverse — see scripts/backtest_extreme_close.py).
    # Defaults from scripts/calibration_ttc.py (May 2026, n>3k per cell).
    extreme_close_prob: float = 0.90
    extreme_close_ttc_seconds: float = 180.0
    extreme_close_persistence_seconds: float = 30.0


class SignalEngine:
    """Stateful signal detector. Feed ticks in chronological order.

    Emits each (kind, side) combo at most once per market until `reset()` is
    called. This is essential for live trading: a single explosion in late
    phase produces hundreds of qualifying ticks if probability stays at the
    extreme — we want a single actionable signal.
    """

    def __init__(self, config: SignalConfig) -> None:
        self.config = config
        self._history: deque[Tick] = deque(maxlen=config.history_size)
        self._plateau_start: datetime | None = None
        self._plateau_side: Side | None = None
        # Extreme-close persistence timer (separate from plateau timer).
        self._extreme_start: datetime | None = None
        self._extreme_side: Side | None = None
        self._fired: set[tuple[str, str]] = set()

    def reset(self) -> None:
        self._history.clear()
        self._plateau_start = None
        self._plateau_side = None
        self._extreme_start = None
        self._extreme_side = None
        self._fired.clear()

    def evaluate(self, tick: Tick, phase: MarketPhase,
                 ttc_seconds: float | None = None) -> Signal:
        """Append tick, update plateau state, emit best **new** signal.

        `ttc_seconds` is time-to-close in seconds (positive while market is
        open). Required for EXTREME_CLOSE; ignored by other kinds.
        """
        self._history.append(tick)
        prob = tick.yes_mid

        if prob is None:
            return self._none_signal(tick, phase, ttc_seconds, 0.0, 0.0, "no_probability")

        extreme = self._detect_extreme_close(tick, phase, prob, ttc_seconds)
        if extreme.kind == SignalKind.EXTREME_CLOSE:
            key = ("extreme_close", extreme.side.value)
            if key not in self._fired:
                self._fired.add(key)
                return extreme
            # already fired — fall through to other detectors

        explosion = self._detect_explosion(tick, phase, prob, ttc_seconds)
        if explosion.kind == SignalKind.EXPLOSION:
            key = ("explosion", explosion.side.value)
            if key not in self._fired:
                self._fired.add(key)
                return explosion

        plateau = self._detect_plateau(tick, phase, prob, ttc_seconds)
        if plateau.kind == SignalKind.PLATEAU:
            key = ("plateau", plateau.side.value)
            if key not in self._fired:
                self._fired.add(key)
                return plateau
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0, "plateau_already_fired")
        return plateau

    def _detect_extreme_close(self, tick: Tick, phase: MarketPhase, prob: float,
                              ttc_seconds: float | None) -> Signal:
        if ttc_seconds is None or ttc_seconds < 0:
            self._extreme_start = None
            self._extreme_side = None
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0, "no_ttc")

        side: Side | None = None
        if prob >= self.config.extreme_close_prob:
            side = Side.YES
        elif prob <= 1.0 - self.config.extreme_close_prob:
            side = Side.NO

        # Track persistence even when above the TTC gate — the timer keeps
        # warming so it's ready when TTC drops into the gate. Side flip or
        # prob leaving the extreme resets the timer.
        if side is None:
            self._extreme_start = None
            self._extreme_side = None
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0,
                                     "prob_not_extreme")
        if side != self._extreme_side:
            self._extreme_start = tick.captured_at
            self._extreme_side = side

        if ttc_seconds > self.config.extreme_close_ttc_seconds:
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0,
                                     f"ttc_{int(ttc_seconds)}s_above_gate")

        elapsed = (tick.captured_at - self._extreme_start).total_seconds()
        if elapsed < self.config.extreme_close_persistence_seconds:
            return self._none_signal(
                tick, phase, ttc_seconds, prob, 0.0,
                f"extreme_warming_{int(elapsed)}s",
            )

        signed_prob = prob if side == Side.YES else (1.0 - prob)
        return Signal(
            ticker=tick.ticker,
            captured_at=tick.captured_at,
            kind=SignalKind.EXTREME_CLOSE,
            side=side,
            phase=phase,
            probability=prob,
            delta=signed_prob - self.config.extreme_close_prob,
            notes=f"p={signed_prob:.2f} ttc={int(ttc_seconds)}s persist={int(elapsed)}s",
            ttc_seconds=ttc_seconds,
        )

    def _detect_explosion(self, tick: Tick, phase: MarketPhase, prob: float,
                          ttc_seconds: float | None) -> Signal:
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
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0, "no_baseline")

        delta = prob - baseline
        if abs(delta) < self.config.explosion_delta:
            return self._none_signal(tick, phase, ttc_seconds, prob, delta,
                                     "below_explosion_delta")

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
            ttc_seconds=ttc_seconds,
        )

    def _detect_plateau(self, tick: Tick, phase: MarketPhase, prob: float,
                        ttc_seconds: float | None) -> Signal:
        side: Side | None = None
        if prob >= self.config.plateau_threshold:
            side = Side.YES
        elif prob <= 1.0 - self.config.plateau_threshold:
            side = Side.NO

        if side is None:
            self._plateau_start = None
            self._plateau_side = None
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0, "no_plateau")

        if self._plateau_side != side:
            self._plateau_start = tick.captured_at
            self._plateau_side = side
            return self._none_signal(tick, phase, ttc_seconds, prob, 0.0, "plateau_starting")

        elapsed = (tick.captured_at - self._plateau_start).total_seconds()
        if elapsed < self.config.plateau_seconds:
            return self._none_signal(
                tick, phase, ttc_seconds, prob, 0.0,
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
            ttc_seconds=ttc_seconds,
        )

    def _none_signal(self, tick: Tick, phase: MarketPhase,
                     ttc_seconds: float | None, prob: float,
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
            ttc_seconds=ttc_seconds,
        )
