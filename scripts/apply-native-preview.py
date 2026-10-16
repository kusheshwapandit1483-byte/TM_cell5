#!/usr/bin/env python3
"""Install frame observation around the working local iframe, restarting only the browser."""
import json
import os
import secrets
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DROPIN = Path("/etc/systemd/system/tm-camera-kiosk.service.d/96-native-player.conf")
EXTENSION = Path("/usr/local/lib/tm-camera/native-player-observer")
WRAPPER = Path("/usr/local/lib/tm-camera/native-kiosk.py")
RUNNER = Path("/usr/local/lib/tm-camera/kiosk.py")
BACKUP_ROOT = Path("/var/backups/tm-camera")


class Verification:
    """Temporary loopback-only receiver, independent of Chromium's logging settings."""
    STAGES = {"dashboard-ready", "observer-ready", "observer-frames", "parent-ready", "parent-frames", "parent-source-rejected"}

    def __init__(self):
        self.nonce = secrets.token_urlsafe(24)
        self.stages = set()
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                prefix = f"/verify/{receiver.nonce}/"
                stage = self.path[len(prefix):] if self.path.startswith(prefix) else ""
                if stage not in receiver.STAGES:
                    self.send_error(404)
                    return
                with receiver.lock:
                    receiver.stages.add(stage)
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def parameters(self):
        return [("tm_verify_port", str(self.server.server_port)), ("tm_verify_nonce", self.nonce)]

    def snapshot(self):
        with self.lock:
            return sorted(self.stages)

    def verified(self):
        return {"dashboard-ready", "parent-ready", "parent-frames"}.issubset(self.snapshot())


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
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in ("preview_recovery", "tm_verify_port", "tm_verify_nonce")]
    query.append(("preview_recovery", "native2"))
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
        def configure(target):
            DROPIN.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + str(WRAPPER)
                              + '\nEnvironment=' + json.dumps("KIOSK_URL=" + target.replace("%", "%%")) + '\n')
            subprocess.run(["systemctl", "daemon-reload"], check=True)

        with Verification() as verification:
            checking_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                                       urlencode(query + verification.parameters), parts.fragment))
            configure(checking_url)
            subprocess.run(["systemctl", "restart", "tm-camera-kiosk.service"], check=True)
            print("Browser restarted. Directly checking observer and decoded frames (up to 40 seconds)...", flush=True)
            last = None
            for attempt in range(40):
                stages = verification.snapshot()
                if stages != last:
                    print("Verification: " + (", ".join(stages) or "waiting for dashboard"), flush=True)
                    last = stages
                if verification.verified():
                    subprocess.run(["systemctl", "is-active", "--quiet", "tm-camera-kiosk.service"], check=True)
                    configure(url)  # Future browser starts do not retain the temporary receiver URL.
                    print("Verified on this browser: original MediaMTX player, frame observer, and decoded video progress.\nRecording server and MediaMTX were not restarted. Backup: " + str(backup))
                    return
                time.sleep(1)
            stages = verification.snapshot()
            (backup / "verification.json").write_text(json.dumps({"received": stages}) + "\n")
            raise RuntimeError("Verification failed; received: " + (", ".join(stages) or "no dashboard reports")
                               + ". Diagnostics: " + str(backup / "verification.json"))
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
