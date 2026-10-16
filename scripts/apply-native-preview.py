#!/usr/bin/env python3
"""Install frame observation around the working local iframe, restarting only the browser."""
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DROPIN = Path("/etc/systemd/system/tm-camera-kiosk.service.d/96-native-player.conf")
EXTENSION = Path("/usr/local/lib/tm-camera/native-player-observer")
WRAPPER = Path("/usr/local/lib/tm-camera/native-kiosk.py")
RUNNER = Path("/usr/local/lib/tm-camera/kiosk.py")
BACKUP_ROOT = Path("/var/backups/tm-camera")


def show(prop):
    return subprocess.check_output(["systemctl", "show", "tm-camera-kiosk.service", "-p", prop, "--value"], text=True).strip()


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run through sudo")
    installed = subprocess.check_output(["systemctl", "show", "tm-camera-monitor.service", "-p", "WorkingDirectory", "--value"], text=True).strip()
    if not installed or Path(installed).resolve() != ROOT:
        raise RuntimeError("This is not the installed monitor folder; no configuration changed")
    if show("LoadState") != "loaded" or not RUNNER.is_file():
        raise RuntimeError("The expected separate-user Chromium kiosk is not installed")
    environment = dict(item.split("=", 1) for item in shlex.split(show("Environment")) if "=" in item)
    parts = urlsplit(environment.get("KIOSK_URL", "http://127.0.0.1:8080"))
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or parts.port != 8080:
        raise RuntimeError("This observer is scoped to the local dashboard on port 8080")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "preview_recovery"]
    query.append(("preview_recovery", "native1"))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))
    files = [DROPIN, WRAPPER, *(EXTENSION / name for name in ("manifest.json", "observer.js"))]
    if any(path.is_symlink() for path in files) or EXTENSION.is_symlink():
        raise RuntimeError("Refusing to overwrite a configuration symlink")
    previous = {path: path.read_bytes() if path.exists() else None for path in files}
    backup = BACKUP_ROOT / f"native-preview-{time.time_ns()}"
    backup.mkdir(parents=True)
    for path, data in previous.items():
        if data is not None:
            (backup / path.name).write_bytes(data)
    (backup / "previous.json").write_text(json.dumps({str(path): data is not None for path, data in previous.items()}))
    try:
        EXTENSION.mkdir(parents=True, exist_ok=True)
        for name in ("manifest.json", "observer.js"):
            shutil.copyfile(ROOT / "native-player-observer" / name, EXTENSION / name)
            (EXTENSION / name).chmod(0o644)
        EXTENSION.chmod(0o755)
        shutil.copyfile(ROOT / "scripts/native-kiosk.py", WRAPPER)
        WRAPPER.chmod(0o644)
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        DROPIN.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + str(WRAPPER)
                          + '\nEnvironment=' + json.dumps("KIOSK_URL=" + url.replace("%", "%%")) + '\n')
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", "tm-camera-kiosk.service"], check=True)
        invocation = show("InvocationID")
        if not re.fullmatch(r"[0-9a-f]{32}", invocation):
            raise RuntimeError("Could not identify this kiosk start for verification")
        print("Browser restarted. Checking for observer heartbeat and actual decoded frames (up to 40 seconds)...", flush=True)
        for attempt in range(40):
            result = subprocess.run(["journalctl", "-u", "tm-camera-kiosk.service", f"_SYSTEMD_INVOCATION_ID={invocation}", "--no-pager", "-o", "cat"], capture_output=True, text=True, check=True)
            if "TM_NATIVE_OBSERVER_READY" in result.stdout and "TM_NATIVE_VIDEO_PROGRESS" in result.stdout:
                subprocess.run(["systemctl", "is-active", "--quiet", "tm-camera-kiosk.service"], check=True)
                print("Verified on this browser: original MediaMTX player, frame observer, and decoded video progress.\nRecording server and MediaMTX were not restarted. Backup: " + str(backup))
                return
            time.sleep(1)
        raise RuntimeError("Could not verify observer and decoded frames; restoring previous kiosk configuration")
    except Exception:
        for path, data in previous.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "restart", "tm-camera-kiosk.service"], check=False)
        print("Previous kiosk configuration restored. The webpage retains the original MediaMTX player.", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Native player update stopped: {exc}", file=sys.stderr)
        sys.exit(1)
