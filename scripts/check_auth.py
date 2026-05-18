"""Verify Kalshi authentication and list current BTC 15min market."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.market_discovery import current_market  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.infrastructure.kalshi.client import KalshiClient  # noqa: E402


async def main() -> int:
    settings = get_settings()
    async with KalshiClient(settings) as client:
        print(f"Base URL: {settings.kalshi_base_url}")
        print(f"Key ID  : {settings.kalshi_api_key_id}")
        print(f"Key file: {settings.kalshi_private_key_path} "
              f"(exists={Path(settings.kalshi_private_key_path).exists()})")
        print()

        balance = await client.get_balance()
        print("=== /portfolio/balance ===")
        print(json.dumps(balance, indent=2, default=str))
        print()

        positions = await client.get_positions(limit=5)
        print("=== /portfolio/positions (5) ===")
        print(json.dumps(positions, indent=2, default=str))
        print()

        market = await current_market(client, settings.kalshi_series_ticker)
        if market is None:
            print("No open KXBTC15M market found.")
            return 1

        print("=== Active market ===")
        print(f"  ticker     : {market.ticker}")
        print(f"  title      : {market.title}")
        print(f"  window     : {market.open_time}  →  {market.close_time}")
        print(f"  yes_bid    : {market.yes_bid}")
        print(f"  yes_ask    : {market.yes_ask}")
        print(f"  no_bid     : {market.no_bid}")
        print(f"  no_ask     : {market.no_ask}")
        print(f"  last_price : {market.last_price}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
