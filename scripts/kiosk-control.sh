#!/usr/bin/env bash
set -euo pipefail
case "${1:-status}" in
  start|stop|restart|status)
    exec systemctl "${1:-status}" tm-camera-kiosk.service
    ;;
  *) echo "Usage: sudo bash scripts/kiosk-control.sh [start|stop|restart|status]" >&2; exit 2 ;;
esac
