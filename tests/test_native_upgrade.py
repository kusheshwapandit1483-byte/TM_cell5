import importlib.util
from pathlib import Path
import tempfile
import shutil
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_upgrade", ROOT / "scripts/upgrade-native-preview.py")
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
spec = importlib.util.spec_from_file_location("native_apply", ROOT / "scripts/apply-native-preview.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class NativeUpgradeTests(unittest.TestCase):
    def test_confirmed_rollback_is_preserved_but_other_changes_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as folder:
            root = Path(folder)
            (root / "app.js").write_text("original\n\n")
            with patch.object(upgrade.subprocess, "check_output", return_value="original\n\n"):
                self.assertTrue(upgrade.known_rollback(root, ["app.js"]))
                self.assertFalse(upgrade.known_rollback(root, ["server.py"]))
                (root / "app.js").write_text("original\n// custom code\n")
                self.assertFalse(upgrade.known_rollback(root, ["app.js"]))

    def install_fixture(self, ready):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT / "tests")))
        shutil.copytree(ROOT / "native-player-observer", root / "native-player-observer")
        (root / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/native-kiosk.py", root / "scripts/native-kiosk.py")
        library = root / "installed"
        library.mkdir()
        runner = library / "kiosk.py"
        runner.write_text("original launcher")
        dropin = root / "systemd" / "96-native-player.conf"
        dropin.parent.mkdir()
        dropin.write_text("previous configuration")
        for name, value in {"ROOT": root, "EXTENSION": library / "observer", "WRAPPER": library / "native-kiosk.py",
                            "RUNNER": runner, "DROPIN": dropin, "BACKUP_ROOT": root / "backups"}.items():
            stack.enter_context(patch.object(apply, name, value))
        stack.enter_context(patch.object(apply.os, "geteuid", return_value=0, create=True))
        stack.enter_context(patch.object(apply.subprocess, "check_output", return_value=str(root)))
        stack.enter_context(patch.object(apply, "show", side_effect=lambda p: {"LoadState": "loaded", "Environment": 'KIOSK_URL=http://127.0.0.1:8080', "InvocationID": "a" * 32}[p]))
        log = "TM_NATIVE_OBSERVER_READY\nTM_NATIVE_VIDEO_PROGRESS" if ready else "observer absent"
        run = stack.enter_context(patch.object(apply.subprocess, "run", return_value=SimpleNamespace(stdout=log, returncode=0)))
        stack.enter_context(patch.object(apply.time, "sleep"))
        return root, runner, dropin, run

    def test_install_requires_decoded_frames_and_only_restarts_kiosk(self):
        root, runner, dropin, run = self.install_fixture(True)
        apply.main()
        self.assertEqual(runner.read_text(), "original launcher")
        self.assertIn("native-kiosk.py", dropin.read_text())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([cmd for cmd in commands if "restart" in cmd], [["systemctl", "restart", "tm-camera-kiosk.service"]])
        self.assertTrue((root / "installed/observer/manifest.json").is_file())

    def test_unverified_observer_restores_previous_kiosk_files(self):
        root, runner, dropin, run = self.install_fixture(False)
        with self.assertRaisesRegex(RuntimeError, "Could not verify"):
            apply.main()
        self.assertEqual(dropin.read_text(), "previous configuration")
        self.assertEqual(runner.read_text(), "original launcher")
        self.assertFalse((root / "installed/native-kiosk.py").exists())
        self.assertFalse((root / "installed/observer/manifest.json").exists())
        self.assertTrue(all("tm-camera-monitor.service" not in call.args[0] and "mediamtx.service" not in call.args[0] for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
