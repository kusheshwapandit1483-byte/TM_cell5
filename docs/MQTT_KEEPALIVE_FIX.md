# MQTT keepalive correction for raspberrypibase

The previous monitor sent PINGREQ only when socket.recv timed out. With status
messages arriving every second, reads did not time out, so the monitor could send
no packets after SUBSCRIBE despite advertising a 30-second keepalive. MQTT 3.1.1
requires CLIENT-to-broker traffic within that interval; receiving publications
does not fulfil that obligation. A broker may therefore close this connection.

The correction checks a monotonic 20-second ping timer independently of incoming
packets. A stable per-monitor random alphanumeric client ID also avoids collisions
between Pi devices/processes that happen to use the same numeric PID.

This is a demonstrated code defect consistent with the reported MQTT socket
closure; it is not proof that every observed disconnection has that cause. The
snapshot on AEGIX301 had recording stopped, zero occupancy, and a disconnected
MQTT client. It does not prove which earlier outage produced a particular long
clip. Unknown/stale status still holds the previous recording decision; this
policy is not changed silently. Entry/exit/ESP32 firmware and the 35 GB cap are
unchanged. The native player and its extension are also unchanged.

Update the installed checkout as its administrator:

```bash
cd /home/aegixcore/raspberrypibase &&
git fetch origin fix/raspberrypibase-mqtt-keepalive &&
git switch --no-track -c fix/raspberrypibase-mqtt-keepalive FETCH_HEAD &&
sudo python3 scripts/apply-mqtt-fix.py
```

If that local branch already exists, switch to it and use git pull --ff-only
origin fix/raspberrypibase-mqtt-keepalive before running the application script.
Local edits are never forcibly discarded. The script verifies the installed
monitor directory and restarts only the monitor. This briefly interrupts any
active recording, using its existing graceful stop/finalisation. The archive
path and completed recordings are retained. No kiosk/LightDM installation is run.

Verification observes at least 70 continuous seconds of fresh MQTT data, exceeding
the previous broker keepalive timeout. It reports a failure if the connection
still drops, instead of reporting that a successful service start proves health.
It does not claim long-term operation or physical network faults are fixed.

Before the observation window, the script imports the existing session discovery as
the actual kiosk service user. On Wayland, it lists display output power state,
tries wlopm --on '*', then lists output state again. This is a diagnostic wake,
not proof that screen blanking caused the original black display. It does not
change display resolution, disable security, or replace the operator account.
If waking succeeds, the remaining task is to identify the idle/power process and
its configuration. If output was already on, investigate the browser/compositor
and physical display instead. Missing wlopm/session is reported explicitly.

Protocol reference:
https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html#_Toc398718030

Tests: python3 -m unittest discover -s tests -v. New regression tests simulate a
65-second continuous publish stream and an idle stream, requiring periodic pings
in both, and check unique client IDs with identical simulated process IDs.
