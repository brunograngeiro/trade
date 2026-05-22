import asyncio

from app.application.risk import RiskManager
from app.config import Settings
from app.domain.entities import OrderRequest, Side


class FakeClient:
    async def get_balance(self):
        return {"balance": 1200}


class FakeDb:
    def __init__(self, *, open_risk=0, has_open=False, trades=0, pnl=0.0, losses=0):
        self.open_risk = open_risk
        self.has_open = has_open
        self.trades = trades
        self.pnl = pnl
        self.losses = losses

    def risk_summary(self):
        return {
            "open_risk_cents": self.open_risk,
            "open_trades": [],
            "trades_today": self.trades,
            "daily_realized_pnl_dollars": self.pnl,
            "consecutive_losses": self.losses,
        }

    def has_open_real_trade(self, ticker):
        return self.has_open


def _settings(**overrides):
    data = {
        "RISK_MAX_BALANCE_FRACTION": 0.5,
        "RISK_MAX_DAILY_TRADES": 10,
        "RISK_MAX_DAILY_LOSS_DOLLARS": 6.0,
        "RISK_MAX_CONSECUTIVE_LOSSES": 3,
    }
    data.update(overrides)
    return Settings(**data)


def _request(price=71, count=1):
    return OrderRequest(
        ticker="KXBTC15M-TEST",
        side=Side.YES,
        action="buy",
        count=count,
        limit_price_cents=price,
        client_order_id="test",
        dry_run=False,
    )


def test_risk_allows_entry_under_half_balance():
    manager = RiskManager(_settings(), FakeDb(open_risk=500), FakeClient())
    check = asyncio.run(manager.approve_entry(_request(price=71)))

    assert check.approved is True
    assert check.max_risk_cents == 600
    assert check.projected_risk_cents == 571


def test_risk_blocks_entry_over_half_balance():
    manager = RiskManager(_settings(), FakeDb(open_risk=550), FakeClient())
    check = asyncio.run(manager.approve_entry(_request(price=71)))

    assert check.approved is False
    assert check.reason == "portfolio_risk_limit_exceeded"


def test_risk_blocks_second_open_trade_same_market():
    manager = RiskManager(_settings(), FakeDb(has_open=True), FakeClient())
    check = asyncio.run(manager.approve_entry(_request()))

    assert check.approved is False
    assert check.reason == "open_trade_exists_for_market"
