import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


class HTTPDeploymentTests(unittest.TestCase):
    def test_dashboard_and_recording_api_start_without_a_graphical_session(self):
        with tempfile.TemporaryDirectory(prefix="tm-http-test-", dir=Path(__file__).parent) as directory:
            opener = build_opener(ProxyHandler({}))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = dict(os.environ, PORT=str(port), RECORDINGS_DIR=directory,
                       SETTINGS_FILE=str(Path(directory) / "settings.json"),
                       HLS_CACHE_DIR=str(Path(directory) / ".hls"),
                       PYTHONUNBUFFERED="1",
                       ESP32_MQTT_HOST="127.0.0.1", ESP32_MQTT_PORT="1", AI_DETECTION_ENABLED="0")
            child = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
            try:
                base = f"http://127.0.0.1:{port}"
                last_error = None
                for attempt in range(50):
                    self.assertIsNone(child.poll(), "Dashboard server exited during startup")
                    try:
                        with opener.open(base + "/api/recording/status", timeout=1) as response:
                            status = json.load(response)
                        break
                    except (URLError, TimeoutError, ConnectionError) as exc:
                        last_error = exc
                        time.sleep(0.1)
                else:
                    child.terminate()
                    logs, _ = child.communicate(timeout=5)
                    self.fail(f"Dashboard HTTP startup timed out: {last_error}; server log: {logs}")
                self.assertEqual(status["mode"], "occupied")
                self.assertEqual(status["storageLimitGB"], 35)
                self.assertFalse(status["running"], "No ESP32 status must not start a recording")
                with opener.open(base, timeout=2) as response:
                    html = response.read().decode()
                for element in ('id="personCount"', 'id="cameraFrame"', 'id="recordingsList"'):
                    self.assertIn(element, html)
                self.assertIn("35 GB", html)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                if child.stdout:
                    child.stdout.close()


if __name__ == "__main__":
    unittest.main()
