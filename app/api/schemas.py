"""Pydantic schemas for API request/response."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    real_orders_enabled: bool


class LiveResponse(BaseModel):
    ticker: str | None
    title: str | None
    phase: str | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    yes_mid: float | None
    captured_at: str | None
    last_signal_kind: str | None
    last_signal_side: str | None


class OrderRequestModel(BaseModel):
    ticker: str
    side: Literal["yes", "no"] = "yes"
    action: Literal["buy", "sell"] = "buy"
    count: int = Field(default=1, ge=1, le=10)
    limit_price_cents: int = Field(ge=1, le=99)
    dry_run: bool | None = None


class OrderResponse(BaseModel):
    ok: bool
    error: str | None
    ticker: str
    side: str
    action: str
    count: int
    limit_price_cents: int
    client_order_id: str
    dry_run: bool
    submitted_at: str
    raw: dict


class BalanceResponse(BaseModel):
    ok: bool
    balance_cents: int | None = None
    raw: dict
