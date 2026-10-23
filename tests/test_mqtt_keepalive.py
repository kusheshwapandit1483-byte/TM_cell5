import importlib.util
from pathlib import Path
import socket
import threading
import io
import json
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mqtt_server", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
spec = importlib.util.spec_from_file_location("apply_mqtt", ROOT / "scripts/apply-mqtt-fix.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class KeepaliveTests(unittest.TestCase):
    def session(self, incoming):
        clock = [0]
        stop = threading.Event()
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        monitor = server.ESP32StatusMonitor("localhost", 1883, "eagle/system/status")
        handshakes = iter([(0x20, b"\x00\x00"), (0x90, b"\x00\x01\x00")])
        phase = [0]

        def receive(_sock):
            if phase[0] < 2:
                phase[0] += 1
                return next(handshakes)
            clock[0] += 1
            if clock[0] >= 65:
                stop.set()
            if incoming == "timeout":
                raise socket.timeout()
            return (0x30, b"status")

        with patch.object(server, "shutdown_event", stop), \
             patch.object(server.socket, "create_connection", return_value=sock), \
             patch.object(server, "read_mqtt_packet", side_effect=receive), \
             patch.object(server.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(monitor, "handle_publish"):
            monitor.mqtt_session()
        return [call.args[0] for call in sock.sendall.call_args_list].count(b"\xc0\x00")

    def test_continuous_status_messages_do_not_suppress_keepalive(self):
        self.assertEqual(self.session("publish"), 3)

    def test_idle_connection_also_sends_keepalive(self):
        self.assertEqual(self.session("timeout"), 3)

    def test_client_ids_are_unique_across_devices_even_with_equal_process_ids(self):
        with patch.object(server.os, "getpid", return_value=1204):
            first = server.ESP32StatusMonitor("localhost", 1883, "status")
            second = server.ESP32StatusMonitor("localhost", 1883, "status")
        self.assertNotEqual(first.client_id, second.client_id)
        self.assertLessEqual(len(first.client_id), 23)
        self.assertEqual(first.connect_packet(), first.connect_packet())
        self.assertIn(first.client_id.encode(), first.connect_packet())

    def apply_fixture(self, healthy):
        clock = [0]
        opener = Mock()

        def response(url, **kwargs):
            data = {"mode": "occupied", "storageLimitGB": 35, "recordingsDir": "/retained/archive"} if url.endswith("recording/status") else {
                "connected": healthy, "ageSeconds": 0 if healthy else 10, "error": None if healthy else "MQTT socket closed"}
            return io.BytesIO(json.dumps(data).encode())

        opener.open.side_effect = response
        with patch.object(apply.os, "geteuid", return_value=0, create=True), \
             patch.object(apply, "show", return_value=str(ROOT)), \
             patch.object(apply, "wake_display") as wake, \
             patch.object(apply, "build_opener", return_value=opener), \
             patch.object(apply.subprocess, "run") as run, \
             patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(apply.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch("builtins.print"):
            if healthy:
                apply.main()
                self.assertGreaterEqual(clock[0], 70)
            else:
                with self.assertRaisesRegex(RuntimeError, "did not remain fresh"):
                    apply.main()
            wake.assert_called_once()
            self.assertEqual([call.args[0] for call in run.call_args_list], [["systemctl", "restart", "tm-camera-monitor.service"]])

    def test_application_verifies_seventy_seconds_and_preserves_kiosk(self):
        self.apply_fixture(True)

    def test_failed_mqtt_check_still_attempts_display_wake_and_reports_failure(self):
        self.apply_fixture(False)


if __name__ == "__main__":
    unittest.main()
