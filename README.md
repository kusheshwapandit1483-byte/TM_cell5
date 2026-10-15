# Raspberry Pi Base: occupancy recording and separate kiosk user

This is the self-contained deployment for the special Cell 5 Raspberry Pi.
The recording server runs as your administrator/application account (aegixcore).
The screen automatically logs in as a separate operator account and opens the
dashboard. ESP32 occupancy controls recording; the archive is capped at 35 GB.

## Install in one pass

Use the existing Raspberry Pi OS Desktop device, with its existing MediaMTX
camera relay running and ESP32 connected. Log in as aegixcore through SSH or
the text console. Do not run this as operator or from a root login shell.

```bash
git clone --single-branch --branch raspberrypibase-occupancy-35gb https://github.com/kusheshwapandit1483-byte/TM_cell5.git /home/aegixcore/raspberrypibase
cd /home/aegixcore/raspberrypibase
sudo bash scripts/setup-kiosk.sh --reboot
```

Run each line only if the preceding line succeeds. If that destination already
exists, do not delete or overwrite it; use the existing checkout if it tracks
this branch, or clone into a different empty directory.

The installer handles packages, tests, configuration backup, server and browser
services, separate user creation, graphical autologin, health checks and reboot.
No manual DISPLAY export or separate service-creation commands are needed.
Without --reboot, it completes setup and prints the reboot instruction instead.
The new login configuration takes effect on reboot; graphical visibility must
be checked on the actual screen then.

The installer checks the RTSP video stream and ESP32 TCP connection before
changing kiosk configuration. It refuses a missing/unsupported desktop session
or an existing non-LightDM display manager rather than replacing that setup.
It does not download or replace MediaMTX, flash the ESP32, or import reCamera flows.
For a local relay it enables the existing MediaMTX system service. If MediaMTX
was launched manually, it registers that running executable, existing config,
working directory and MTX environment overrides as a boot service. Camera
configuration is not rewritten. If that boot configuration cannot be identified,
setup stops before changing kiosk configuration instead of risking a blank
camera after reboot. It assumes a single local MediaMTX deployment.

Default endpoints:
- Dashboard: http://127.0.0.1:8080
- Existing RTSP relay: rtsp://127.0.0.1:8554/pramacam
- ESP32 MQTT: 192.168.10.1:1883, eagle/system/status

An existing base server unit, its environment settings and drop-ins are retained.
A managed drop-in points the service to this checkout. The existing archive
directory is reused when found. An explicit RECORDINGS_DIR overrides it. If no
archive exists, this checkout's recordings directory is used. The installer
verifies write access as the application user before proceeding.

Existing camera and ESP32 environment settings are used for preflight when
available; an already-running server's effective environment is inspected too.
For a custom endpoint not available from the old service, supply the matching
RECORDING_SOURCE or ESP32_MQTT_HOST/PORT/TOPIC to sudo. Configure credentials in
the service environment rather than posting them in terminal output or chat.
An inactive service using custom EnvironmentFile settings may require explicit
preflight overrides. The dashboard port is configured to 8080.

Setup restarts the recording service and briefly interrupts recording. Oldest
completed clips can be deleted immediately if the reused archive exceeds 35 GB.
Back up any recordings you need to keep outside this archive before installing.

## How the display starts

LightDM logs into operator using an installed labwc or LXDE session. SESSION_NAME
can explicitly select another installed desktop session. The existing admin
account is kept separate; never set KIOSK_USER to the administrator account.

The desktop publishes its actual X11/Wayland environment through a small capture
hook. The browser service waits for that user's session and the dashboard. It
uses the actual display/socket and chooses Chromium's matching ozone platform.
It does not assume DISPLAY=:0 or /home/operator/.Xauthority. A session lock
prevents multiple instances of this launcher. Chromium is relaunched after exit.

Existing unrelated LXDE/labwc startup lines are retained. Old TM launcher lines
are replaced with the session capture hook, so desktop autostart and the service
do not compete to launch Chromium. Kiosk settings are written under the kiosk
user's actual home. The account's password login is locked and sudo membership
is removed. This is a separate-user kiosk setup, not complete desktop lockdown.

## Stop and start the kiosk

From SSH or an admin console:

```bash
sudo systemctl stop tm-camera-kiosk.service
sudo systemctl start tm-camera-kiosk.service
```

Stopping the browser leaves the recording server running. The next start opens
it on the existing operator desktop. If no operator desktop is logged in yet,
the service waits until the configured graphical login starts.

Press Ctrl+Alt+F2 and log in as the administrator for local maintenance. Switch
back to the graphical console with Ctrl+Alt+F1 or Ctrl+Alt+F7, depending on the OS.

## Verify after reboot

```bash
cd /home/aegixcore/raspberrypibase
sudo bash scripts/check-kiosk.sh
```

Both services should be active. The recording API should show mode=occupied and
storageLimitGB=35. ESP32 status must be connected, recent and contain occupancy.
Test an entry and a confirmed clear exit: recording starts while occupied,
stops when clear, and completed clips play in Recordings. Live view remains
available while empty. Check the browser on the physical screen; service-active
alone cannot prove a visible picture. Review diagnostics before sharing them
because the recording source URL can include camera credentials.

## Recording policy

Positive effective ESP32 occupancy starts FFmpeg stream-copy recording. This
includes an inside-camera presence correction if the ESP32 publishes it as
effective occupancy. The Pi does not recalculate max entry/max exit itself.
Zero stops recording after published presence-clear/vacancy-clear flags finish;
with firmware that omits those flags, a fresh zero stops it directly.

Missing, disconnected, malformed, retained or over-five-second-old status does
not start a new recording; an active recording holds its previous decision
during a status loss. There is no pre-entry buffer. Source quality is preserved
with stream copy, and visits split into at most 30-minute MP4 segments.

The archive budget is 35,000,000,000 bytes including playback cache and support
files. Cleanup deletes the oldest completed clips first, with no age expiry.
Active clips are protected and finalized before size reclamation if needed.
Checks run every 10 seconds and on stop/cache events; brief overshoot is possible.

## Backup and restore

Setup records affected configuration files, their ownership/modes and prior
service enabled/active states under /var/backups/tm-camera/<backup-id> before
rewriting them. The installed deployment points to its backup in
/etc/tm-camera/deployment.json. A failure after configuration begins triggers
automatic configuration restoration; if that fails, the manual command is
printed. To roll back a completed deployment manually:

```bash
sudo python3 scripts/deploy.py --rollback /var/backups/tm-camera/BACKUP_ID
sudo reboot
```

Restore reinstates saved configuration and service states. It does not undo
installed packages or account/group changes, or recover pruned recordings.
Do not use rollback to restore archive content. Keep the backup private: old
service files can contain credentials. The installer never restarts LightDM
mid-session; a requested reboot applies the new login configuration.

## Source and tests

This branch is based on TM_cell5/original commit f5499f9. Its occupancy recorder
and recorder tests come from TM_Seating_sanand/feature/occupancy-recording-35gb,
commit 9c17518. The kiosk installer/runner are revised for this separate-user
deployment. Other repositories and their source branches are unchanged.

```bash
python3 -m unittest discover -s tests -v
```

The tests exercise recording/retention, real FFmpeg segmentation, session/socket
selection, LightDM/autostart migration, directory ownership and backup restoration.
They do not emulate a physical Pi desktop; initial screen/camera verification on
the device remains required. Optional Pi-side AI inference is disabled by default.
