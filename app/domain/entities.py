"""Domain entities — pure data classes, no IO.

Kalshi YES/NO mechanics (essential context):
  - Each market is binary; resolves to YES or NO at close_time
  - You BUY YES at `yes_ask` cents (1-99); pays $1 if YES wins, $0 if NO wins
  - You BUY NO at `no_ask` cents (1-99); pays $1 if NO wins, $0 if YES wins
  - `yes_ask + no_bid` and `no_ask + yes_bid` should ≈ 1.00 (no arb)
  - To EXIT before settlement, SELL at the opposite side's bid
      (sell YES = buy NO + cancellation in book); we don't use exits today
  - Fees on entry: ceil(0.07 × count × P × (1-P)) in dollars (`kalshi_fee_dollars`)
  - No fees on payout. No maker rebate.
  - "Contrarian" = signal says YES → buy NO (fade the move). Sometimes called
    counter-trend or mean-reversion entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class MarketPhase(str, Enum):
    EARLY = "early"
    MIDDLE = "middle"
    LATE = "late"
    EXPIRED = "expired"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class SignalKind(str, Enum):
    EXPLOSION = "explosion"
    PLATEAU = "plateau"
    NONE = "none"


@dataclass(frozen=True)
class Market:
    ticker: str
    title: str
    open_time: datetime
    close_time: datetime
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    status: str

    def phase_at(self, when: datetime) -> MarketPhase:
        total = (self.close_time - self.open_time).total_seconds()
        if total <= 0:
            return MarketPhase.EXPIRED
        elapsed = (when - self.open_time).total_seconds()
        if elapsed < 0:
            return MarketPhase.EARLY
        ratio = elapsed / total
        if ratio >= 1.0:
            return MarketPhase.EXPIRED
        if ratio < 0.34:
            return MarketPhase.EARLY
        if ratio < 0.67:
            return MarketPhase.MIDDLE
        return MarketPhase.LATE


@dataclass(frozen=True)
class Tick:
    ticker: str
    captured_at: datetime
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: int | None

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return self.last_price
        return (self.yes_bid + self.yes_ask) / 2.0


@dataclass(frozen=True)
class Signal:
    ticker: str
    captured_at: datetime
    kind: SignalKind
    side: Side
    phase: MarketPhase
    probability: float
    delta: float
    notes: str


@dataclass(frozen=True)
class OrderRequest:
    ticker: str
    side: Side
    action: str  # "buy" | "sell"
    count: int
    limit_price_cents: int
    client_order_id: str
    dry_run: bool


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    request: OrderRequest
    raw: dict
    error: str | None
    submitted_at: datetime

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
