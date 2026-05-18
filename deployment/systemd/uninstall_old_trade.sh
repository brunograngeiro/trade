#!/usr/bin/env bash
# Stop & disable old `trade` systemd units. Run ONLY after confirming with the user.
# This script is intentionally idempotent and verbose.
set -euo pipefail

OLD=(
  trade.service
  trade-collector.service
  trade-dashboard.service
  trade-auto-testnet.service
  trade-kalshi-real.service
  trade-daily-report.service
  trade-daily-report.timer
  trade-data-retention.service
  trade-data-retention.timer
  trade-strategy-refresh.service
  trade-strategy-refresh.timer
)

echo "Stopping old trade units..."
for u in "${OLD[@]}"; do
  if systemctl list-unit-files | grep -q "^$u"; then
    sudo systemctl stop "$u" || true
    sudo systemctl disable "$u" || true
    echo "  stopped+disabled: $u"
  else
    echo "  skipped (not installed): $u"
  fi
done

echo
echo "Status check:"
for u in trade.service trade-collector.service trade-kalshi-real.service; do
  printf "  %-32s " "$u"
  systemctl is-active "$u" 2>/dev/null || true
done

echo
echo "Done. Files in /etc/systemd/system are NOT deleted; remove manually if desired."
