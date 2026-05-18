"""Coinbase Exchange public BTC-USD ticker client.

Aligns with Kalshi's BRTI settlement source (CF Benchmarks uses Coinbase among others).
Public REST: https://api.exchange.coinbase.com/products/BTC-USD/ticker

No auth needed for ticker / time / book endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


COINBASE_BASE = "https://api.exchange.coinbase.com"


@dataclass(frozen=True)
class SpotTick:
    product: str
    captured_at: datetime
    price: float
    bid: float | None
    ask: float | None
    volume_24h: float | None


class CoinbaseClient:
    def __init__(self, client: httpx.AsyncClient | None = None,
                 product: str = "BTC-USD") -> None:
        self._client = client or httpx.AsyncClient(timeout=8.0)
        self._owns_client = client is None
        self.product = product

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "CoinbaseClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def get_ticker(self) -> SpotTick | None:
        resp = await self._client.get(
            f"{COINBASE_BASE}/products/{self.product}/ticker",
            headers={"User-Agent": "trade2-coinbase/0.1"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        try:
            captured = datetime.fromisoformat(data["time"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except (KeyError, ValueError):
            captured = datetime.now(timezone.utc)
        return SpotTick(
            product=self.product,
            captured_at=captured,
            price=float(data["price"]),
            bid=float(data["bid"]) if data.get("bid") else None,
            ask=float(data["ask"]) if data.get("ask") else None,
            volume_24h=float(data["volume"]) if data.get("volume") else None,
        )
