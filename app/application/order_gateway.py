"""Order placement with safety gates."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.config import Settings
from app.domain.entities import OrderRequest, OrderResult, Side
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


log = logging.getLogger(__name__)


class OrderGateway:
    """Builds, validates and (optionally) submits orders to Kalshi.

    Three layers of safety:
      1. dry_run flag (request-level)
      2. settings.enable_real_orders (global kill switch)
      3. settings.max_order_cost_cents (cost gate)
    """

    def __init__(self, settings: Settings, client: KalshiClient, db: Database) -> None:
        self.settings = settings
        self.client = client
        self.db = db

    def build(self, *, ticker: str, side: Side, count: int,
              limit_price_cents: int, action: str = "buy",
              dry_run: bool | None = None,
              time_in_force: str | None = None,
              reduce_only: bool = False) -> OrderRequest:
        return OrderRequest(
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            limit_price_cents=limit_price_cents,
            client_order_id=f"trade2-{uuid.uuid4().hex[:16]}",
            dry_run=bool(dry_run) if dry_run is not None else not self.settings.enable_real_orders,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
        )

    async def submit(self, request: OrderRequest) -> OrderResult:
        cost = request.limit_price_cents * request.count
        if request.action == "buy" and cost > self.settings.max_order_cost_cents:
            return self._record(request, ok=False, raw={},
                                error=f"cost_exceeds_max:{cost}>{self.settings.max_order_cost_cents}")

        if request.dry_run:
            payload = self._payload(request)
            log.info("[dry-run] would submit: %s", payload)
            return self._record(request, ok=True, raw={"dry_run": True, "payload": payload}, error=None)

        if not self.settings.enable_real_orders:
            return self._record(request, ok=False, raw={},
                                error="real_orders_disabled_by_settings")

        payload = self._payload(request)
        raw = await self.client.place_order(payload)
        ok = bool(raw.get("ok", True)) and raw.get("http_status") is None
        return self._record(request, ok=ok, raw=raw,
                            error=None if ok else str(raw.get("error") or raw))

    def _payload(self, request: OrderRequest) -> dict:
        payload = {
            "ticker": request.ticker,
            "client_order_id": request.client_order_id,
            "side": request.side.value,
            "action": request.action,
            "count": request.count,
            "type": "limit",
        }
        if request.time_in_force:
            payload["time_in_force"] = request.time_in_force
        if request.reduce_only:
            payload["reduce_only"] = True
        if request.side == Side.YES:
            payload["yes_price"] = request.limit_price_cents
        else:
            payload["no_price"] = request.limit_price_cents
        return payload

    def _record(self, request: OrderRequest, *, ok: bool, raw: dict,
                error: str | None) -> OrderResult:
        result = OrderResult(
            ok=ok,
            request=request,
            raw=raw,
            error=error,
            submitted_at=datetime.now(timezone.utc),
        )
        try:
            self.db.save_order(result)
        except Exception:  # noqa: BLE001 — best-effort persistence
            log.exception("Failed to persist order")
        return result
