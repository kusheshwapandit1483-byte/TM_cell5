import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "server.py"
spec = importlib.util.spec_from_file_location("camera_server", MODULE_PATH)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tm-recorder-test-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.clean_temp)
        self.overrides = patch.multiple(
            server, RECORDINGS_DIR=self.root, HLS_CACHE_DIR=self.root / ".hls",
            SETTINGS_FILE=self.root / "settings.json", STORAGE_LIMIT_BYTES=1000,
            recorder_segments_file=self.root / ".recorder-segments.csv",
            recorder_process=None, recorder_log=None, recorder_started_at=None,
            recorder_session_prefix=None, recording_wanted=False,
            storage_used_bytes=0, storage_over_limit=False,
            shutdown_event=threading.Event(), recorder_wake=threading.Event(),
            storage_wake=threading.Event(), esp32_monitor=None,
        )
        self.overrides.start()
        self.addCleanup(self.overrides.stop)

    def clean_temp(self):
        # Temporary cleanup is confined to the explicitly created test directory.
        assert self.root.parent == Path(__file__).resolve().parent
        assert self.root.name.startswith("tm-recorder-test-")
        self.temp.cleanup()

    def clip(self, name, size, age):
        path = self.root / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def status(self, count, **extra):
        return {"connected": True, "ageSeconds": 0, "data": {"occupancy": count, **extra}}

    def test_presence_override_starts_and_confirmed_clear_stops(self):
        monitor = Mock()
        server.esp32_monitor = monitor
        with patch.object(server, "start_recorder") as start, patch.object(server, "stop_recorder") as stop:
            monitor.snapshot.return_value = self.status(0, presence_ready_for_empty=True)
            server.ensure_recorder_running()
            start.assert_not_called()
            monitor.snapshot.return_value = self.status(1, counted_occupancy=0, presence_override=True)
            server.ensure_recorder_running()
            start.assert_called_once()
            server.recorder_process = Mock(poll=Mock(return_value=None))
            monitor.snapshot.return_value = self.status(0, presence_ready_for_empty=True, vacancy_clear_active=True)
            server.ensure_recorder_running()
            stop.assert_not_called()
            monitor.snapshot.return_value = self.status(0, presence_ready_for_empty=True, vacancy_clear_active=False)
            server.ensure_recorder_running()
            stop.assert_called_once()

    def test_unknown_stale_and_malformed_status_hold_previous_decision(self):
        bad = [
            {}, {"connected": False, "ageSeconds": 0, "data": {"occupancy": 0}},
            {"connected": True, "ageSeconds": 8, "data": {"occupancy": 0}},
            self.status(True), self.status(-1), self.status("0"), self.status(0.0),
            self.status(0, presence_ready_for_empty=False),
            self.status(0, presence_ready_for_empty="false"),
        ]
        for snapshot in bad:
            for previous in (True, False):
                with self.subTest(snapshot=snapshot, previous=previous):
                    self.assertEqual(server.occupancy_recording_decision(snapshot, previous)[0], previous)
        self.assertTrue(server.occupancy_recording_decision(self.status(3), False)[0])

    def test_oldest_first_and_no_day_expiry(self):
        old = self.clip("old.mp4", 450, 10 * 86400)
        middle = self.clip("middle.mp4", 450, 100)
        newest = self.clip("newest.mp4", 450, 10)
        self.assertEqual(server.cleanup_old_recordings(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(middle.exists() and newest.exists())
        self.assertLessEqual(server.storage_used_bytes, 1000)
        os.utime(middle, (1, 1))
        self.assertEqual(server.cleanup_old_recordings(), 0, "Old age alone must not delete history")

    def test_cache_counts_toward_cap_and_is_removed_with_clip(self):
        old = self.clip("old.mp4", 300, 100)
        newest = self.clip("new.mp4", 300, 10)
        cache = server.HLS_CACHE_DIR / old.stem
        cache.mkdir(parents=True)
        (cache / "segment.ts").write_bytes(b"x" * 500)
        self.assertEqual(server.cleanup_old_recordings(), 1)
        self.assertFalse(old.exists() or cache.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(server.storage_used_bytes, 300)

    def test_only_closed_segments_are_listed_and_pruned(self):
        closed = self.clip("session_closed.mp4", 600, 100)
        active = self.clip("session_active.mp4", 600, 200)
        server.recorder_process = Mock(poll=Mock(return_value=None))
        server.recorder_session_prefix = "session_"
        server.recorder_segments_file.write_text(f'"{closed.name}",0,1\n')
        self.assertEqual(server.active_recording_paths(), {active})
        self.assertEqual([c["name"] for c in server.list_recordings()], [closed.name])
        self.assertEqual(server.cleanup_old_recordings(), 1)
        self.assertTrue(active.exists())
        self.assertFalse(closed.exists())

    def test_oversized_active_segment_is_finalized_before_deletion(self):
        active = self.clip("session_active.mp4", 1100, 100)
        server.recorder_process = Mock(poll=Mock(return_value=None))
        server.recorder_session_prefix = "session_"
        self.assertEqual(server.cleanup_old_recordings(), 0)
        self.assertTrue(active.exists() and server.storage_over_limit)
        monitor = Mock(snapshot=Mock(return_value=self.status(1)))
        server.esp32_monitor = monitor
        with patch.object(server, "stop_recorder") as stop, patch.object(server, "start_recorder") as start:
            server.ensure_recorder_running()
            stop.assert_called_once()
            start.assert_not_called()
        server.recorder_process = None
        self.assertEqual(server.cleanup_old_recordings(), 1)
        self.assertFalse(server.storage_over_limit)

    def test_legacy_retention_settings_do_not_override_size_policy(self):
        server.SETTINGS_FILE.write_text('{"retentionDays":1}')
        self.assertNotIn("retentionDays", server.load_settings())
        self.assertEqual(server.load_settings()["storageLimitGB"], 35)

    def test_playback_preparation_files_are_protected_during_cleanup(self):
        selected = self.clip("selected.mp4", 400, 100)
        newer = self.clip("newer.mp4", 400, 10)
        temporary = server.HLS_CACHE_DIR / ".selected.tmp"
        temporary.mkdir(parents=True)
        (temporary / "segment.ts").write_bytes(b"x" * 300)
        server.cleanup_old_recordings(protected_paths={selected}, protected_cache_dirs={temporary})
        self.assertTrue(selected.exists() and temporary.exists())
        self.assertFalse(newer.exists())
        self.assertLessEqual(server.storage_used_bytes, 1000)

    def test_monitor_rejects_non_object_and_ignores_retained_status(self):
        monitor = server.ESP32StatusMonitor("localhost", 1883, "eagle/system/status")
        with self.assertRaises(ValueError):
            monitor.set_latest([])
        body = server.mqtt_string(monitor.topic) + b'{"occupancy":1}'
        monitor.handle_publish(0x31, body, Mock())
        self.assertIsNone(monitor.latest)
        monitor.handle_publish(0x30, body, Mock())
        self.assertEqual(monitor.latest["occupancy"], 1)
        self.assertTrue(server.recorder_wake.is_set())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    def test_real_ffmpeg_rotation_and_graceful_stop_produce_playable_clips(self):
        fixture = self.root / "source.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=160x120:r=10", "-t", "3", "-c:v", "libx264", "-g", "10",
            "-pix_fmt", "yuv420p", str(fixture)
        ], check=True, timeout=30)
        # Test the real recorder and segment CSV on a synthetic local source, without RTSP/network.
        popen = subprocess.Popen
        def local_source(cmd, **kwargs):
            cmd = list(cmd)
            if "-rtsp_transport" not in cmd:
                return popen(cmd, **kwargs)
            idx = cmd.index("-rtsp_transport")
            del cmd[idx:idx+2]
            idx = cmd.index("-i")
            cmd[idx:idx] = ["-re", "-stream_loop", "-1"]
            return popen(cmd, **kwargs)
        with patch.multiple(server, RECORDING_SOURCE=str(fixture), SEGMENT_SECONDS=1,
                            STORAGE_LIMIT_BYTES=35_000_000_000):
            with patch.object(server.subprocess, "Popen", side_effect=local_source):
                try:
                    result = server.start_recorder()
                    self.assertTrue(result["ok"], result)
                    time.sleep(2.2)
                    active = server.active_recording_paths()
                    self.assertTrue(active)
                    self.assertTrue(server.list_recordings(), "A completed segment should be available")
                    self.assertTrue(server.stop_recorder()["ok"])
                    clips = list(self.root.glob("data_*.mp4"))
                    self.assertGreaterEqual(len(clips), 2)
                    for clip in clips:
                        checked = subprocess.run([
                            "ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(clip)
                        ], capture_output=True, text=True, check=True, timeout=10)
                        self.assertGreater(float(checked.stdout.strip()), 0)
                    prepared = server.ensure_recording_hls(clips[0].name)
                    self.assertTrue(prepared["ok"], prepared)
                    self.assertTrue((server.HLS_CACHE_DIR / clips[0].stem / "index.m3u8").exists())
                finally:
                    server.stop_recorder()


if __name__ == "__main__":
    unittest.main()
