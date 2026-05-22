"""Run the daily Kalshi market radar once."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.market_radar import run_market_radar
from app.config import get_settings
from app.infrastructure.db.sqlite import Database
from app.infrastructure.kalshi.client import KalshiClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def main() -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    async with KalshiClient(settings) as client:
        result = await run_market_radar(settings, client, db)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
