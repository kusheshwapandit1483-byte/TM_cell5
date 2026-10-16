#!/usr/bin/env python3
"""Apply this checkout's preview fix and refresh only the kiosk browser."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def refreshed_url(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "preview_recovery"]
    query.append(("preview_recovery", "1"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def show(unit, property_name):
    return subprocess.check_output(["systemctl", "show", unit, "-p", property_name, "--value"], text=True).strip()


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run: sudo python3 scripts/update-preview.py")
    installed = show("tm-camera-monitor.service", "WorkingDirectory")
    if not installed or Path(installed).resolve() != ROOT:
        raise RuntimeError("The running application points to a different folder. "
                           "Update that installed checkout; no service settings have been changed.")
    if show("tm-camera-kiosk.service", "LoadState") != "loaded":
        raise RuntimeError("The raspberrypibase kiosk service is missing. No alternate kiosk method has been installed.")
    for name in ("app.js", "index.html", "preview-recovery.js", "mediamtx-reader.js"):
        if not (ROOT / name).is_file():
            raise RuntimeError(f"Incomplete checkout: {name} is missing")
    environment = dict(item.split("=", 1) for item in shlex.split(show("tm-camera-kiosk.service", "Environment")) if "=" in item)
    url = refreshed_url(environment.get("KIOSK_URL", "http://127.0.0.1:8080"))
    if any(char in url for char in '\r\n'):
        raise RuntimeError("Invalid dashboard URL")
    path = Path("/etc/systemd/system/tm-camera-kiosk.service.d/95-preview-recovery.conf")
    if path.is_symlink():
        raise RuntimeError("Refusing to overwrite a configuration symlink")
    old = path.read_bytes() if path.exists() else None
    if old is not None:
        backup = Path("/var/backups/tm-camera") / f"preview-recovery-{time.time_ns()}.conf"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Service]\nEnvironment=" + json.dumps("KIOSK_URL=" + url.replace("%", "%%")) + "\n", encoding="utf-8")
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", "tm-camera-kiosk.service"], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", "tm-camera-kiosk.service"], check=True)
    except Exception:
        if old is None:
            path.unlink()
        else:
            path.write_bytes(old)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "restart", "tm-camera-kiosk.service"], check=False)
        raise
    print("Preview recovery applied. Browser restarted with a fresh dashboard URL.\n"
          "Recording server was not restarted. Check the screen once; no reboot is required.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Preview update stopped: {exc}", file=sys.stderr)
        sys.exit(1)
