#!/usr/bin/env bash
# Install trade2 systemd units. Idempotent.
set -euo pipefail

UNITS=(trade2-api.service trade2-resolver.service trade2-dashboard.service)
SRC_DIR="$(dirname "$(readlink -f "$0")")"
DST_DIR="/etc/systemd/system"

for u in "${UNITS[@]}"; do
  sudo cp "$SRC_DIR/$u" "$DST_DIR/$u"
  echo "installed $u"
done

sudo systemctl daemon-reload
for u in "${UNITS[@]}"; do
  sudo systemctl enable "$u"
done

echo
echo "to start:    sudo systemctl start ${UNITS[*]}"
echo "to status:   systemctl status ${UNITS[0]}"
echo "to journal:  journalctl -u ${UNITS[0]} -f"
