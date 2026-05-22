"""Portfolio risk checks for real order entries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.domain.entities import OrderRequest
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


@dataclass(frozen=True)
class RiskCheck:
    approved: bool
    reason: str
    balance_cents: int
    max_risk_cents: int
    open_risk_cents: int
    requested_risk_cents: int
    projected_risk_cents: int
    trades_today: int
    daily_realized_pnl_dollars: float
    consecutive_losses: int


class RiskManager:
    def __init__(self, settings: Settings, db: Database, client: KalshiClient) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.last_check: RiskCheck | None = None

    async def approve_entry(self, request: OrderRequest) -> RiskCheck:
        requested = request.limit_price_cents * request.count
        if request.dry_run or request.action != "buy":
            check = RiskCheck(True, "dry_run_or_non_entry", 0, 0, 0, requested,
                              requested, 0, 0.0, 0)
            self.last_check = check
            return check

        balance_cents = await self._balance_cents()
        summary = self.db.risk_summary()
        open_risk = int(summary["open_risk_cents"])
        projected = open_risk + requested
        max_risk = int(balance_cents * self.settings.risk_max_balance_fraction)
        trades_today = int(summary["trades_today"])
        daily_pnl = float(summary["daily_realized_pnl_dollars"])
        loss_streak = int(summary["consecutive_losses"])

        approved = True
        reason = "approved"
        if not self.settings.risk_manager_enabled:
            reason = "risk_manager_disabled"
        elif self.db.has_open_real_trade(request.ticker):
            approved = False
            reason = "open_trade_exists_for_market"
        elif requested > balance_cents:
            approved = False
            reason = "requested_risk_exceeds_cash_balance"
        elif projected > max_risk:
            approved = False
            reason = "portfolio_risk_limit_exceeded"
        elif self.settings.risk_max_daily_trades > 0 and trades_today >= self.settings.risk_max_daily_trades:
            approved = False
            reason = "daily_trade_limit_reached"
        elif (
            self.settings.risk_max_daily_loss_dollars > 0
            and daily_pnl <= -self.settings.risk_max_daily_loss_dollars
        ):
            approved = False
            reason = "daily_loss_limit_reached"
        elif (
            self.settings.risk_max_consecutive_losses > 0
            and loss_streak >= self.settings.risk_max_consecutive_losses
        ):
            approved = False
            reason = "consecutive_loss_limit_reached"

        check = RiskCheck(
            approved=approved,
            reason=reason,
            balance_cents=balance_cents,
            max_risk_cents=max_risk,
            open_risk_cents=open_risk,
            requested_risk_cents=requested,
            projected_risk_cents=projected,
            trades_today=trades_today,
            daily_realized_pnl_dollars=round(daily_pnl, 4),
            consecutive_losses=loss_streak,
        )
        self.last_check = check
        return check

    async def status(self) -> dict[str, Any]:
        balance_cents = await self._balance_cents()
        summary = self.db.risk_summary()
        max_risk = int(balance_cents * self.settings.risk_max_balance_fraction)
        return {
            "enabled": self.settings.risk_manager_enabled,
            "balance_cents": balance_cents,
            "balance_dollars": balance_cents / 100.0,
            "max_balance_fraction": self.settings.risk_max_balance_fraction,
            "max_risk_cents": max_risk,
            "max_risk_dollars": max_risk / 100.0,
            "open_risk_cents": summary["open_risk_cents"],
            "open_risk_dollars": summary["open_risk_cents"] / 100.0,
            "risk_used_fraction": summary["open_risk_cents"] / max_risk if max_risk > 0 else 0.0,
            "trades_today": summary["trades_today"],
            "max_daily_trades": self.settings.risk_max_daily_trades,
            "daily_realized_pnl_dollars": summary["daily_realized_pnl_dollars"],
            "max_daily_loss_dollars": self.settings.risk_max_daily_loss_dollars,
            "consecutive_losses": summary["consecutive_losses"],
            "max_consecutive_losses": self.settings.risk_max_consecutive_losses,
            "open_trades": summary["open_trades"],
            "last_check": self.last_check.__dict__ if self.last_check else None,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _balance_cents(self) -> int:
        raw = await self.client.get_balance()
        try:
            return int(raw.get("balance") or 0)
        except (TypeError, ValueError):
            return 0
