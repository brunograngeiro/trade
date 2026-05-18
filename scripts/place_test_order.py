"""Place a small real order (~$0.50) on the current BTC 15min market.

Safety:
  - Auto-discovers current market
  - Buys the cheaper side (or the side you specify) at best ask
  - Caps at MAX_ORDER_COST_CENTS
  - Requires explicit `--confirm-real` flag to actually send

Usage:
    python scripts/place_test_order.py                   # dry-run
    python scripts/place_test_order.py --confirm-real    # send real order
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.market_discovery import current_market  # noqa: E402
from app.application.order_gateway import OrderGateway  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain.entities import Side  # noqa: E402
from app.infrastructure.db.sqlite import Database  # noqa: E402
from app.infrastructure.kalshi.client import KalshiClient  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-real", action="store_true",
                    help="Actually send the order (default is dry-run)")
    ap.add_argument("--side", choices=["yes", "no"], default="yes")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--max-price-cents", type=int, default=None,
                    help="Override limit price cap")
    args = ap.parse_args()

    settings = get_settings()
    # If sending real, force enable_real_orders for this process only
    if args.confirm_real:
        settings.enable_real_orders = True

    db = Database(settings.db_path)
    async with KalshiClient(settings) as client:
        market = await current_market(client, settings.kalshi_series_ticker)
        if market is None:
            print("No open market found", file=sys.stderr)
            return 1

        side = Side(args.side)
        if side == Side.YES:
            ref_price_dollars = market.yes_ask or market.yes_bid
        else:
            ref_price_dollars = market.no_ask or market.no_bid

        if ref_price_dollars is None:
            print("No price available for chosen side", file=sys.stderr)
            return 1

        ref_cents = max(1, int(round(ref_price_dollars * 100)))
        cap = args.max_price_cents or settings.max_order_cost_cents // max(1, args.count)
        limit_cents = min(ref_cents, cap)

        gateway = OrderGateway(settings, client, db)
        request = gateway.build(
            ticker=market.ticker, side=side, count=args.count,
            limit_price_cents=limit_cents,
            dry_run=not args.confirm_real,
        )

        print("=== Order request ===")
        print(f"  ticker       : {request.ticker}")
        print(f"  side         : {request.side.value}")
        print(f"  count        : {request.count}")
        print(f"  limit_cents  : {request.limit_price_cents} (ref={ref_cents}¢)")
        print(f"  cost_estimate: {request.limit_price_cents * request.count}¢")
        print(f"  dry_run      : {request.dry_run}")
        print(f"  client_id    : {request.client_order_id}")
        print()

        result = await gateway.submit(request)
        print("=== Result ===")
        print(f"  ok    : {result.ok}")
        print(f"  error : {result.error}")
        print(f"  raw   : {json.dumps(result.raw, indent=2, default=str)}")
        return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
