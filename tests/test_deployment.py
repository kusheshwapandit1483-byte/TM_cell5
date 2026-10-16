import configparser
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("deploy")
kiosk = load("kiosk")
preview_update = load("update-preview")


class DeploymentTests(unittest.TestCase):
    def test_preview_refresh_keeps_playback_query_and_is_idempotent(self):
        url = preview_update.refreshed_url("http://127.0.0.1:8080/?live=webrtc&label=a%20b")
        self.assertIn("live=webrtc", url)
        self.assertIn("preview_recovery=1", url)
        self.assertEqual(preview_update.refreshed_url(url), url)

    def test_existing_mediamtx_service_is_reused(self):
        with patch.object(deploy.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            files, service = deploy.mediamtx_boot_plan("rtsp://127.0.0.1:8554/pramacam", "aegixcore")
        self.assertEqual(files, {})
        self.assertEqual(service, "tm-mediamtx.service")

    def test_remote_camera_does_not_create_a_local_relay_service(self):
        with patch.object(deploy.subprocess, "run") as run:
            self.assertEqual(deploy.mediamtx_boot_plan("rtsp://192.168.10.20/live", "aegixcore"), ({}, None))
            run.assert_not_called()

    def test_unidentified_local_relay_blocks_setup_before_configuration(self):
        with patch.object(deploy.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
             patch.object(Path, "glob", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "boot configuration could not be identified"):
                deploy.mediamtx_boot_plan("rtsp://127.0.0.1:8554/pramacam", "aegixcore")

    def test_health_requires_recording_policy_and_fresh_numeric_occupancy(self):
        status = {"mode": "occupied", "storageLimitGB": 35}
        mqtt = {"connected": True, "ageSeconds": 1, "data": {"occupancy": 0}}
        self.assertTrue(deploy.healthy(status, mqtt))
        for data in ({}, {"occupancy": True}, {"occupancy": "0"}, {"occupancy": -1}):
            self.assertFalse(deploy.healthy(status, dict(mqtt, data=data)))
        self.assertFalse(deploy.healthy(status, dict(mqtt, ageSeconds=9)))
        self.assertFalse(deploy.healthy(status, dict(mqtt, connected=False)))
        self.assertFalse(deploy.healthy(dict(status, mode="always"), mqtt))

    def test_lightdm_preserves_unrelated_settings_and_updates_specific_seat(self):
        old = "[LightDM]\nminimum-vt=7\n[Seat:*]\ngreeter-session=pi-greeter\nautologin-user=admin\n[Seat:seat0]\nautologin-user=old\n"
        output = deploy.lightdm_config(old, "operator", "rpd-labwc")
        config = configparser.ConfigParser()
        config.read_string(output)
        self.assertEqual(config["LightDM"]["minimum-vt"], "7")
        self.assertEqual(config["Seat:*"]["greeter-session"], "pi-greeter")
        for seat in ("Seat:*", "Seat:seat0"):
            self.assertEqual(config[seat]["autologin-user"], "operator")
            self.assertEqual(config[seat]["user-session"], "rpd-labwc")
        self.assertEqual(output, deploy.lightdm_config(output, "operator", "rpd-labwc"))

    def test_autostart_migrates_only_our_launcher_and_is_idempotent(self):
        old = "#!/bin/sh\nwallpaper-tool &\n@env KIOSK_URL=http://localhost /usr/local/bin/tm-camera-kiosk-start\npanel-tool &\n"
        output = deploy.autostart_text(old)
        self.assertIn("wallpaper-tool &", output)
        self.assertIn("panel-tool &", output)
        self.assertNotIn("tm-camera-kiosk-start", output)
        self.assertEqual(output.count("/usr/local/bin/tm-camera-kiosk-session"), 1)
        self.assertEqual(output, deploy.autostart_text(output))

    def test_chromium_uses_session_platform_and_keeps_arguments_separate(self):
        url = "http://localhost:8080/?test=a&b=c"
        for platform in ("x11", "wayland"):
            args = kiosk.chromium_command("chromium", url, "/home/operator/profile", {"XDG_SESSION_TYPE": platform})
            self.assertIn(f"--ozone-platform={platform}", args)
            self.assertEqual(args[2], url)

    def test_wayland_uses_existing_owned_socket(self):
        info = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFSOCK)
        with patch.object(Path, "stat", return_value=info), patch.object(Path, "resolve", lambda p: p):
            env = kiosk.valid_session({"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-1"}, uid=1000)
            self.assertEqual(env["XDG_SESSION_TYPE"], "wayland")
            self.assertNotIn("DISPLAY", env)

    def test_wrong_user_runtime_and_missing_display_are_rejected(self):
        info = SimpleNamespace(st_uid=2000, st_mode=stat.S_IFSOCK)
        with patch.object(Path, "stat", return_value=info):
            self.assertIsNone(kiosk.valid_session({"WAYLAND_DISPLAY": "wayland-0"}, uid=1000))
        info.st_uid = 1000
        with patch.object(Path, "stat", return_value=info):
            self.assertIsNone(kiosk.valid_session({}, uid=1000))

    def test_wayland_socket_outside_runtime_is_rejected(self):
        info = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFSOCK)
        with patch.object(Path, "stat", return_value=info), patch.object(Path, "resolve", lambda p: p):
            self.assertIsNone(kiosk.valid_session({"XDG_RUNTIME_DIR": "/run/user/1000",
                                                  "WAYLAND_DISPLAY": "/run/user/2000/wayland-0"}, uid=1000))

    def test_x11_rejects_unreadable_authority(self):
        info = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFSOCK)
        with patch.object(Path, "stat", return_value=info), patch.object(kiosk.os, "access", return_value=False):
            self.assertIsNone(kiosk.valid_session({"DISPLAY": ":1", "XAUTHORITY": "/private/auth"}, uid=1000))

    def test_x11_uses_actual_display_number(self):
        info = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFSOCK)
        with patch.object(Path, "stat", return_value=info):
            env = kiosk.valid_session({"DISPLAY": ":1"}, uid=1000)
            self.assertEqual(env["DISPLAY"], ":1")
            self.assertEqual(env["XDG_SESSION_TYPE"], "x11")

    def test_snapshot_restores_existing_and_removes_new_files(self):
        with tempfile.TemporaryDirectory(prefix="tm-deploy-test-", dir=Path(__file__).parent) as directory:
            root = Path(directory)
            original = root / "original.conf"
            created = root / "created.conf"
            original.write_text("previous settings")
            backups = root / "backups"
            with patch.object(deploy, "BACKUPS", backups), patch.object(deploy, "command"), \
                 patch.object(deploy.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                 patch.object(deploy.subprocess, "check_output", return_value="graphical.target\n"), \
                 patch.object(deploy.os, "chown", create=True):
                backup = deploy.snapshot([original, created])
                original.write_text("updated settings")
                created.write_text("new settings")
                deploy.restore(backup)
                self.assertEqual(original.read_text(), "previous settings")
                self.assertFalse(created.exists())
                self.assertEqual(json.loads((backup / "system-state.json").read_text())["target"], "graphical.target")

    def test_new_kiosk_config_directories_receive_kiosk_ownership(self):
        with tempfile.TemporaryDirectory(prefix="tm-deploy-test-", dir=Path(__file__).parent) as directory:
            path = Path(directory) / ".config/labwc/autostart"
            with patch.object(deploy.os, "chown", create=True) as chown:
                deploy.write(path, "session capture", owner=SimpleNamespace(pw_uid=1000, pw_gid=1000))
                owned = [call.args[0] for call in chown.call_args_list]
                self.assertIn(path.parent, owned)
                self.assertIn(path.parent.parent, owned)
                self.assertEqual(path.read_text(), "session capture")


if __name__ == "__main__":
    unittest.main()
