"""Operational decision layer for Kalshi BTC 15m."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.domain.entities import Market, Side, Signal, SignalKind, Tick


class DecisionAction(str, Enum):
    ENTER = "enter"
    EXIT = "exit"
    FLIP = "flip"
    HOLD = "hold"
    SKIP = "skip"


@dataclass(frozen=True)
class DecisionConfig:
    enabled: bool = True
    entry_confidence_floor: float = 0.60
    entry_persistence_seconds: float = 20.0
    entry_ttc_seconds: float = 180.0
    late_cross_ttc_seconds: float = 180.0
    late_cross_confirmation_seconds: float = 10.0
    late_cross_strong_confidence: float = 0.70
    late_cross_final_ttc_seconds: float = 60.0
    late_cross_final_confidence: float = 0.85
    flip_cross_ttc_seconds: float = 180.0
    flip_enabled: bool = False
    exit_warn_confidence: float = 0.55
    exit_confirmation_seconds: float = 10.0
    min_exit_ttc_seconds: float = 5.0
    extreme_close_ttc_seconds: float = 60.0
    max_entry_price_180s_cents: int = 72
    max_entry_price_60s_cents: int = 92
    max_entry_price_30s_cents: int = 97
    spot_guard_enabled: bool = True
    spot_guard_buffer_dollars: float = 5.0
    spot_guard_momentum_seconds: float = 60.0
    spot_guard_momentum_dollars: float = 15.0


@dataclass(frozen=True)
class StrategyDecision:
    ticker: str
    captured_at: datetime
    action: DecisionAction
    side: Side | None
    probability: float | None
    confidence: float | None
    ttc_seconds: float | None
    limit_price_cents: int | None
    reason: str
    signal_kind: str
    crossed_50: bool = False
    # Side being closed (set on EXIT/FLIP). On FLIP, `side` is the new long;
    # `previous_side` is the one to sell out. On ENTER/HOLD/SKIP, None.
    previous_side: Side | None = None
    # Limit price (cents) for the side being closed — best bid of that side.
    # On EXIT/FLIP, used to construct the SELL order. On other actions, None.
    close_limit_price_cents: int | None = None


class DecisionEngine:
    """Turns signals and market state into enter/exit/flip/skip decisions."""

    def __init__(self, config: DecisionConfig) -> None:
        self.config = config
        self._ticker: str | None = None
        self._last_side: Side | None = None
        self._support_side: Side | None = None
        self._support_start: datetime | None = None
        self._pending_cross_side: Side | None = None
        self._pending_cross_start: datetime | None = None
        self._exit_warning_side: Side | None = None
        self._exit_warning_start: datetime | None = None
        self._position_side: Side | None = None
        self._spot_history: deque[tuple[datetime, float]] = deque(maxlen=1200)
        # Spot at the start of the current 15-min window. KXBTC15M is a
        # directional market ("did BTC go up in the last 15 min?"), so the
        # implicit strike is the spot price when the market opened. We track
        # it explicitly here so the spot guard can fade trades that contradict
        # the realised move.
        self._spot_at_open: float | None = None

    def reset(self) -> None:
        self._ticker = None
        self._last_side = None
        self._support_side = None
        self._support_start = None
        self._pending_cross_side = None
        self._pending_cross_start = None
        self._exit_warning_side = None
        self._exit_warning_start = None
        self._position_side = None
        self._spot_history.clear()
        self._spot_at_open = None

    def set_position(self, side: Side | None) -> None:
        """External hook: hydrate the held position (e.g. from Kalshi positions
        endpoint after a service restart). Caller is responsible for matching
        the ticker."""
        self._position_side = side

    def evaluate(self, tick: Tick, market: Market, signal: Signal,
                 ttc_seconds: float | None, spot_price: float | None = None,
                 spot_captured_at: datetime | None = None) -> StrategyDecision:
        if tick.ticker != self._ticker:
            self.reset()
            self._ticker = tick.ticker
        self._record_spot(spot_price, spot_captured_at or tick.captured_at)
        self._update_spot_at_open(market, spot_price,
                                  spot_captured_at or tick.captured_at)

        prob = tick.yes_mid
        if not self.config.enabled:
            return self._decision(tick, DecisionAction.SKIP, None, prob, ttc_seconds,
                                  None, "decision_engine_disabled", signal)
        if prob is None:
            return self._decision(tick, DecisionAction.SKIP, None, prob, ttc_seconds,
                                  None, "no_probability", signal)

        side = Side.YES if prob > 0.5 else Side.NO
        confidence = prob if side == Side.YES else 1.0 - prob
        # Hysteresis: only register a 50% cross when prob CLEARS the indecision
        # band (>= confidence_floor on the other side). Inside 0.40-0.60 we keep
        # the previous "committed" side so noise doesn't spam crossed_50 flags
        # and trigger spurious FLIPs.
        crossed_50 = False
        if confidence >= self.config.entry_confidence_floor:
            if self._last_side is not None and side != self._last_side:
                crossed_50 = True
            self._last_side = side

        self._update_support(tick.captured_at, side, confidence)
        supported_seconds = self._supported_seconds(tick.captured_at, side)
        limit_price = self._entry_price_cents(tick, side)

        if self._position_side is not None:
            return self._position_decision(
                tick, signal, ttc_seconds, side, confidence, crossed_50, limit_price
            )

        if confidence < self.config.entry_confidence_floor:
            self._clear_pending_cross()
            return self._decision(tick, DecisionAction.SKIP, side, prob, ttc_seconds,
                                  limit_price, "inside_40_60_no_trade", signal,
                                  crossed_50=crossed_50)

        if not self._inside_ttc(ttc_seconds, self.config.entry_ttc_seconds):
            return self._decision(
                tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
                f"ttc_outside_entry_window_{int(ttc_seconds or -1)}s", signal,
                crossed_50=crossed_50,
            )

        spot_block_reason = self._spot_guard_reason(side, spot_price)
        if spot_block_reason:
            return self._decision(
                tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
                spot_block_reason, signal, crossed_50=crossed_50,
            )

        max_price = self._max_entry_price(ttc_seconds)
        if max_price is not None and limit_price is not None and limit_price > max_price:
            return self._decision(
                tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
                f"entry_price_{limit_price}c_above_max_{max_price}c", signal,
                crossed_50=crossed_50,
            )

        if signal.kind == SignalKind.EXTREME_CLOSE:
            self._clear_pending_cross()
            return self._decision(tick, DecisionAction.ENTER, side, prob, ttc_seconds,
                                  limit_price, "extreme_close_calibrated_entry",
                                  signal, crossed_50=crossed_50)

        late_cross = self._late_cross_decision(
            tick, signal, ttc_seconds, side, prob, confidence, limit_price, crossed_50
        )
        if late_cross is not None:
            return late_cross

        if supported_seconds >= self.config.entry_persistence_seconds:
            self._clear_pending_cross()
            return self._decision(
                tick, DecisionAction.ENTER, side, prob, ttc_seconds, limit_price,
                f"persistent_{side.value}_{int(supported_seconds)}s", signal,
                crossed_50=crossed_50,
            )

        return self._decision(
            tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
            f"warming_persistence_{int(supported_seconds)}s", signal,
            crossed_50=crossed_50,
        )

    def _position_decision(self, tick: Tick, signal: Signal, ttc_seconds: float | None,
                           market_side: Side, confidence: float, crossed_50: bool,
                           limit_price: int | None) -> StrategyDecision:
        prob = tick.yes_mid
        assert self._position_side is not None

        if self._inside_ttc(ttc_seconds, self.config.min_exit_ttc_seconds):
            self._clear_exit_warning()
            return self._decision(tick, DecisionAction.HOLD, self._position_side, prob,
                                  ttc_seconds, limit_price, "position_near_close_no_exit",
                                  signal, crossed_50=crossed_50)

        if market_side != self._position_side and crossed_50:
            old_side = self._position_side
            self._clear_exit_warning()
            # FLIP requires the same confidence floor as a fresh ENTER — we
            # don't pivot into a barely-better-than-coin-flip side. Below the
            # floor we still close the losing leg (EXIT) but skip the re-entry.
            confident_enough = confidence >= self.config.entry_confidence_floor
            if (self.config.flip_enabled
                    and confident_enough
                    and self._inside_ttc(ttc_seconds, self.config.flip_cross_ttc_seconds)):
                return self._decision(
                    tick, DecisionAction.FLIP, market_side, prob, ttc_seconds, limit_price,
                    f"late_50_cross_against_{old_side.value}", signal,
                    crossed_50=True, previous_side=old_side,
                )
            reason = (f"50_cross_exit_flip_disabled_{old_side.value}"
                      if confident_enough and not self.config.flip_enabled
                      else f"50_cross_invalidated_{old_side.value}" if confident_enough
                      else f"50_cross_low_confidence_{confidence:.2f}_exit_{old_side.value}")
            return self._decision(
                tick, DecisionAction.EXIT, old_side, prob, ttc_seconds, limit_price,
                reason, signal, crossed_50=True, previous_side=old_side,
            )

        position_confidence = prob if self._position_side == Side.YES else 1.0 - (prob or 0.5)
        if position_confidence < self.config.exit_warn_confidence:
            old_side = self._position_side
            if self._exit_warning_side != old_side or self._exit_warning_start is None:
                self._exit_warning_side = old_side
                self._exit_warning_start = tick.captured_at
            warning_seconds = max(
                0.0, (tick.captured_at - self._exit_warning_start).total_seconds()
            )
            if warning_seconds < self.config.exit_confirmation_seconds:
                return self._decision(
                    tick, DecisionAction.HOLD, old_side, prob, ttc_seconds, limit_price,
                    f"exit_waiting_confirmation_{int(warning_seconds)}s_confidence_{position_confidence:.2f}",
                    signal, crossed_50=crossed_50,
                )
            self._clear_exit_warning()
            return self._decision(
                tick, DecisionAction.EXIT, old_side, prob, ttc_seconds, limit_price,
                f"position_confidence_dropped_to_{position_confidence:.2f}_confirmed",
                signal, crossed_50=crossed_50, previous_side=old_side,
            )

        self._clear_exit_warning()
        return self._decision(tick, DecisionAction.HOLD, self._position_side, prob,
                              ttc_seconds, limit_price, "position_still_valid",
                              signal, crossed_50=crossed_50)

    def _late_cross_decision(self, tick: Tick, signal: Signal, ttc_seconds: float | None,
                             side: Side, prob: float, confidence: float,
                             limit_price: int | None,
                             crossed_50: bool) -> StrategyDecision | None:
        if not self._inside_ttc(ttc_seconds, self.config.late_cross_ttc_seconds):
            self._clear_pending_cross()
            return None

        in_final_minute = self._inside_ttc(ttc_seconds, self.config.late_cross_final_ttc_seconds)

        if crossed_50:
            self._pending_cross_side = side
            self._pending_cross_start = tick.captured_at
            if in_final_minute:
                if confidence >= self.config.late_cross_final_confidence:
                    self._clear_pending_cross()
                    return self._decision(
                        tick, DecisionAction.ENTER, side, prob, ttc_seconds, limit_price,
                        "late_50_cross_final_high_confidence", signal, crossed_50=True,
                    )
                return self._decision(
                    tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
                    f"late_cross_final_needs_{int(self.config.late_cross_final_confidence * 100)}c",
                    signal, crossed_50=True,
                )
            if confidence >= self.config.late_cross_strong_confidence:
                self._clear_pending_cross()
                return self._decision(
                    tick, DecisionAction.ENTER, side, prob, ttc_seconds, limit_price,
                    "late_50_cross_strong_confidence", signal, crossed_50=True,
                )
            return self._decision(
                tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
                "late_50_cross_waiting_confirmation_0s", signal, crossed_50=True,
            )

        if self._pending_cross_side != side or self._pending_cross_start is None:
            return None

        pending_seconds = max(
            0.0, (tick.captured_at - self._pending_cross_start).total_seconds()
        )
        if in_final_minute:
            if confidence >= self.config.late_cross_final_confidence:
                self._clear_pending_cross()
                return self._decision(
                    tick, DecisionAction.ENTER, side, prob, ttc_seconds, limit_price,
                    "late_50_cross_final_high_confidence", signal, crossed_50=False,
                )
            return self._decision(
                tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
                f"late_cross_final_needs_{int(self.config.late_cross_final_confidence * 100)}c",
                signal, crossed_50=False,
            )
        if pending_seconds >= self.config.late_cross_confirmation_seconds:
            self._clear_pending_cross()
            return self._decision(
                tick, DecisionAction.ENTER, side, prob, ttc_seconds, limit_price,
                f"late_50_cross_confirmed_{int(pending_seconds)}s",
                signal, crossed_50=False,
            )
        return self._decision(
            tick, DecisionAction.SKIP, side, prob, ttc_seconds, limit_price,
            f"late_50_cross_waiting_confirmation_{int(pending_seconds)}s",
            signal, crossed_50=False,
        )

    def _record_spot(self, spot_price: float | None, when: datetime) -> None:
        if spot_price is None:
            return
        self._spot_history.append((when, spot_price))

    def _update_spot_at_open(self, market: Market, spot_price: float | None,
                             when: datetime) -> None:
        """Capture the spot price at (or near) market open.

        KXBTC15M is directional: it resolves YES if BTC's price rose during the
        15-min window, NO if it fell. The implicit strike is the spot at open.
        We accept the earliest spot we observe inside the window as a proxy
        when the precise open-time tick is unavailable (e.g. service restart
        after the window started).
        """
        if self._spot_at_open is not None or spot_price is None:
            return
        # Only freeze the open-spot once we're actually inside this market's
        # window. Outside it we may have ticks from a previous market still
        # carrying state.
        if when < market.open_time or when > market.close_time:
            return
        self._spot_at_open = spot_price

    def _spot_guard_reason(self, side: Side, spot_price: float | None) -> str | None:
        if not self.config.spot_guard_enabled or spot_price is None:
            return None

        # Directional guard for KXBTC15M: fade trades that contradict the
        # realised BTC move since market open. NO bets only make sense if BTC
        # is below its open-price; YES bets only if it is above.
        if self._spot_at_open is not None:
            buffer = self.config.spot_guard_buffer_dollars
            delta = spot_price - self._spot_at_open
            if side == Side.NO and delta >= buffer:
                return (f"spot_already_up_against_no_{delta:+.2f}"
                        f"_open_{self._spot_at_open:.2f}")
            if side == Side.YES and delta <= -buffer:
                return (f"spot_already_down_against_yes_{delta:+.2f}"
                        f"_open_{self._spot_at_open:.2f}")

        # Short-term momentum guard: even if the realised move so far favours
        # us, a sharp reversal in the last minute is reason to skip.
        old_spot = self._spot_before_seconds(self.config.spot_guard_momentum_seconds)
        if old_spot is None:
            return None
        delta = spot_price - old_spot
        if side == Side.NO and delta >= self.config.spot_guard_momentum_dollars:
            return f"spot_momentum_against_no_{delta:+.2f}_{int(self.config.spot_guard_momentum_seconds)}s"
        if side == Side.YES and delta <= -self.config.spot_guard_momentum_dollars:
            return f"spot_momentum_against_yes_{delta:+.2f}_{int(self.config.spot_guard_momentum_seconds)}s"
        return None

    def _spot_before_seconds(self, seconds: float) -> float | None:
        if not self._spot_history:
            return None
        target = self._spot_history[-1][0].timestamp() - seconds
        best: float | None = None
        for when, price in reversed(self._spot_history):
            if when.timestamp() <= target:
                best = price
                break
        return best

    def _update_support(self, when: datetime, side: Side, confidence: float) -> None:
        if confidence < self.config.entry_confidence_floor:
            self._support_side = None
            self._support_start = None
            return
        if self._support_side != side:
            self._support_side = side
            self._support_start = when

    def _supported_seconds(self, when: datetime, side: Side) -> float:
        if self._support_side != side or self._support_start is None:
            return 0.0
        return max(0.0, (when - self._support_start).total_seconds())

    def _clear_pending_cross(self) -> None:
        self._pending_cross_side = None
        self._pending_cross_start = None

    def _clear_exit_warning(self) -> None:
        self._exit_warning_side = None
        self._exit_warning_start = None

    def _entry_price_cents(self, tick: Tick, side: Side) -> int | None:
        price = tick.yes_ask if side == Side.YES else tick.no_ask
        if price is None:
            return None
        return int(round(price * 100))

    def _max_entry_price(self, ttc_seconds: float | None) -> int | None:
        if ttc_seconds is None:
            return self.config.max_entry_price_180s_cents
        if ttc_seconds <= 30:
            return self.config.max_entry_price_30s_cents
        if ttc_seconds <= 60:
            return self.config.max_entry_price_60s_cents
        return self.config.max_entry_price_180s_cents

    def _inside_ttc(self, ttc_seconds: float | None, gate: float) -> bool:
        return ttc_seconds is not None and 0 <= ttc_seconds <= gate

    def _decision(self, tick: Tick, action: DecisionAction, side: Side | None,
                  prob: float | None, ttc_seconds: float | None,
                  limit_price_cents: int | None, reason: str, signal: Signal,
                  *, crossed_50: bool = False,
                  previous_side: Side | None = None) -> StrategyDecision:
        confidence = max(prob, 1.0 - prob) if prob is not None else None
        close_limit = None
        if previous_side is not None:
            # To close, sell our held side at its best bid.
            close_limit = self._close_price_cents(tick, previous_side)
        return StrategyDecision(
            ticker=tick.ticker,
            captured_at=tick.captured_at,
            action=action,
            side=side,
            probability=prob,
            confidence=confidence,
            ttc_seconds=ttc_seconds,
            limit_price_cents=limit_price_cents,
            reason=reason,
            signal_kind=signal.kind.value,
            crossed_50=crossed_50,
            previous_side=previous_side,
            close_limit_price_cents=close_limit,
        )

    def _close_price_cents(self, tick: Tick, side: Side) -> int | None:
        bid = tick.yes_bid if side == Side.YES else tick.no_bid
        if bid is None:
            return None
        return int(round(bid * 100))
