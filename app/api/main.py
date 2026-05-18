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
from app.application.order_gateway import OrderGateway
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

    async def on_tick(tick: Tick, market: Market, signal: Signal) -> None:
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
        })

    collector = Collector(settings, client, db, on_tick=on_tick, coinbase=coinbase)
    orders = OrderGateway(settings, client, db)
    state = AppState(settings=settings, client=client, db=db, collector=collector, orders=orders)
    app.state.runtime = state

    collector.start()
    try:
        yield
    finally:
        await collector.stop()
        await client.close()
        await coinbase.close()


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
