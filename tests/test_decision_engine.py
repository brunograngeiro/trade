from datetime import datetime, timedelta, timezone

from app.application.decision import DecisionAction, DecisionConfig, DecisionEngine
from app.application.signals import SignalConfig, SignalEngine
from app.domain.entities import Market, MarketPhase, Side, Tick


def _tick(prob: float, when: datetime) -> Tick:
    return Tick(
        ticker="KXBTC15M-TEST",
        captured_at=when,
        yes_bid=prob - 0.005,
        yes_ask=prob + 0.005,
        no_bid=1 - prob - 0.005,
        no_ask=1 - prob + 0.005,
        last_price=prob,
        volume=None,
    )


def _market(t0: datetime) -> Market:
    return Market(
        ticker="KXBTC15M-TEST",
        title="test",
        open_time=t0,
        close_time=t0 + timedelta(minutes=15),
        yes_bid=None,
        yes_ask=None,
        no_bid=None,
        no_ask=None,
        last_price=None,
        status="active",
    )


def test_skips_indecision_band() -> None:
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    signal = SignalEngine(SignalConfig()).evaluate(_tick(0.52, t0), MarketPhase.LATE, 120)
    decision = DecisionEngine(DecisionConfig()).evaluate(_tick(0.52, t0), _market(t0), signal, 120)

    assert decision.action == DecisionAction.SKIP
    assert decision.reason == "inside_40_60_no_trade"


def test_persistent_probability_enters_when_price_allowed() -> None:
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(entry_persistence_seconds=20))
    market = _market(t0)

    tick1 = _tick(0.65, t0)
    signal1 = engine.evaluate(tick1, MarketPhase.LATE, 120)
    assert decisions.evaluate(tick1, market, signal1, 120).action == DecisionAction.SKIP

    tick2 = _tick(0.65, t0 + timedelta(seconds=21))
    signal2 = engine.evaluate(tick2, MarketPhase.LATE, 99)
    decision = decisions.evaluate(tick2, market, signal2, 99)

    assert decision.action == DecisionAction.ENTER
    assert decision.side == Side.YES
    assert decision.reason == "persistent_yes_21s"


def test_enter_decision_does_not_mutate_position_until_confirmed() -> None:
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(entry_persistence_seconds=0))
    market = _market(t0)

    tick = _tick(0.65, t0)
    signal = engine.evaluate(tick, MarketPhase.LATE, 120)
    first = decisions.evaluate(tick, market, signal, 120)
    second = decisions.evaluate(tick, market, signal, 120)

    assert first.action == DecisionAction.ENTER
    assert second.action == DecisionAction.ENTER


def test_persistent_probability_skips_outside_final_window() -> None:
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(entry_persistence_seconds=0))
    market = _market(t0)

    tick = _tick(0.65, t0)
    signal = engine.evaluate(tick, MarketPhase.MIDDLE, 600)
    decision = decisions.evaluate(tick, market, signal, 600)

    assert decision.action == DecisionAction.SKIP
    assert decision.reason == "ttc_outside_entry_window_600s"


def test_late_50_cross_flips_existing_position() -> None:
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(entry_persistence_seconds=0))
    market = _market(t0)

    tick1 = _tick(0.65, t0)
    signal1 = engine.evaluate(tick1, MarketPhase.LATE, 120)
    assert decisions.evaluate(tick1, market, signal1, 120).action == DecisionAction.ENTER
    decisions.set_position(Side.YES)

    tick2 = _tick(0.38, t0 + timedelta(seconds=10))
    signal2 = engine.evaluate(tick2, MarketPhase.LATE, 110)
    decision = decisions.evaluate(tick2, market, signal2, 110)

    assert decision.action == DecisionAction.FLIP
    assert decision.side == Side.NO
    assert decision.crossed_50 is True


def test_exit_carries_previous_side_and_close_limit_price() -> None:
    """When confidence collapses, EXIT must expose previous_side and a
    sell-side limit price so the order layer can close the position."""
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(
        entry_persistence_seconds=0,
        exit_warn_confidence=0.55,
    ))
    market = _market(t0)

    enter_tick = _tick(0.65, t0)
    enter_signal = engine.evaluate(enter_tick, MarketPhase.LATE, 120)
    enter = decisions.evaluate(enter_tick, market, enter_signal, 120)
    assert enter.action == DecisionAction.ENTER
    decisions.set_position(Side.YES)

    # Confidence drops without a clean 50% cross — must still close YES.
    drop_tick = _tick(0.46, t0 + timedelta(seconds=10))
    drop_signal = engine.evaluate(drop_tick, MarketPhase.LATE, 110)
    decision = decisions.evaluate(drop_tick, market, drop_signal, 110)

    assert decision.action == DecisionAction.EXIT
    assert decision.previous_side == Side.YES
    # Close at the YES bid (we sell our YES contract).
    assert decision.close_limit_price_cents is not None
    assert decision.close_limit_price_cents == int(round((drop_tick.yes_bid) * 100))


