#!/usr/bin/env bash
set -uo pipefail
echo '== Installed deployment =='
cat /etc/tm-camera/deployment.json 2>/dev/null || true
echo
echo '== Server and kiosk services =='
systemctl --no-pager --full status tm-camera-monitor.service tm-camera-kiosk.service || true
echo
echo '== Graphical sessions =='
loginctl list-sessions || true
echo
echo '== Dashboard recording status =='
curl -fsS --max-time 5 http://127.0.0.1:8080/api/recording/status | python3 -m json.tool || true
echo
echo '== ESP32 status =='
curl -fsS --max-time 5 http://127.0.0.1:8080/api/esp32/status | python3 -m json.tool || true
echo
echo '== Kiosk startup log =='
journalctl -u tm-camera-kiosk.service -n 50 --no-pager || true
echo
echo '== Application log =='
journalctl -u tm-camera-monitor.service -n 50 --no-pager || true
