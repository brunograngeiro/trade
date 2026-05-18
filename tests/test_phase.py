"""Tests for Market.phase_at."""

from datetime import datetime, timedelta, timezone

from app.domain.entities import Market, MarketPhase


def _market() -> Market:
    open_t = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    close_t = open_t + timedelta(minutes=15)
    return Market(
        ticker="KXBTC15M-TEST",
        title="t",
        open_time=open_t,
        close_time=close_t,
        yes_bid=0.5, yes_ask=0.5, no_bid=0.5, no_ask=0.5, last_price=0.5,
        status="active",
    )


def test_phase_early() -> None:
    m = _market()
    assert m.phase_at(m.open_time + timedelta(minutes=1)) == MarketPhase.EARLY


def test_phase_middle() -> None:
    m = _market()
    assert m.phase_at(m.open_time + timedelta(minutes=8)) == MarketPhase.MIDDLE


def test_phase_late() -> None:
    m = _market()
    assert m.phase_at(m.open_time + timedelta(minutes=13)) == MarketPhase.LATE


def test_phase_expired() -> None:
    m = _market()
    assert m.phase_at(m.close_time + timedelta(seconds=1)) == MarketPhase.EXPIRED
