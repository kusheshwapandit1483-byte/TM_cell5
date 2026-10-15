#!/usr/bin/env bash
set -euo pipefail
echo "Stopping the kiosk browser. Recording server remains running."
systemctl stop tm-camera-kiosk.service
echo "For temporary use: sudo systemctl start tm-camera-kiosk.service"
echo "To restore pre-install login/configuration, use --rollback as described in README.md."
