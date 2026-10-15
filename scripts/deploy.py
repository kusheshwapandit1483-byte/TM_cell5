#!/usr/bin/env python3
"""Install and restore the separate-user kiosk without assuming its display number."""
import argparse
import configparser
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.parse import urlparse

STATE = Path("/etc/tm-camera/deployment.json")
BACKUPS = Path("/var/backups/tm-camera")
ROOT = Path(__file__).resolve().parents[1]
MARKER = "# TM CAMERA SESSION CAPTURE"


def command(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def lightdm_config(text, user, session):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read_string(text)
    sections = ["Seat:*"] + (["Seat:seat0"] if config.has_section("Seat:seat0") else [])
    for section in sections:
        if not config.has_section(section):
            config.add_section(section)
        for key, value in {"autologin-user": user, "autologin-user-timeout": "0",
                           "autologin-guest": "false", "user-session": session,
                           "autologin-session": session}.items():
            config.set(section, key, value)
    output = io.StringIO()
    config.write(output, space_around_delimiters=False)
    return output.getvalue()


def autostart_text(text, prefix=""):
    # Remove only this project's old launcher/capture lines; retain other apps.
    lines = [line for line in text.splitlines()
             if "tm-camera-kiosk-start" not in line
             and "tm-camera-kiosk-session" not in line and line != MARKER]
    return "\n".join(lines).rstrip() + f"\n{MARKER}\n{prefix}/usr/local/bin/tm-camera-kiosk-session\n"


def select_session(requested=""):
    for name in ([requested] if requested else ["rpd-labwc", "labwc", "LXDE-pi", "LXDE", "xfce"]):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RuntimeError("Invalid SESSION_NAME")
        if any((Path(folder) / f"{name}.desktop").is_file()
               for folder in ("/usr/share/wayland-sessions", "/usr/share/xsessions")):
            return name
    raise RuntimeError("No supported installed desktop session found. Use Raspberry Pi OS Desktop; "
                       "or set SESSION_NAME to an installed session. Nothing has been reconfigured.")


def snapshot(paths, extra_services=()):
    backup = BACKUPS / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    manifest = []
    for index, path in enumerate(paths):
        path = Path(path)
        if path.is_symlink():
            raise RuntimeError(f"Refusing to replace symlink: {path}")
        item = {"path": str(path), "exists": path.exists()}
        if path.exists():
            info = path.stat()
            if not path.is_file():
                raise RuntimeError(f"Expected configuration file: {path}")
            item.update(mode=info.st_mode & 0o777, uid=info.st_uid, gid=info.st_gid, copy=str(index))
            shutil.copy2(path, backup / str(index))
        manifest.append(item)
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    services = {}
    for name in ("tm-camera-monitor.service", "tm-camera-kiosk.service", "lightdm.service", *extra_services):
        services[name] = {
            "enabled": subprocess.run(["systemctl", "is-enabled", "--quiet", name]).returncode == 0,
            "active": subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0,
        }
    target = subprocess.check_output(["systemctl", "get-default"], text=True).strip()
    (backup / "system-state.json").write_text(json.dumps({"services": services, "target": target}), encoding="utf-8")
    return backup


def restore(backup):
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError("Backup must be directly inside /var/backups/tm-camera")
    saved = json.loads((backup / "system-state.json").read_text(encoding="utf-8"))
    for service in saved["services"]:
        if service == "lightdm.service":
            continue
        subprocess.run(["systemctl", "stop", service], check=False)
        subprocess.run(["systemctl", "disable", service], check=False)
    for item in json.loads((backup / "manifest.json").read_text(encoding="utf-8")):
        path = Path(item["path"])
        if path.is_symlink():
            raise RuntimeError(f"Refusing to restore over symlink: {path}")
        if item["exists"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / item["copy"], path)
            os.chmod(path, item["mode"])
            os.chown(path, item["uid"], item["gid"])
        elif path.exists():
            path.unlink()
    command("systemctl", "daemon-reload")
    for service, previous in saved["services"].items():
        subprocess.run(["systemctl", "enable" if previous["enabled"] else "disable", service], check=False)
        if previous["active"] and service != "lightdm.service":
            command("systemctl", "start", service)
    command("systemctl", "set-default", saved["target"])
    print("Configuration restored. Reboot to apply the restored login configuration.")


def write(path, text, mode=0o644, owner=None):
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if owner:
        for directory in missing:
            os.chown(directory, owner.pw_uid, owner.pw_gid)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    if owner:
        os.chown(path, owner.pw_uid, owner.pw_gid)


def service_environment():
    result = subprocess.run(["systemctl", "show", "tm-camera-monitor.service", "-p", "Environment", "--value"],
                            capture_output=True, text=True, check=False)
    values = dict(value.split("=", 1) for value in shlex.split(result.stdout) if "=" in value)
    # A running process includes effective EnvironmentFile overrides too.
    pid = subprocess.run(["systemctl", "show", "tm-camera-monitor.service", "-p", "MainPID", "--value"],
                         capture_output=True, text=True, check=False).stdout.strip()
    if pid.isdigit() and int(pid) > 0:
        try:
            for field in (Path("/proc") / pid / "environ").read_bytes().split(b"\0"):
                key, separator, value = field.partition(b"=")
                name = key.decode(errors="replace")
                if separator and name in ("RECORDING_SOURCE", "RECORDINGS_DIR", "ESP32_MQTT_HOST", "ESP32_MQTT_PORT"):
                    values[name] = value.decode(errors="replace")
        except OSError:
            pass
    return values


def check_connections(source, host, port):
    # Do not print camera URLs, which may contain credentials.
    try:
        result = command("ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                "-i", source, "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                "-of", "csv=p=0", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=12)
        if not result.stdout.strip():
            raise RuntimeError("RTSP source has no video stream")
    except subprocess.SubprocessError:
        raise RuntimeError("MediaMTX RTSP video preflight failed. Start the existing relay/camera before setup.") from None
    try:
        with socket.create_connection((host, int(port)), timeout=5):
            pass
    except OSError:
        raise RuntimeError("ESP32 MQTT preflight failed. Check the controller connection before setup.") from None


def mediamtx_boot_plan(source, app_user):
    """Reuse a system service, or register the running local relay for next boot."""
    parsed = urlparse(source)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return {}, None
    for name in ("tm-mediamtx.service", "mediamtx.service"):
        if subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0:
            return {}, name
    for process in Path("/proc").glob("[0-9]*"):
        try:
            binary = (process / "exe").resolve(strict=True)
            if binary.name != "mediamtx":
                continue
            cgroup = (process / "cgroup").read_text()
            managed = re.search(r"/system\.slice/([A-Za-z0-9_.@-]+\.service)(?:/|$)", cgroup, re.MULTILINE)
            if managed:
                return {}, managed.group(1)
            args = (process / "cmdline").read_bytes().decode().strip("\0").split("\0")
            cwd = (process / "cwd").resolve(strict=True)
            config = Path(args[1]) if len(args) == 2 and not args[1].startswith("-") else cwd / "mediamtx.yml"
            if not config.is_absolute():
                config = cwd / config
            config = config.resolve(strict=True)
            if not config.is_file() or any(not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(p)) for p in (binary, config)):
                raise RuntimeError("Cannot safely register the existing MediaMTX paths")
            command("runuser", "-u", app_user, "--", "test", "-r", str(config))
            command("runuser", "-u", app_user, "--", "test", "-x", str(binary))
            environment = []
            for field in (process / "environ").read_bytes().split(b"\0"):
                key, separator, value = field.partition(b"=")
                if separator and re.fullmatch(rb"MTX_[A-Za-z0-9_]+", key):
                    environment.append(key.decode() + "=" + json.dumps(value.decode(), ensure_ascii=False))
            return {
                "/etc/tm-camera/mediamtx.env": "\n".join(environment) + "\n",
                "/etc/systemd/system/tm-mediamtx.service": f"""[Unit]
Description=Existing MediaMTX camera relay
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User={app_user}
WorkingDirectory={cwd}
EnvironmentFile=/etc/tm-camera/mediamtx.env
ExecStart={binary} {config}
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
""",
            }, "tm-mediamtx.service"
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    raise RuntimeError("Local stream is running but its MediaMTX boot configuration could not be identified. "
                       "No kiosk configuration has been changed. Start the existing MediaMTX normally and retry.")


