"""Kalshi REST client (httpx) with signed and public endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.infrastructure.kalshi.signer import sign_request


log = logging.getLogger(__name__)


class KalshiClient:
    """Thin wrapper around Kalshi v2 REST API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._signed_prefix = urlparse(settings.kalshi_base_url).path or "/trade-api/v2"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ---------- Public ----------

    async def get_markets(self, *, series_ticker: str | None = None, status: str = "open",
                          limit: int = 100) -> dict:
        params: dict[str, Any] = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await self._public("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        return await self._public("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        return await self._public("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ---------- Signed ----------

    async def get_balance(self) -> dict:
        return await self._signed("GET", "/portfolio/balance")

    async def get_positions(self, limit: int = 100) -> dict:
        return await self._signed("GET", "/portfolio/positions", params={"limit": limit})

    async def get_orders(self, limit: int = 100) -> dict:
        return await self._signed("GET", "/portfolio/orders", params={"limit": limit})

    async def get_fills(self, limit: int = 100) -> dict:
        return await self._signed("GET", "/portfolio/fills", params={"limit": limit})

    async def place_order(self, payload: dict) -> dict:
        return await self._signed("POST", "/portfolio/orders", json_body=payload)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._signed("DELETE", f"/portfolio/orders/{order_id}")

    # ---------- Internals ----------

    async def _public(self, method: str, path: str, *, params: dict | None = None) -> dict:
        url = f"{self.settings.kalshi_base_url}{path}"
        resp = await self._client.request(method, url, params=params, headers={"Accept": "application/json"})
        return self._parse(resp)

    async def _signed(self, method: str, path: str, *,
                      params: dict | None = None,
                      json_body: dict | None = None) -> dict:
        if not self.settings.kalshi_api_key_id:
            return {"ok": False, "error": "credentials_missing"}

        signed_path = f"{self._signed_prefix}{path}"
        timestamp, signature = sign_request(
            self.settings.kalshi_private_key_path, method, signed_path
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.settings.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
        url = f"{self.settings.kalshi_base_url}{path}"
        resp = await self._client.request(
            method, url,
            params=params,
            content=json.dumps(json_body) if json_body is not None else None,
            headers=headers,
        )
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> dict:
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            return {"ok": False, "http_status": resp.status_code, **payload}
        if isinstance(payload, dict):
            return {"ok": True, **payload}
        return {"ok": True, "data": payload}