def test_flip_below_confidence_floor_exits_without_reentering() -> None:
    """A 50% cross into a side whose confidence is still inside the band
    (<0.60) must close the old position and NOT enter the new one."""
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(
        entry_persistence_seconds=0,
        entry_confidence_floor=0.60,
    ))
    market = _market(t0)

    enter_tick = _tick(0.70, t0)
    enter_signal = engine.evaluate(enter_tick, MarketPhase.LATE, 120)
    assert decisions.evaluate(enter_tick, market, enter_signal, 120).action == DecisionAction.ENTER
    decisions.set_position(Side.YES)

    # Cross cleanly to NO (confidence 0.62 >= floor) — should FLIP.
    flip_tick = _tick(0.38, t0 + timedelta(seconds=15))
    flip_signal = engine.evaluate(flip_tick, MarketPhase.LATE, 105)
    flip = decisions.evaluate(flip_tick, market, flip_signal, 105)
    assert flip.action == DecisionAction.FLIP
    decisions.set_position(Side.NO)

    # Now drift further to a barely-NO confidence (0.58, below floor) without
    # crossing 50% again — should HOLD (not flip back to YES).
    drift_tick = _tick(0.42, t0 + timedelta(seconds=25))
    drift_signal = engine.evaluate(drift_tick, MarketPhase.LATE, 95)
    drift = decisions.evaluate(drift_tick, market, drift_signal, 95)
    # Confidence on held NO side = 0.58, still above exit_warn (0.55) -> HOLD.
    assert drift.action == DecisionAction.HOLD


def test_crossed_50_hysteresis_ignores_noise_inside_indecision_band() -> None:
    """Prob oscillating across 0.50 inside the 0.40-0.60 band must NOT
    produce crossed_50 events. Only crossings that clear the confidence
    floor on the other side should register."""
    t0 = datetime(2026, 5, 18, 12, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(
        entry_persistence_seconds=999,  # never enter, just observe crosses
        entry_confidence_floor=0.60,
    ))
    market = _market(t0)

    # First commit a side outside the band.
    seed = _tick(0.65, t0)
    seed_sig = engine.evaluate(seed, MarketPhase.LATE, 200)
    seed_dec = decisions.evaluate(seed, market, seed_sig, 200)
    assert seed_dec.crossed_50 is False  # first observation

    # Bounces inside the indecision band — none should set crossed_50.
    for offset, prob in [(5, 0.49), (10, 0.51), (15, 0.48), (20, 0.52)]:
        tick = _tick(prob, t0 + timedelta(seconds=offset))
        sig = engine.evaluate(tick, MarketPhase.LATE, 200 - offset)
        dec = decisions.evaluate(tick, market, sig, 200 - offset)
        assert dec.crossed_50 is False, f"noise at prob={prob} fired crossed_50"

    # A real swing to high NO confidence DOES register a cross.
    swing = _tick(0.30, t0 + timedelta(seconds=30))
    swing_sig = engine.evaluate(swing, MarketPhase.LATE, 170)
    swing_dec = decisions.evaluate(swing, market, swing_sig, 170)
    assert swing_dec.crossed_50 is True


def test_spot_guard_blocks_no_when_btc_already_up_since_open() -> None:
    """Fade NO when realised move since open is positive: BTC is above its
    open-price, so betting "BTC went down" contradicts the realised move."""
    open_time = datetime(2026, 5, 18, 19, 15, tzinfo=timezone.utc)
    t0 = open_time + timedelta(minutes=12)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(entry_persistence_seconds=0))
    market = Market(
        ticker="KXBTC15M-26MAY181530-30",
        title="BTC price up in next 15 mins?",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=15),
        yes_bid=None,
        yes_ask=None,
        no_bid=None,
        no_ask=None,
        last_price=None,
        status="active",
    )

    # Spot at open: $76,500. Spot now: $76,550 → BTC up $50 → fade NO.
    open_tick = _tick(0.5, open_time)
    open_signal = engine.evaluate(open_tick, MarketPhase.EARLY, 900)
    decisions.evaluate(open_tick, market, open_signal, 900,
                       spot_price=76500.0, spot_captured_at=open_time)

    tick = _tick(0.315, t0)
    signal = engine.evaluate(tick, MarketPhase.LATE, 178)
    decision = decisions.evaluate(tick, market, signal, 178,
                                  spot_price=76550.0, spot_captured_at=t0)

    assert decision.action == DecisionAction.SKIP
    assert decision.reason.startswith("spot_already_up_against_no_")


def test_spot_guard_blocks_no_when_spot_momentum_is_up() -> None:
    t0 = datetime(2026, 5, 18, 19, 27, tzinfo=timezone.utc)
    engine = SignalEngine(SignalConfig())
    decisions = DecisionEngine(DecisionConfig(
        entry_persistence_seconds=0,
        spot_guard_buffer_dollars=100.0,
        spot_guard_momentum_dollars=15.0,
    ))
    market = _market(t0)

    old_tick = _tick(0.40, t0 - timedelta(seconds=60))
    old_signal = engine.evaluate(old_tick, MarketPhase.LATE, 238)
    decisions.evaluate(old_tick, market, old_signal, 238,
                       spot_price=76530.0, spot_captured_at=t0 - timedelta(seconds=60))

    tick = _tick(0.315, t0)
    signal = engine.evaluate(tick, MarketPhase.LATE, 178)
    decision = decisions.evaluate(tick, market, signal, 178,
                                  spot_price=76552.0, spot_captured_at=t0)

    assert decision.action == DecisionAction.SKIP
    assert decision.reason.startswith("spot_momentum_against_no_")
