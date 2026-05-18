"""Stand-alone process that syncs market resolutions every 5 minutes."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.resolution_sync import run_loop  # noqa: E402
from app.config import get_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> int:
    settings = get_settings()
    asyncio.run(run_loop(settings, interval_seconds=300))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
