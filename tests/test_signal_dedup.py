"""Regression: signal engine must emit each (kind, side) at most once per market."""

from datetime import datetime, timedelta, timezone

from app.application.signals import SignalConfig, SignalEngine
from app.domain.entities import MarketPhase, SignalKind, Tick


def _tick(prob: float, when: datetime) -> Tick:
    return Tick(ticker="KXBTC15M-TEST", captured_at=when,
                yes_bid=prob - 0.005, yes_ask=prob + 0.005,
                no_bid=1 - prob - 0.005, no_ask=1 - prob + 0.005,
                last_price=prob, volume=None)


def test_explosion_fires_once_per_market() -> None:
    engine = SignalEngine(SignalConfig(explosion_delta=0.10, explosion_window_seconds=60))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)

    engine.evaluate(_tick(0.50, t0), MarketPhase.LATE)
    # explosion pushes prob to extreme
    s1 = engine.evaluate(_tick(0.05, t0 + timedelta(seconds=70)), MarketPhase.LATE)
    assert s1.kind == SignalKind.EXPLOSION

    # subsequent ticks at extreme keep matching delta — should NOT re-fire
    for offset in (75, 80, 85, 90, 95):
        s = engine.evaluate(_tick(0.05, t0 + timedelta(seconds=offset)), MarketPhase.LATE)
        assert s.kind == SignalKind.NONE


def test_reset_re_enables_signals() -> None:
    engine = SignalEngine(SignalConfig(explosion_delta=0.10, explosion_window_seconds=60))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    engine.evaluate(_tick(0.50, t0), MarketPhase.LATE)
    s1 = engine.evaluate(_tick(0.05, t0 + timedelta(seconds=70)), MarketPhase.LATE)
    assert s1.kind == SignalKind.EXPLOSION

    engine.reset()
    engine.evaluate(_tick(0.50, t0), MarketPhase.LATE)
    s2 = engine.evaluate(_tick(0.05, t0 + timedelta(seconds=70)), MarketPhase.LATE)
    assert s2.kind == SignalKind.EXPLOSION
