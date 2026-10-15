#!/usr/bin/env python3
"""Session-aware Chromium runner. Called as the separate kiosk user."""
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
from urllib.request import urlopen

SESSION_KEYS = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "XDG_SESSION_TYPE", "XDG_SESSION_ID", "DBUS_SESSION_BUS_ADDRESS")


def valid_session(env, uid=None):
    """Do not guess DISPLAY=:0 or connect to another user's Wayland socket."""
    uid = os.getuid() if uid is None else uid
    runtime = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
    try:
        if runtime.stat().st_uid != uid:
            return None
        wayland = env.get("WAYLAND_DISPLAY")
        if wayland:
            endpoint = Path(wayland) if wayland.startswith("/") else runtime / wayland
            if endpoint.parent.resolve() != runtime.resolve():
                return None
            info = endpoint.stat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == uid:
                return dict(env, XDG_RUNTIME_DIR=str(runtime), XDG_SESSION_TYPE="wayland")
        display = env.get("DISPLAY", "")
        if display.startswith(":"):
            number = display[1:].split(".")[0]
            endpoint = Path("/tmp/.X11-unix") / f"X{number}"
            if number.isdigit() and stat.S_ISSOCK(endpoint.stat().st_mode):
                authority = env.get("XAUTHORITY")
                if authority and not os.access(authority, os.R_OK):
                    return None
                return dict(env, XDG_RUNTIME_DIR=str(runtime), XDG_SESSION_TYPE="x11")
    except (OSError, ValueError):
        return None
    return None


def session_file():
    return Path(f"/run/user/{os.getuid()}/tm-camera-session.json")


def capture_session():
    env = {key: os.environ[key] for key in SESSION_KEYS if key in os.environ}
    if not valid_session(env):
        raise RuntimeError("No usable graphical session; run capture from desktop autostart")
    destination = session_file()
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(env), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(destination)


def discover_session():
    # Desktop autostart publishes the actual environment. Process discovery also
    # supports reinstalling on a kiosk session which is already logged in.
    candidates = []
    try:
        path = session_file()
        if path.stat().st_uid == os.getuid() and not path.is_symlink():
            saved = json.loads(path.read_text(encoding="utf-8"))
            candidates.append({k: v for k, v in saved.items()
                               if k in SESSION_KEYS and isinstance(v, str)})
    except (OSError, ValueError, AttributeError):
        pass
    candidates.append({key: os.environ[key] for key in SESSION_KEYS if key in os.environ})
    for candidate in candidates:
        if session := valid_session(candidate):
            return session
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid != os.getuid():
                continue
            values = {}
            for field in (process / "environ").read_bytes().split(b"\0"):
                key, separator, value = field.partition(b"=")
                name = key.decode(errors="replace")
                if separator and name in SESSION_KEYS:
                    values[name] = value.decode(errors="replace")
            if session := valid_session(values):
                return session
        except (OSError, ValueError):
            continue
    return None


def chromium_command(binary, url, profile, session):
    return [binary, "--kiosk", url, f"--user-data-dir={profile}",
            f"--ozone-platform={session['XDG_SESSION_TYPE']}",
            "--password-store=basic", "--no-first-run", "--no-default-browser-check",
            "--noerrdialogs", "--disable-session-crashed-bubble",
            "--autoplay-policy=no-user-gesture-required"]


def run():
    import fcntl  # Linux-only; imported here so policy tests can run on Windows.
    stopping = False
    child = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    binary = shutil.which("chromium") or shutil.which("chromium-browser")
    if not binary:
        raise RuntimeError("Chromium is not installed; rerun setup")
    url = os.environ.get("KIOSK_URL", "http://127.0.0.1:8080")
    profile = Path.home() / ".config/tm-camera-chromium"
    profile.mkdir(parents=True, exist_ok=True)
    print("Waiting for kiosk user's graphical session and dashboard", flush=True)
    lock = None
    while not stopping:
        session = discover_session()
        if not session and lock is not None:
            lock.close()
            lock = None
        if session:
            try:
                with urlopen(url, timeout=3) as response:
                    ready = response.status == 200
            except (OSError, ValueError):
                ready = False
            if ready:
                if lock is None:
                    lock = (Path(session["XDG_RUNTIME_DIR"]) / "tm-camera-kiosk.lock").open("a")
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        print("Another kiosk launcher already owns this session", flush=True)
                        return
                env = dict(os.environ)
                for key in SESSION_KEYS:
                    env.pop(key, None)
                env.update(session)
                if session["XDG_SESSION_TYPE"] == "x11" and shutil.which("xset"):
                    for args in (("s", "off"), ("s", "noblank"), ("-dpms",)):
                        subprocess.run(["xset", *args], env=env, check=False,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"Starting Chromium in {session['XDG_SESSION_TYPE']} session", flush=True)
                child = subprocess.Popen(chromium_command(binary, url, profile, session), env=env)
                while child.poll() is None and not stopping:
                    time.sleep(1)
                if stopping:
                    try:
                        child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                child = None
        if not stopping:
            time.sleep(2)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "capture":
            capture_session()
        else:
            run()
    except Exception as exc:
        print(f"Kiosk startup failed: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
