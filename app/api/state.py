"""Shared runtime state attached to the FastAPI app."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.application.collector import Collector
from app.application.order_gateway import OrderGateway
from app.config import Settings
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


@dataclass
class AppState:
    settings: Settings
    client: KalshiClient
    db: Database
    collector: Collector
    orders: OrderGateway
    ws_clients: set[asyncio.Queue] = field(default_factory=set)

    async def broadcast(self, message: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in list(self.ws_clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.ws_clients.discard(q)
