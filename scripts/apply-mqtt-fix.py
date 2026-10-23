#!/usr/bin/env python3
"""Apply the MQTT correction without replacing kiosk configuration or recordings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


def show(unit, prop):
    return subprocess.check_output(["systemctl", "show", unit, "-p", prop, "--value"], text=True).strip()


def wake_display():
    user = show("tm-camera-kiosk.service", "User")
    if not user or not Path("/usr/local/lib/tm-camera/kiosk.py").is_file():
        print("Display check skipped: expected kiosk session launcher not found")
        return
    # Run in the actual kiosk account, importing its existing session discovery.
    code = '''import importlib.util,os,shutil,subprocess
spec=importlib.util.spec_from_file_location("kiosk","/usr/local/lib/tm-camera/kiosk.py")
kiosk=importlib.util.module_from_spec(spec);spec.loader.exec_module(kiosk)
session=kiosk.discover_session()
if not session: raise SystemExit("No usable kiosk graphical session was found")
env=dict(os.environ);env.update(session)
print("Kiosk session:",session.get("XDG_SESSION_TYPE"),flush=True)
if session.get("XDG_SESSION_TYPE")=="wayland":
 tool=shutil.which("wlopm")
 if not tool: raise SystemExit("wlopm not installed; no display settings changed")
 print("Display outputs before wake:",flush=True);subprocess.run([tool],env=env,timeout=5)
 result=subprocess.run([tool,"--on","*"],env=env,timeout=5)
 print("Display wake return code:",result.returncode,flush=True)
 print("Display outputs after wake:",flush=True);subprocess.run([tool],env=env,timeout=5)
else:
 print("X11 session; no Wayland power command applied",flush=True)
'''
    print("Checking/waking the display as kiosk user " + user + ". This does not change desktop startup settings.", flush=True)
    result = subprocess.run(["runuser", "-u", user, "--", "/usr/bin/python3", "-c", code], timeout=25)
    if result.returncode:
        print("Display check could not complete; send this output. MQTT update remains installed.")


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run through sudo")
    installed = show("tm-camera-monitor.service", "WorkingDirectory")
    if not installed or Path(installed).resolve() != ROOT:
        raise RuntimeError(f"The active monitor uses {installed}; run the update in that folder")
    subprocess.run(["systemctl", "restart", "tm-camera-monitor.service"], check=True)
    try:
        wake_display()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Display check failed: {exc}; continuing MQTT verification", flush=True)
    print("Monitor restarted with the MQTT keepalive correction. Observing fresh status for 70 continuous seconds...", flush=True)
    opener = build_opener(ProxyHandler({}))
    stable_since = None
    deadline = time.monotonic() + 100
    last_print = 0
    last_reason = "waiting for server"
    while time.monotonic() < deadline:
        try:
            with opener.open("http://127.0.0.1:8080/api/esp32/status", timeout=2) as response:
                status = json.load(response)
            age = status.get("ageSeconds")
            fresh = status.get("connected") is True and isinstance(age, (int, float)) and 0 <= age <= 5
            now = time.monotonic()
            stable_since = (now if stable_since is None else stable_since) if fresh else None
            elapsed = now - stable_since if stable_since is not None else 0
            last_reason = f"connected={status.get('connected')} age={age} error={status.get('error')}"
            if now - last_print >= 10:
                print(f"MQTT: {last_reason}; continuous fresh status={int(elapsed)}s", flush=True)
                last_print = now
            if elapsed >= 70:
                with opener.open("http://127.0.0.1:8080/api/recording/status", timeout=2) as response:
                    recording = json.load(response)
                if recording.get("mode") != "occupied" or recording.get("storageLimitGB") != 35:
                    raise RuntimeError("Recording policy verification failed")
                print("Verified: 70 seconds of fresh MQTT status, occupancy recording, 35 GB limit.\nRecordings directory retained: " + recording["recordingsDir"], flush=True)
                break
        except (OSError, ValueError) as exc:
            stable_since = None
            last_reason = str(exc)
        time.sleep(1)
    else:
        raise RuntimeError("MQTT did not remain fresh: " + last_reason + ". The correction is installed; ESP32/network/broker still needs investigation")



if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Update/check stopped: {exc}", file=sys.stderr)
        sys.exit(1)
