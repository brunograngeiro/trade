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


def test_extreme_close_yes_fires_when_prob_high_and_ttc_low() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=0.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    s = engine.evaluate(_tick(0.92, t0), MarketPhase.LATE, ttc_seconds=120)
    assert s.kind == SignalKind.EXTREME_CLOSE
    assert s.side.value == "yes"
    assert s.ttc_seconds == 120


def test_extreme_close_no_fires_at_low_prob() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=0.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    s = engine.evaluate(_tick(0.05, t0), MarketPhase.LATE, ttc_seconds=60)
    assert s.kind == SignalKind.EXTREME_CLOSE
    assert s.side.value == "no"


def test_extreme_close_does_not_fire_outside_ttc_gate() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=0.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    s = engine.evaluate(_tick(0.95, t0), MarketPhase.LATE, ttc_seconds=600)
    # Outside the TTC gate, extreme_close stays silent; another detector may
    # still emit a non-actionable status, but the kind must not be EXTREME_CLOSE.
    assert s.kind != SignalKind.EXTREME_CLOSE


def test_extreme_close_does_not_fire_without_ttc() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=0.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    s = engine.evaluate(_tick(0.95, t0), MarketPhase.LATE)
    assert s.kind == SignalKind.NONE


def test_extreme_close_requires_persistence_when_configured() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=30.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    # First tick at high prob inside the TTC gate — must still warm up.
    s1 = engine.evaluate(_tick(0.95, t0), MarketPhase.LATE, ttc_seconds=120)
    assert s1.kind == SignalKind.NONE
    # 10s in: still warming.
    s2 = engine.evaluate(_tick(0.95, t0 + timedelta(seconds=10)),
                         MarketPhase.LATE, ttc_seconds=110)
    assert s2.kind == SignalKind.NONE
    # 31s in: persistence cleared.
    s3 = engine.evaluate(_tick(0.95, t0 + timedelta(seconds=31)),
                         MarketPhase.LATE, ttc_seconds=90)
    assert s3.kind == SignalKind.EXTREME_CLOSE
    assert s3.side.value == "yes"


def test_extreme_close_persistence_resets_on_side_flip() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=30.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    engine.evaluate(_tick(0.95, t0), MarketPhase.LATE, ttc_seconds=120)
    engine.evaluate(_tick(0.05, t0 + timedelta(seconds=10)),
                    MarketPhase.LATE, ttc_seconds=110)  # flip side, timer resets
    s = engine.evaluate(_tick(0.95, t0 + timedelta(seconds=40)),
                        MarketPhase.LATE, ttc_seconds=80)
    # 30s after the flip-back-to-yes — counted from t0+10 it would be 30s, fire.
    # But the flip restarts at t0+10 only if side stayed; here at t0+10 side
    # was NO, so YES restarts at t0+40 -> still warming.
    assert s.kind != SignalKind.EXTREME_CLOSE


def test_extreme_close_fires_only_once_per_market() -> None:
    engine = SignalEngine(SignalConfig(
        extreme_close_prob=0.90, extreme_close_ttc_seconds=180,
        extreme_close_persistence_seconds=0.0,
    ))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    s1 = engine.evaluate(_tick(0.95, t0), MarketPhase.LATE, ttc_seconds=100)
    s2 = engine.evaluate(_tick(0.97, t0 + timedelta(seconds=5)),
                         MarketPhase.LATE, ttc_seconds=95)
    assert s1.kind == SignalKind.EXTREME_CLOSE
    assert s2.kind != SignalKind.EXTREME_CLOSE  # already fired this side


def test_plateau_resets_on_dip_below_threshold() -> None:
    engine = SignalEngine(SignalConfig(plateau_threshold=0.60, plateau_seconds=60))
    t0 = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    engine.evaluate(_tick(0.65, t0), MarketPhase.MIDDLE)
    engine.evaluate(_tick(0.55, t0 + timedelta(seconds=30)), MarketPhase.MIDDLE)  # below
    s = engine.evaluate(_tick(0.62, t0 + timedelta(seconds=120)), MarketPhase.MIDDLE)
    # timer was reset; still warming
    assert s.kind == SignalKind.NONE