def healthy(status, mqtt):
    occupancy = mqtt.get("data", {}).get("occupancy") if isinstance(mqtt.get("data"), dict) else None
    age = mqtt.get("ageSeconds")
    return (status.get("mode") == "occupied" and status.get("storageLimitGB") == 35
            and mqtt.get("connected") is True and isinstance(age, (int, float)) and 0 <= age < 5
            and type(occupancy) is int and occupancy >= 0)


def setup(reboot=False):
    import pwd
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("This installer requires a running systemd-based Raspberry Pi OS desktop")
    app_user = os.environ.get("APP_RUN_USER") or os.environ.get("SUDO_USER", "")
    kiosk_user = os.environ.get("KIOSK_USER", "operator")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", kiosk_user):
        raise RuntimeError("Invalid kiosk username")
    app_account = pwd.getpwnam(app_user)
    if app_account.pw_uid == 0 or kiosk_user == app_user or kiosk_user == "root":
        raise RuntimeError("Run with sudo from your admin account; KIOSK_USER must be a separate account")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(ROOT)):
        raise RuntimeError("Project path must not contain spaces or systemd escape characters")
    session = select_session(os.environ.get("SESSION_NAME", ""))
    display_manager = Path("/etc/X11/default-display-manager")
    if display_manager.exists() and "lightdm" not in display_manager.read_text():
        raise RuntimeError("Existing display manager is not LightDM; refusing to replace it automatically")
    print(f"Installing for app user {app_user}, kiosk user {kiosk_user}, desktop {session}", flush=True)
    command("apt-get", "update")
    packages = ["python3", "ffmpeg", "curl", "lightdm", "x11-xserver-utils"]
    if not (shutil.which("chromium") or shutil.which("chromium-browser")):
        for package in ("chromium", "chromium-browser"):
            candidate = subprocess.run(["apt-cache", "policy", package], capture_output=True,
                                       text=True, check=True).stdout
            if re.search(r"Candidate:\s+(?!\(none\))\S+", candidate):
                packages.append(package)
                break
        else:
            raise RuntimeError("No installable Chromium package found")
    command("apt-get", "install", "-y", *packages, env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
    command("runuser", "-u", app_user, "--", "python3", "-m", "unittest", "discover", "-s", "tests", "-v", cwd=ROOT)
    existing = service_environment()
    source = os.environ.get("RECORDING_SOURCE") or existing.get("RECORDING_SOURCE", "rtsp://127.0.0.1:8554/pramacam")
    mqtt_host = os.environ.get("ESP32_MQTT_HOST") or existing.get("ESP32_MQTT_HOST", "192.168.10.1")
    mqtt_port = os.environ.get("ESP32_MQTT_PORT") or existing.get("ESP32_MQTT_PORT", "1883")
    print("Checking existing MediaMTX camera stream and ESP32 MQTT connection", flush=True)
    check_connections(source, mqtt_host, mqtt_port)
    media_files, media_service = mediamtx_boot_plan(source, app_user)
    try:
        kiosk_account = pwd.getpwnam(kiosk_user)
        if kiosk_account.pw_uid == 0:
            raise RuntimeError("Kiosk account must not be root")
    except KeyError:
        command("adduser", "--disabled-password", "--gecos", "Camera Kiosk", kiosk_user)
        kiosk_account = pwd.getpwnam(kiosk_user)
    kiosk_home = Path(kiosk_account.pw_dir)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(kiosk_home)):
        raise RuntimeError("Unsupported kiosk home path")
    # Preserve the installed archive when moving code into a new checkout.
    old_workdir = subprocess.run(["systemctl", "show", "tm-camera-monitor.service", "-p", "WorkingDirectory", "--value"],
                                 capture_output=True, text=True, check=False).stdout.strip()
    archive = os.environ.get("RECORDINGS_DIR") or existing.get("RECORDINGS_DIR")
    if not archive:
        archive = str(Path(old_workdir) / "recordings") if old_workdir and (Path(old_workdir) / "recordings").is_dir() else str(ROOT / "recordings")
    if not Path(archive).is_absolute() or any(c in archive for c in '\n\r"%\\'):
        raise RuntimeError("RECORDINGS_DIR must be an absolute path without systemd escape characters")
    command("runuser", "-u", app_user, "--", "python3", "-c",
            "from pathlib import Path; import sys,tempfile; p=Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); "
            "tempfile.TemporaryFile(dir=p).close()", archive)
    files = {
        "/etc/systemd/system/tm-camera-kiosk.service": f"""[Unit]
Description=TM separate-user kiosk (X11 or Wayland)
After=systemd-user-sessions.service lightdm.service tm-camera-monitor.service
Wants=lightdm.service tm-camera-monitor.service
StartLimitIntervalSec=0
[Service]
Type=simple
User={kiosk_user}
Environment=HOME={kiosk_home}
Environment=PYTHONUNBUFFERED=1
Environment=KIOSK_URL=http://127.0.0.1:8080
ExecStart=/usr/local/bin/tm-camera-kiosk-start
Restart=always
RestartSec=3
TimeoutStopSec=12
[Install]
WantedBy=graphical.target
""",
        "/etc/systemd/system/tm-camera-monitor.service.d/90-raspberrypibase.conf": f"""[Service]
User={app_user}
WorkingDirectory={ROOT}
ExecStart=
ExecStart=/usr/bin/python3 {ROOT}/server.py
Environment=PORT=8080
Environment="RECORDINGS_DIR={archive}"
""",
        "/usr/local/bin/tm-camera-kiosk-start": "#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/tm-camera/kiosk.py\n",
        "/usr/local/bin/tm-camera-kiosk-session": "#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/tm-camera/kiosk.py capture\n",
        "/usr/local/lib/tm-camera/kiosk.py": (ROOT / "scripts/kiosk.py").read_text(encoding="utf-8"),
        "/etc/lightdm/lightdm.conf.d/99-tm-camera-kiosk.conf": lightdm_config("", kiosk_user, session),
        "/etc/lightdm/lightdm.conf": lightdm_config(Path("/etc/lightdm/lightdm.conf").read_text()
                                                   if Path("/etc/lightdm/lightdm.conf").exists() else "", kiosk_user, session),
        str(kiosk_home / ".config/autostart/tm-camera-kiosk.desktop"): """[Desktop Entry]
Type=Application
Name=TM Camera Session
Exec=/usr/local/bin/tm-camera-kiosk-session
Terminal=false
X-GNOME-Autostart-enabled=true
""",
    }
    files.update(media_files)
    # Keep a pre-existing base server unit and its environment/drop-ins intact.
    base_unit = Path("/etc/systemd/system/tm-camera-monitor.service")
    if not base_unit.exists():
        files[str(base_unit)] = """[Unit]
Description=TM occupancy recording server
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
"""
    # Explicit deployment overrides are added without changing existing settings.
    for name in ("RECORDING_SOURCE", "ESP32_MQTT_HOST", "ESP32_MQTT_PORT", "ESP32_MQTT_TOPIC"):
        if name in os.environ:
            value = os.environ[name]
            if any(c in value for c in '\n\r"%\\'):
                raise RuntimeError(f"Unsupported escape characters in {name}; use existing service environment")
            files["/etc/systemd/system/tm-camera-monitor.service.d/90-raspberrypibase.conf"] += f'Environment="{name}={value}"\n'
    for relative, prefix in ((".config/labwc/autostart", ""),
                             (".config/lxsession/LXDE-pi/autostart", ""),
                             (".config/lxsession/LXDE/autostart", "")):
        path = kiosk_home / relative
        files[str(path)] = autostart_text(path.read_text() if path.exists() else "", prefix)
    backup = snapshot([*files, str(STATE)], [media_service] if media_service else [])
    print(f"Configuration backup: {backup}", flush=True)
    try:
        for filename, text in files.items():
            mode = 0o600 if filename.endswith("/mediamtx.env") else 0o755 if filename.startswith("/usr/local/bin/") or filename.endswith("/labwc/autostart") else 0o644
            write(filename, text, mode, kiosk_account if filename.startswith(str(kiosk_home) + "/") else None)
        write(STATE, json.dumps({"backup": str(backup), "appDir": str(ROOT), "appUser": app_user,
                                "kioskUser": kiosk_user, "kioskHome": str(kiosk_home), "session": session}, indent=2))
        command("passwd", "-l", kiosk_user, stdout=subprocess.DEVNULL)
        groups = subprocess.check_output(["id", "-nG", kiosk_user], text=True).split()
        if "sudo" in groups:
            command("gpasswd", "-d", kiosk_user, "sudo")
        for group in ("video", "audio", "render", "input", "autologin"):
            if subprocess.run(["getent", "group", group], stdout=subprocess.DEVNULL).returncode == 0:
                command("usermod", "-aG", group, kiosk_user)
        command("systemctl", "daemon-reload")
        effective_dir = subprocess.check_output(["systemctl", "show", "tm-camera-monitor.service", "-p", "WorkingDirectory", "--value"], text=True).strip()
        effective_user = subprocess.check_output(["systemctl", "show", "tm-camera-kiosk.service", "-p", "User", "--value"], text=True).strip()
        if effective_dir != str(ROOT) or effective_user != kiosk_user:
            raise RuntimeError("An existing later systemd drop-in overrides the installed app path or kiosk user")
        command("systemctl", "enable", "tm-camera-monitor.service", "tm-camera-kiosk.service")
        if media_service:
            command("systemctl", "enable", media_service)
        command("systemctl", "set-default", "graphical.target")
        command("systemctl", "restart", "tm-camera-monitor.service")
        verified = False
        for _ in range(30):
            try:
                with urlopen("http://127.0.0.1:8080/api/recording/status", timeout=2) as response:
                    status = json.load(response)
                with urlopen("http://127.0.0.1:8080/api/esp32/status", timeout=2) as response:
                    mqtt = json.load(response)
                if healthy(status, mqtt):
                    verified = True
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        if not verified:
            raise RuntimeError("Updated dashboard or fresh ESP32 MQTT status did not pass health checks")
        command("systemctl", "is-active", "--quiet", "tm-camera-monitor.service")
        command("systemctl", "enable", "lightdm.service")
        command("systemctl", "restart", "tm-camera-kiosk.service")
    except Exception:
        print("Setup failed; restoring the saved service and login configuration.", file=sys.stderr, flush=True)
        try:
            restore(backup)
        except Exception:
            print(f"Automatic restore could not complete. Run: sudo python3 {ROOT}/scripts/deploy.py --rollback {backup}", file=sys.stderr)
        raise
    print("SETUP VERIFIED: occupancy recorder, 35 GB archive, fresh ESP32 status.\n"
          "The operator desktop starts after reboot; browser visibility still needs a screen check.\n"
          "Stop/start browser: sudo systemctl stop/start tm-camera-kiosk.service\n"
          "Diagnostic report: sudo bash scripts/check-kiosk.sh", flush=True)
    if reboot:
        print("Rebooting into the separate kiosk user now.", flush=True)
        command("systemctl", "reboot")
    else:
        print("Finish with: sudo reboot", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reboot", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo from your administrator account")
    try:
        restore(args.rollback) if args.rollback else setup(args.reboot)
    except Exception as exc:
        # Avoid echoing subprocess arguments which could include camera credentials.
        print(f"Deployment stopped: {type(exc).__name__}. See the preceding diagnostic. "
              "No successful deployment is being reported.", file=sys.stderr)
        if not isinstance(exc, subprocess.SubprocessError):
            print(str(exc), file=sys.stderr)
        sys.exit(1)
