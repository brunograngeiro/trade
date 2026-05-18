"""Data retention / pruning for trade2.

VPS is small — keep granular data only as long as we use it for backtest/dashboard.

Policy (defaults):
  - ticks            : keep last 14 days
  - spot_ticks       : keep last 14 days
  - signals          : keep last 7 days  (mostly noise — needed only for live audit)
  - balance_snapshots: KEEP ALL (~300 rows/day, trivial)
  - market_resolutions: KEEP ALL (ground truth, precious)
  - orders           : KEEP ALL (audit trail)
  - trade_outcomes   : KEEP ALL

After pruning, VACUUM to reclaim disk.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass


log = logging.getLogger(__name__)


@dataclass
class RetentionPolicy:
    ticks_days: int = 14
    spot_ticks_days: int = 14
    signals_days: int = 7
    vacuum: bool = True


def prune(db_path: str, policy: RetentionPolicy | None = None) -> dict:
    pol = policy or RetentionPolicy()
    result: dict[str, int] = {}

    # Use isolation_level=None so DELETEs are committed before VACUUM (which requires no tx)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        result["ticks_deleted"] = _delete_old(
            conn, "ticks", "captured_at", pol.ticks_days)
        result["spot_ticks_deleted"] = _delete_old(
            conn, "spot_ticks", "captured_at", pol.spot_ticks_days)
        result["signals_deleted"] = _delete_old(
            conn, "signals", "captured_at", pol.signals_days)

        if pol.vacuum:
            log.info("VACUUM started")
            conn.execute("VACUUM")
            result["vacuumed"] = 1
    finally:
        conn.close()
    log.info("retention prune result: %s", result)
    return result


def _delete_old(conn: sqlite3.Connection, table: str, ts_col: str,
                keep_days: int) -> int:
    cur = conn.execute(
        f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    n = cur.rowcount or 0
    log.info("pruned %d rows from %s (keep_days=%d)", n, table, keep_days)
    return n
