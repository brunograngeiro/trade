"""One-shot data retention pruner. Run from a systemd timer (daily)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.data_retention import RetentionPolicy, prune  # noqa: E402
from app.config import get_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks-days", type=int, default=14)
    ap.add_argument("--spot-days", type=int, default=14)
    ap.add_argument("--signals-days", type=int, default=7)
    ap.add_argument("--no-vacuum", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    db = settings.db_path
    pol = RetentionPolicy(
        ticks_days=args.ticks_days,
        spot_ticks_days=args.spot_days,
        signals_days=args.signals_days,
        vacuum=not args.no_vacuum,
    )

    import os
    before_mb = os.path.getsize(db) / 1024 / 1024
    print(f"DB before: {before_mb:.1f} MB")

    result = prune(db, pol)

    after_mb = os.path.getsize(db) / 1024 / 1024
    print(f"DB after : {after_mb:.1f} MB (Δ {after_mb - before_mb:+.1f} MB)")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
