"""Smoke tests for the signal engine."""

from datetime import datetime, timedelta, timezone

from app.application.signals import SignalConfig, SignalEngine
from app.domain.entities import MarketPhase, SignalKind, Tick


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


def test_explosion_yes() -> None:
    engine = SignalEngine(SignalConfig(explosion_delta=0.10, explosion_window_seconds=60))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)

    s1 = engine.evaluate(_tick(0.50, t0), MarketPhase.MIDDLE)
    assert s1.kind == SignalKind.NONE

    s2 = engine.evaluate(_tick(0.52, t0 + timedelta(seconds=30)), MarketPhase.MIDDLE)
    assert s2.kind == SignalKind.NONE

    s3 = engine.evaluate(_tick(0.68, t0 + timedelta(seconds=70)), MarketPhase.MIDDLE)
    assert s3.kind == SignalKind.EXPLOSION
    assert s3.side.value == "yes"


def test_plateau_emits_after_sustained_window() -> None:
    engine = SignalEngine(SignalConfig(plateau_threshold=0.60, plateau_seconds=60))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)

    s1 = engine.evaluate(_tick(0.65, t0), MarketPhase.MIDDLE)
    assert s1.kind == SignalKind.NONE  # still warming

    s2 = engine.evaluate(_tick(0.65, t0 + timedelta(seconds=30)), MarketPhase.MIDDLE)
    assert s2.kind == SignalKind.NONE

    s3 = engine.evaluate(_tick(0.65, t0 + timedelta(seconds=61)), MarketPhase.MIDDLE)
    assert s3.kind == SignalKind.PLATEAU
    assert s3.side.value == "yes"


def test_plateau_resets_on_dip_below_threshold() -> None:
    engine = SignalEngine(SignalConfig(plateau_threshold=0.60, plateau_seconds=60))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    engine.evaluate(_tick(0.65, t0), MarketPhase.MIDDLE)
    engine.evaluate(_tick(0.55, t0 + timedelta(seconds=30)), MarketPhase.MIDDLE)  # below
    s = engine.evaluate(_tick(0.62, t0 + timedelta(seconds=120)), MarketPhase.MIDDLE)
    # timer was reset; still warming
    assert s.kind == SignalKind.NONE
