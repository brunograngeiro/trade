"""FastAPI entrypoint for trade2."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api.schemas import (
    BalanceResponse,
    HealthResponse,
    LiveResponse,
    OrderRequestModel,
    OrderResponse,
)
from app.api.state import AppState
from app.application.collector import Collector
from app.application.analytics import final_minute_probability_stats, readonly_query
from app.application.decision import StrategyDecision
from app.application.market_scanner import MarketScanner
from app.application.order_gateway import OrderGateway
from app.application.risk import RiskManager
from app.config import get_settings
from app.domain.entities import Market, Side, Signal, Tick
from app.infrastructure.coinbase.client import CoinbaseClient
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


VERSION = "0.1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trade2")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.db_path)
    client = KalshiClient(settings)
    coinbase = CoinbaseClient()

    async def on_tick(tick: Tick, market: Market, signal: Signal,
                      decision: StrategyDecision | None) -> None:
        order_results: list = []

        if decision is not None:
            action = decision.action.value
            real_order = settings.enable_real_orders
            dry_run = not real_order

            # Always close on EXIT and FLIP (closing existing exposure is not
            # gated by the entry cap — once we're in, we must be able to get
            # out). The entry cap still gates ENTER and the buy leg of FLIP.
            if action in ("exit", "flip") and decision.previous_side is not None:
                close_price = _exit_limit_price(decision.close_limit_price_cents, settings.strategy_exit_slippage_cents)
                if close_price is None or close_price <= 0:
                    log.warning("Skipping %s sell: no close price (ticker=%s)",
                                action, decision.ticker)
                else:
                    sell_req = state.orders.build(
                        ticker=decision.ticker,
                        side=decision.previous_side,
                        count=settings.default_order_count,
                        limit_price_cents=close_price,
                        action="sell",
                        dry_run=dry_run,
                        time_in_force="immediate_or_cancel",
                        reduce_only=True,
                    )
                    sell_result = await state.orders.submit(sell_req)
                    order_results.append(sell_result)

                    if action == "flip" and not _order_filled(sell_result):
                        log.warning("Skipping flip buy: close leg did not fill (ticker=%s)", decision.ticker)
                        action = "exit"

            # ENTER (fresh entry) or FLIP buy-leg goes through the entry cap.
            should_buy = (
                (action == "enter" or action == "flip")
                and decision.side is not None
                and decision.limit_price_cents is not None
            )
            if should_buy:
                cap = settings.strategy_real_order_cap
                real_count = 0
                if real_order and settings.strategy_real_order_cap_since:
                    real_count = state.db.count_real_orders_since(
                        settings.strategy_real_order_cap_since
                    )
                capped = real_order and cap >= 0 and real_count >= cap
                if capped:
                    log.info("Entry cap reached (%d/%d) — skipping buy for %s",
                             real_count, cap, decision.ticker)
                    state.collector.decisions.set_position(None)
                else:
                    buy_req = state.orders.build(
                        ticker=decision.ticker,
                        side=decision.side,
                        count=settings.default_order_count,
                        limit_price_cents=decision.limit_price_cents,
                        action="buy",
                        dry_run=dry_run,
                        time_in_force="immediate_or_cancel",
                    )
                    risk_check = await state.risk.approve_entry(buy_req)
                    if not risk_check.approved:
                        log.info("Risk blocked buy for %s: %s",
                                 decision.ticker, risk_check.reason)
                        state.collector.decisions.set_position(None)
                    else:
                        buy_result = await state.orders.submit(buy_req)
                        order_results.append(buy_result)
                        if _order_filled(buy_result):
                            state.collector.decisions.set_position(decision.side)
                        else:
                            state.collector.decisions.set_position(None)

            if action == "exit" and order_results:
                close_result = order_results[-1]
                if close_result.request.action == "sell":
                    if _order_filled(close_result) or _order_cancelled(close_result):
                        state.collector.decisions.set_position(None)

        last_order = order_results[-1] if order_results else None
        await state.broadcast({
            "type": "tick",
            "ticker": tick.ticker,
            "captured_at": tick.captured_at.isoformat(),
            "yes_mid": tick.yes_mid,
            "yes_bid": tick.yes_bid,
            "yes_ask": tick.yes_ask,
            "no_bid": tick.no_bid,
            "no_ask": tick.no_ask,
            "phase": market.phase_at(datetime.now(timezone.utc)).value,
            "signal_kind": signal.kind.value,
            "signal_side": signal.side.value,
            "signal_notes": signal.notes,
            "decision_action": decision.action.value if decision else None,
            "decision_side": decision.side.value if decision and decision.side else None,
            "decision_reason": decision.reason if decision else None,
            "decision_orders_count": len(order_results),
            "decision_order_ok": last_order.ok if last_order else None,
            "decision_order_error": last_order.error if last_order else None,
        })

    collector = Collector(settings, client, db, on_tick=on_tick, coinbase=coinbase)
    scanner = None
    if settings.dry_run_market_scanner_enabled:
        scanner = MarketScanner(settings, client, db, coinbase=coinbase)
    orders = OrderGateway(settings, client, db)
    risk = RiskManager(settings, db, client)
    state = AppState(settings=settings, client=client, db=db, collector=collector,
                     orders=orders, risk=risk)
    state.scanner = scanner
    app.state.runtime = state

    collector.start()
    if scanner is not None:
        scanner.start()
    try:
        yield
    finally:
        if scanner is not None:
            await scanner.stop()
        await collector.stop()
        await client.close()
        await coinbase.close()


def _exit_limit_price(close_limit_price_cents: int | None, slippage_cents: int) -> int | None:
    if close_limit_price_cents is None:
        return None
    return max(1, int(close_limit_price_cents) - max(0, int(slippage_cents)))


def _order_filled(result) -> bool:
    if result is None or not result.ok:
        return False
    if result.request.dry_run:
        return True
    raw = result.raw or {}
    order = raw.get("order") if isinstance(raw, dict) else None
    payload = order if isinstance(order, dict) else raw if isinstance(raw, dict) else {}
    status = str(payload.get("status") or "").lower()
    if status in {"executed", "filled"}:
        return True
    filled = _numeric_field(payload, "fill_count_fp", "filled_count", "filled_count_fp", "count_filled")
    if filled is not None:
        return filled > 0
    count = _numeric_field(payload, "count", "count_fp")
    remaining = _numeric_field(payload, "remaining_count", "remaining_count_fp")
    if count is not None and remaining is not None:
        return remaining < count
    return False


def _order_cancelled(result) -> bool:
    if result is None or not result.ok:
        return False
    raw = result.raw or {}
    order = raw.get("order") if isinstance(raw, dict) else None
    payload = order if isinstance(order, dict) else raw if isinstance(raw, dict) else {}
    return str(payload.get("status") or "").lower() in {"canceled", "cancelled"}


def _numeric_field(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value in {None, ""}:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


app = FastAPI(title="trade2", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _state(app: FastAPI) -> AppState:
    return app.state.runtime


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    s = _state(app)
    return HealthResponse(
        status="ok",
        version=VERSION,
        real_orders_enabled=s.settings.enable_real_orders,
    )


@app.get("/health/collector")
async def health_collector() -> dict:
    return _state(app).collector.health.snapshot()


@app.get("/health/scanner")
async def health_scanner() -> dict:
    scanner = getattr(_state(app), "scanner", None)
    if scanner is None:
        return {"enabled": False}
    return {"enabled": True, **scanner.health.snapshot()}


@app.get("/market/live", response_model=LiveResponse)
async def market_live() -> LiveResponse:
    s = _state(app)
    market = s.collector.last_market
    tick = s.collector.last_tick
    signal = s.collector.last_signal
    if market is None or tick is None:
        return LiveResponse(
            ticker=None, title=None, phase=None,
            yes_bid=None, yes_ask=None, no_bid=None, no_ask=None,
            last_price=None, yes_mid=None, captured_at=None,
            last_signal_kind=None, last_signal_side=None,
        )
    phase = market.phase_at(datetime.now(timezone.utc))
    return LiveResponse(
        ticker=market.ticker,
        title=market.title,
        phase=phase.value,
        yes_bid=tick.yes_bid,
        yes_ask=tick.yes_ask,
        no_bid=tick.no_bid,
        no_ask=tick.no_ask,
        last_price=tick.last_price,
        yes_mid=tick.yes_mid,
        captured_at=tick.captured_at.isoformat(),
        last_signal_kind=signal.kind.value if signal else None,
        last_signal_side=signal.side.value if signal else None,
    )


@app.get("/signals/recent")
async def signals_recent(limit: int = 100) -> dict:
    return {"signals": _state(app).db.recent_signals(limit=limit)}


@app.get("/strategy/decisions/recent")
async def strategy_decisions_recent(limit: int = 100) -> dict:
    return {"decisions": _state(app).db.recent_strategy_decisions(limit=limit)}


@app.get("/markets/snapshots/recent")
async def market_snapshots_recent(limit: int = 100) -> dict:
    return {"snapshots": _state(app).db.recent_market_snapshots(limit=limit)}


@app.get("/markets/radar/recent")
async def market_radar_recent(limit: int = 100) -> dict:
    return {"candidates": _state(app).db.recent_market_radar(limit=limit)}


@app.get("/markets/radar/history")
async def market_radar_history(limit: int = 1000) -> dict:
    return {"candidates": _state(app).db.market_radar_history(limit=limit)}


@app.get("/risk/status")
async def risk_status() -> dict:
    return await _state(app).risk.status()


@app.get("/analytics/final-minute")
async def analytics_final_minute() -> dict:
    return final_minute_probability_stats(_state(app).settings.db_path)


@app.post("/analytics/query")
async def analytics_query(body: dict) -> dict:
    sql = str(body.get("sql") or "")
    limit = int(body.get("limit") or 300)
    try:
        return readonly_query(_state(app).settings.db_path, sql, limit=max(1, min(limit, 1000)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/orders/recent")
async def orders_recent(limit: int = 100) -> dict:
    return {"orders": _state(app).db.recent_orders(limit=limit)}


@app.get("/ticks/{ticker}")
async def ticks_for(ticker: str, limit: int = 500) -> dict:
    return {"ticks": _state(app).db.ticks_for(ticker, limit=limit)}


@app.get("/spot/recent")
async def spot_recent(product: str = "BTC-USD", limit: int = 500) -> dict:
    return {"spot": _state(app).db.recent_spot_ticks(product=product, limit=limit)}


@app.get("/portfolio/snapshot")
async def portfolio_snapshot() -> dict:
    s = _state(app)
    balance = await s.client.get_balance()
    positions = await s.client.get_positions(limit=50)
    orders = await s.client.get_orders(limit=20)
    open_positions = [
        p for p in positions.get("market_positions", [])
        if abs(float(p.get("position_fp", 0))) > 0.001
    ]
    resting = [o for o in orders.get("orders", []) if o.get("status") == "resting"]
    return {
        "balance_cents": balance.get("balance"),
        "portfolio_value_cents": balance.get("portfolio_value"),
        "balance_dollars": (balance.get("balance") or 0) / 100.0,
        "open_positions": open_positions,
        "resting_orders": resting,
        "summary": {
            "open_positions_count": len(open_positions),
            "resting_orders_count": len(resting),
        },
    }


@app.get("/portfolio/equity")
async def portfolio_equity(limit: int = 2000) -> dict:
    return {"history": _state(app).db.balance_history(limit=limit)}


@app.get("/portfolio/outcomes")
async def portfolio_outcomes(limit: int = 200) -> dict:
    return {"outcomes": _state(app).db.trade_outcomes(limit=limit)}


@app.get("/portfolio/balance", response_model=BalanceResponse)
async def portfolio_balance() -> BalanceResponse:
    raw = await _state(app).client.get_balance()
    return BalanceResponse(
        ok=bool(raw.get("ok", True)) and raw.get("http_status") is None,
        balance_cents=raw.get("balance"),
        raw=raw,
    )


@app.post("/orders", response_model=OrderResponse)
async def place_order(body: OrderRequestModel) -> OrderResponse:
    s = _state(app)
    request = s.orders.build(
        ticker=body.ticker,
        side=Side(body.side),
        count=body.count,
        limit_price_cents=body.limit_price_cents,
        action=body.action,
        dry_run=body.dry_run,
    )
    result = await s.orders.submit(request)
    return OrderResponse(
        ok=result.ok,
        error=result.error,
        ticker=request.ticker,
        side=request.side.value,
        action=request.action,
        count=request.count,
        limit_price_cents=request.limit_price_cents,
        client_order_id=request.client_order_id,
        dry_run=request.dry_run,
        submitted_at=result.submitted_at.isoformat(),
        raw=result.raw,
    )


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    s = _state(app)
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    s.ws_clients.add(q)
    try:
        while True:
            msg = await q.get()
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    finally:
        s.ws_clients.discard(q)
