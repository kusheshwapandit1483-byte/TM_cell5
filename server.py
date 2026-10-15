#!/usr/bin/env python3
import csv
import json
import os
import signal
import shutil
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", ROOT / "recordings")).resolve()
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", ROOT / "settings.json")).resolve()
HLS_CACHE_DIR = Path(os.environ.get("HLS_CACHE_DIR", RECORDINGS_DIR / ".hls")).resolve()
RECORDING_SOURCE = os.environ.get("RECORDING_SOURCE", "rtsp://127.0.0.1:8554/pramacam")
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "1800"))
STORAGE_LIMIT_BYTES = 35_000_000_000  # 35 GB, decimal; includes playback cache.
ESP32_STATUS_MAX_AGE_SECONDS = 5
STORAGE_CHECK_SECONDS = 10
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
LIVE_PREVIEW_FPS = os.environ.get("LIVE_PREVIEW_FPS", "8")
LIVE_PREVIEW_HEIGHT = os.environ.get("LIVE_PREVIEW_HEIGHT", "720")
LIVE_PREVIEW_QUALITY = os.environ.get("LIVE_PREVIEW_QUALITY", "6")
HLS_SEGMENT_SECONDS = int(os.environ.get("HLS_SEGMENT_SECONDS", "6"))
PORT = int(os.environ.get("PORT", "8080"))
ESP32_MQTT_HOST = os.environ.get("ESP32_MQTT_HOST", "192.168.10.1")
ESP32_MQTT_PORT = int(os.environ.get("ESP32_MQTT_PORT", "1883"))
ESP32_MQTT_TOPIC = os.environ.get("ESP32_MQTT_TOPIC", "eagle/system/status")
AI_DETECTION_ENABLED = os.environ.get("AI_DETECTION_ENABLED", "0").lower() in ("1", "true", "yes", "on")
AI_DETECTION_BACKEND = os.environ.get("AI_DETECTION_BACKEND", "opencv-onnx")
AI_DETECTION_SOURCE = os.environ.get("AI_DETECTION_SOURCE", RECORDING_SOURCE)
AI_DETECTION_MODEL = os.environ.get("AI_DETECTION_MODEL", "")
AI_DETECTION_INPUT_SIZE = int(os.environ.get("AI_DETECTION_INPUT_SIZE", "640"))
AI_DETECTION_CONFIDENCE = float(os.environ.get("AI_DETECTION_CONFIDENCE", "0.35"))
AI_DETECTION_IOU = float(os.environ.get("AI_DETECTION_IOU", "0.45"))
AI_DETECTION_INTERVAL = float(os.environ.get("AI_DETECTION_INTERVAL", "0.15"))
AI_DETECTION_HOLD_SECONDS = float(os.environ.get("AI_DETECTION_HOLD_SECONDS", "1.0"))

recorder_process = None
recorder_started_at = None
recorder_log = None
recorder_lock = threading.RLock()
storage_lock = threading.RLock()
recorder_wake = threading.Event()
storage_wake = threading.Event()
recorder_session_prefix = None
recorder_segments_file = RECORDINGS_DIR / ".recorder-segments.csv"
recording_wanted = False
recording_reason = "Waiting for ESP32 occupancy"
storage_used_bytes = 0
storage_over_limit = False
shutdown_event = threading.Event()
esp32_monitor = None
person_detection_monitor = None


def mqtt_remaining_length(length):
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 128
        encoded.append(digit)
        if length == 0:
            return bytes(encoded)


def mqtt_string(value):
    payload = value.encode("utf-8")
    return len(payload).to_bytes(2, "big") + payload


def read_exact(sock, length):
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise ConnectionError("MQTT socket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def read_mqtt_packet(sock):
    header = read_exact(sock, 1)[0]
    multiplier = 1
    remaining = 0
    while True:
        digit = read_exact(sock, 1)[0]
        remaining += (digit & 127) * multiplier
        if (digit & 128) == 0:
            break
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise ValueError("Malformed MQTT remaining length")
    return header, read_exact(sock, remaining)


class ESP32StatusMonitor:
    def __init__(self, host, port, topic):
        self.host = host
        self.port = port
        self.topic = topic
        self.lock = threading.Lock()
        self.latest = None
        self.connected = False
        self.last_error = "not connected"
        self.last_message_at = None
        self.last_message_monotonic = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            payload = dict(self.latest or {})
            last_message_at = self.last_message_at
            age_seconds = (time.monotonic() - self.last_message_monotonic
                           if self.last_message_monotonic is not None else None)
            return {
                "connected": self.connected,
                "host": self.host,
                "port": self.port,
                "topic": self.topic,
                "lastMessageAt": last_message_at,
                "ageSeconds": age_seconds,
                "error": self.last_error,
                "data": payload,
            }

    def set_status(self, connected, error=None):
        with self.lock:
            self.connected = connected
            self.last_error = None if connected else error
        recorder_wake.set()

    def set_latest(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("ESP32 status must be a JSON object")
        with self.lock:
            self.latest = payload
            self.connected = True
            self.last_error = None
            self.last_message_at = time.time()
            self.last_message_monotonic = time.monotonic()
        recorder_wake.set()

    def connect_packet(self):
        client_id = f"tm-dashboard-{os.getpid()}"
        variable_header = mqtt_string("MQTT") + bytes([4, 2]) + (30).to_bytes(2, "big")
        payload = mqtt_string(client_id)
        body = variable_header + payload
        return bytes([0x10]) + mqtt_remaining_length(len(body)) + body

    def subscribe_packet(self):
        packet_id = (1).to_bytes(2, "big")
        body = packet_id + mqtt_string(self.topic) + bytes([0])
        return bytes([0x82]) + mqtt_remaining_length(len(body)) + body

    def send_ping(self, sock):
        sock.sendall(b"\xc0\x00")

    def handle_publish(self, header, body, sock):
        if len(body) < 2:
            return
        topic_len = int.from_bytes(body[:2], "big")
        topic_start = 2
        topic_end = topic_start + topic_len
        topic = body[topic_start:topic_end].decode("utf-8", errors="replace")
        qos = (header >> 1) & 0x03
        payload_start = topic_end
        packet_id = None
        if qos:
            packet_id = body[payload_start:payload_start + 2]
            payload_start += 2
        if topic == self.topic and not (header & 0x01):
            # A retained historical status is not evidence of current cell occupancy.
            payload_text = body[payload_start:].decode("utf-8", errors="replace")
            self.set_latest(json.loads(payload_text))
        if qos == 1 and packet_id:
            sock.sendall(b"\x40\x02" + packet_id)

    def mqtt_session(self):
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            sock.settimeout(10)
            sock.sendall(self.connect_packet())
            header, body = read_mqtt_packet(sock)
            if header >> 4 != 2 or len(body) < 2 or body[1] != 0:
                raise ConnectionError("MQTT CONNACK failed")
            sock.sendall(self.subscribe_packet())
            header, body = read_mqtt_packet(sock)
            if header >> 4 != 9:
                raise ConnectionError("MQTT SUBACK failed")
            self.set_status(True, None)
            last_ping = time.time()
            while not shutdown_event.is_set():
                try:
                    header, body = read_mqtt_packet(sock)
                except socket.timeout:
                    now = time.time()
                    if now - last_ping >= 20:
                        self.send_ping(sock)
                        last_ping = now
                    continue
                packet_type = header >> 4
                if packet_type == 3:
                    self.handle_publish(header, body, sock)
                elif packet_type == 13:
                    continue

    def run(self):
        while not shutdown_event.is_set():
            try:
                self.mqtt_session()
            except Exception as exc:
                self.set_status(False, str(exc))
                shutdown_event.wait(5)



class PersonDetectionMonitor:
    def __init__(self, source, backend, model_path, input_size, confidence, iou, interval, hold_seconds):
        self.source = source
        self.backend = backend
        self.model_path = model_path
        self.input_size = input_size
        self.confidence = confidence
        self.iou = iou
        self.interval = interval
        self.hold_seconds = hold_seconds
        self.lock = threading.Lock()
        self.running = False
        self.last_error = None
        self.last_update_at = None
        self.frame_width = None
        self.frame_height = None
        self.detections = []
        self.last_positive_detections = []
        self.last_positive_at = None
        self.held = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            last_update_at = self.last_update_at
            age_seconds = time.time() - last_update_at if last_update_at else None
            return {
                "enabled": AI_DETECTION_ENABLED,
                "running": self.running,
                "backend": self.backend,
                "source": self.source,
                "model": self.model_path,
                "inputSize": self.input_size,
                "confidenceThreshold": self.confidence,
                "holdSeconds": self.hold_seconds,
                "held": self.held,
                "frameWidth": self.frame_width,
                "frameHeight": self.frame_height,
                "updatedAt": last_update_at,
                "ageSeconds": age_seconds,
                "error": self.last_error,
                "detections": list(self.detections),
            }

    def set_error(self, message):
        with self.lock:
            self.running = False
            self.last_error = message

    def set_detections(self, frame_width, frame_height, detections):
        now = time.time()
        visible_detections = detections
        held = False
        if detections:
            self.last_positive_detections = detections
            self.last_positive_at = now
        elif self.last_positive_at and now - self.last_positive_at <= self.hold_seconds:
            visible_detections = self.last_positive_detections
            held = True

        with self.lock:
            self.running = True
            self.last_error = None
            self.last_update_at = now
            self.frame_width = int(frame_width) if frame_width else None
            self.frame_height = int(frame_height) if frame_height else None
            self.detections = visible_detections
            self.held = held

    def load_detector(self, cv2):
        if self.backend == "hog":
            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            return hog
        if self.backend != "opencv-onnx":
            raise ValueError(f"Unsupported AI_DETECTION_BACKEND: {self.backend}")
        if not self.model_path:
            raise ValueError("Set AI_DETECTION_MODEL to a person-capable ONNX model path")
        model = Path(self.model_path).expanduser()
        if not model.exists():
            raise FileNotFoundError(f"AI model not found: {model}")
        return cv2.dnn.readNetFromONNX(str(model))

    def detect_hog(self, cv2, detector, frame):
        height, width = frame.shape[:2]
        boxes, weights = detector.detectMultiScale(frame, winStride=(8, 8), padding=(16, 16), scale=1.05)
        detections = []
        for box, confidence in zip(boxes, weights):
            confidence = float(confidence)
            if confidence < self.confidence:
                continue
            x, y, w, h = [int(value) for value in box]
            detections.append(self.make_detection(x, y, w, h, confidence, width, height))
        return detections

    def letterbox_frame(self, cv2, frame):
        height, width = frame.shape[:2]
        scale = min(self.input_size / width, self.input_size / height)
        resized_width = int(round(width * scale))
        resized_height = int(round(height * scale))
        pad_width = self.input_size - resized_width
        pad_height = self.input_size - resized_height
        pad_left = pad_width // 2
        pad_right = pad_width - pad_left
        pad_top = pad_height // 2
        pad_bottom = pad_height - pad_top
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        canvas = cv2.copyMakeBorder(
            resized,
            pad_top,
            pad_bottom,
            pad_left,
            pad_right,
            cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        return canvas, scale, pad_left, pad_top

    def detect_onnx(self, cv2, detector, frame):
        height, width = frame.shape[:2]
        input_frame, scale, pad_x, pad_y = self.letterbox_frame(cv2, frame)
        blob = cv2.dnn.blobFromImage(
            input_frame,
            1 / 255.0,
            (self.input_size, self.input_size),
            swapRB=True,
            crop=False,
        )
        detector.setInput(blob)
        output_names = detector.getUnconnectedOutLayersNames()
        outputs = detector.forward(output_names)
        rows = outputs[0]
        while len(rows.shape) > 2:
            rows = rows[0]
        if rows.shape[0] in (84, 85) and rows.shape[1] > rows.shape[0]:
            rows = rows.transpose()

        boxes = []
        scores = []
        for row in rows:
            parsed = self.parse_yolo_person_row(row, scale, pad_x, pad_y)
            if not parsed:
                continue
            x, y, w, h, confidence = parsed
            boxes.append([int(x), int(y), int(w), int(h)])
            scores.append(float(confidence))

        indexes = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, self.iou)
        if len(indexes) == 0:
            return []
        detections = []
        for index in indexes.flatten():
            x, y, w, h = boxes[int(index)]
            detections.append(self.make_detection(x, y, w, h, scores[int(index)], width, height))
        return detections

    def scale_yolo_box(self, x, y, width, height, scale, pad_x, pad_y):
        return (
            (x - pad_x) / scale,
            (y - pad_y) / scale,
            width / scale,
            height / scale,
        )

    def parse_yolo_person_row(self, row, scale, pad_x, pad_y):
        values = row.tolist()
        if len(values) == 6:
            class_id = int(values[5])
            confidence = float(values[4])
            if class_id != 0 or confidence < self.confidence:
                return None
            x1, y1, x2, y2 = values[:4]
            return (*self.scale_yolo_box(x1, y1, x2 - x1, y2 - y1, scale, pad_x, pad_y), confidence)

        if len(values) >= 85:
            confidence = float(values[4]) * float(values[5])
        elif len(values) >= 84:
            confidence = float(values[4])
        else:
            return None

        if confidence < self.confidence:
            return None
        cx, cy, w, h = [float(value) for value in values[:4]]
        x = cx - w / 2
        y = cy - h / 2
        return (*self.scale_yolo_box(x, y, w, h, scale, pad_x, pad_y), confidence)

    def make_detection(self, x, y, width, height, confidence, frame_width, frame_height):
        x = max(0, min(int(x), frame_width - 1))
        y = max(0, min(int(y), frame_height - 1))
        width = max(1, min(int(width), frame_width - x))
        height = max(1, min(int(height), frame_height - y))
        return {
            "class": "person",
            "confidence": round(float(confidence), 4),
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "xNorm": x / frame_width,
            "yNorm": y / frame_height,
            "widthNorm": width / frame_width,
            "heightNorm": height / frame_height,
        }

    def run(self):
        if not AI_DETECTION_ENABLED:
            self.set_error("AI detection disabled. Start with AI_DETECTION_ENABLED=1 to enable person detection.")
            return
        try:
            import cv2
        except Exception as exc:
            self.set_error(f"OpenCV import failed: {exc}")
            return

        try:
            detector = self.load_detector(cv2)
        except Exception as exc:
            self.set_error(str(exc))
            return

        while not shutdown_event.is_set():
            capture = cv2.VideoCapture(self.source)
            if not capture.isOpened():
                self.set_error(f"Could not open detection source: {self.source}")
                shutdown_event.wait(3)
                continue
            try:
                while not shutdown_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        self.set_error("Detection source frame read failed")
                        break
                    if self.backend == "hog":
                        detections = self.detect_hog(cv2, detector, frame)
                    else:
                        detections = self.detect_onnx(cv2, detector, frame)
                    height, width = frame.shape[:2]
                    self.set_detections(width, height, detections)
                    shutdown_event.wait(self.interval)
            except Exception as exc:
                self.set_error(f"Detection failed: {exc}")
                shutdown_event.wait(2)
            finally:
                capture.release()
def load_settings():
    # Fixed policy; legacy retentionDays settings no longer expire recording history.
    return {"recordingMode": "occupied", "storageLimitGB": 35,
            "storageLimitBytes": STORAGE_LIMIT_BYTES}


def save_settings(settings):
    policy = load_settings()
    SETTINGS_FILE.write_text(json.dumps(policy, indent=2), encoding="utf-8")
    return policy


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def recorder_running():
    global recorder_process, recorder_started_at, recorder_log
    if recorder_process is None:
        return False
    if recorder_process.poll() is None:
        return True
    recorder_process = None
    recorder_started_at = None
    if recorder_log:
        recorder_log.close()
        recorder_log = None
    return False


def recording_status():
    with recorder_lock:
        return {
            "running": recorder_running(),
            "mode": "occupied",
            "source": RECORDING_SOURCE,
            "segmentSeconds": SEGMENT_SECONDS,
            "recordingsDir": str(RECORDINGS_DIR),
            "storageLimitBytes": STORAGE_LIMIT_BYTES,
            "storageLimitGB": 35,
            "storageUsedBytes": storage_used_bytes,
            "storageOverLimit": storage_over_limit,
            "wanted": recording_wanted,
            "reason": recording_reason,
            "startedAt": recorder_started_at,
        }


def active_recording_paths():
    """Only ffmpeg's completed-segment CSV establishes a current-session clip is closed."""
    with recorder_lock:
        if not recorder_running() or not recorder_session_prefix:
            return set()
        completed = set()
        try:
            with recorder_segments_file.open(newline="", encoding="utf-8") as stream:
                for row in csv.reader(stream):
                    if len(row) >= 3:
                        completed.add(Path(row[0]).name)
        except (OSError, csv.Error):
            pass
        return {p for p in RECORDINGS_DIR.glob(recorder_session_prefix + "*.mp4")
                if p.name not in completed}


def storage_bytes():
    # Count each real file once, including a cache located outside the recordings folder.
    files = set()
    for root in (RECORDINGS_DIR, HLS_CACHE_DIR):
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                files.add(path.resolve())
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def remove_playback_cache(path):
    # Do not follow cache symlinks or recurse outside the configured cache directory.
    root = HLS_CACHE_DIR.resolve()
    target = path.resolve()
    if not path.is_symlink() and target != root and root in target.parents:
        shutil.rmtree(path, ignore_errors=True)


def cleanup_old_recordings(protected_paths=(), protected_cache_dirs=()):
    """Retain history by size, deleting the oldest CLOSED MP4 and its playback cache."""
    global storage_used_bytes, storage_over_limit
    with storage_lock:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        HLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        active = active_recording_paths() | set(protected_paths)
        clips = []
        for path in RECORDINGS_DIR.glob("*.mp4"):
            try:
                if not path.is_symlink():
                    clips.append((path.stat().st_mtime, path))
            except OSError:
                pass
        stems = {path.stem for _, path in clips}
        # The same lock covers HLS preparation, so no in-progress cache is removed here.
        for path in HLS_CACHE_DIR.iterdir():
            if path.is_dir() and path.name not in stems and path not in protected_cache_dirs:
                remove_playback_cache(path)
        used = storage_bytes()
        deleted = 0
        for _, path in sorted(clips, key=lambda item: (item[0], item[1].name)):
            if used <= STORAGE_LIMIT_BYTES:
                break
            # A new ffmpeg segment/session can begin after the initial directory snapshot.
            if path in active or path in active_recording_paths():
                continue
            try:
                path.unlink()
            except OSError:
                continue
            remove_playback_cache(HLS_CACHE_DIR / path.stem)
            deleted += 1
            used = storage_bytes()
        storage_used_bytes = used
        storage_over_limit = used > STORAGE_LIMIT_BYTES
    if storage_over_limit:
        # If only the open segment prevents reclamation, finalize it before pruning.
        recorder_wake.set()
    return deleted


def resolve_recording_path(name):
    safe_name = Path(str(name or "")).name
    recording_path = (RECORDINGS_DIR / safe_name).resolve()
    if (
        RECORDINGS_DIR not in recording_path.parents
        or recording_path.suffix.lower() != ".mp4"
        or not recording_path.exists()
        or not recording_path.is_file()
    ):
        return None
    return recording_path


def start_recorder():
    global recorder_process, recorder_started_at, recorder_log, recorder_session_prefix

    with recorder_lock:
        if shutdown_event.is_set() or storage_over_limit:
            return {"ok": False, "message": "Recorder waiting for available storage", "status": recording_status()}
        if recorder_running():
            return {"ok": True, "message": "Recorder already running", "status": recording_status()}

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        # Unique session prefix prevents overwriting clips after a rapid stop/restart.
        recorder_session_prefix = "data_" + str(time.time_ns()) + "_"
        pattern = RECORDINGS_DIR / (recorder_session_prefix + "%Y-%m-%d_%H-%M-%S.mp4")
        recorder_segments_file.unlink(missing_ok=True)
        log_path = RECORDINGS_DIR / "recorder.log"
        if log_path.exists() and log_path.stat().st_size > 1_048_576:
            log_path.replace(RECORDINGS_DIR / "recorder.previous.log")
        recorder_log = open(log_path, "ab")

        cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            RECORDING_SOURCE,
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(SEGMENT_SECONDS),
            "-segment_list",
            str(recorder_segments_file),
            "-segment_list_type",
            "csv",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(pattern),
        ]

        try:
            recorder_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=recorder_log,
                stderr=recorder_log,
                cwd=str(ROOT),
            )
        except Exception as exc:
            recorder_log.close()
            recorder_log = None
            return {"ok": False, "message": f"Could not start ffmpeg: {exc}", "status": recording_status()}

        time.sleep(0.8)
        if recorder_process.poll() is not None:
            recorder_process = None
            recorder_log.close()
            recorder_log = None
            return {
                "ok": False,
                "message": "ffmpeg stopped immediately. Check recordings/recorder.log",
                "status": recording_status(),
            }

        recorder_started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {"ok": True, "message": "Occupied-cell recording started", "status": recording_status()}


def stop_recorder():
    global recorder_process, recorder_started_at, recorder_log

    with recorder_lock:
        if not recorder_running():
            return {"ok": True, "message": "Recorder is not running", "status": recording_status()}

        process = recorder_process
        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
            process.wait(timeout=8)
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()

        recorder_process = None
        recorder_started_at = None
        if recorder_log:
            recorder_log.close()
            recorder_log = None
        storage_wake.set()
        return {"ok": True, "message": "Recording stopped", "status": recording_status()}


def occupancy_recording_decision(snapshot, previous):
    """Fresh positive count starts; fresh confirmed-clear zero stops. Unknown holds state."""
    data = snapshot.get("data") or {}
    age = snapshot.get("ageSeconds")
    if (not snapshot.get("connected") or age is None or age < 0
            or age > ESP32_STATUS_MAX_AGE_SECONDS):
        return previous, "ESP32 status unavailable; holding recording state"
    if not isinstance(data, dict):
        return previous, "Invalid ESP32 occupancy; holding recording state"
    occupancy = data.get("occupancy")
    if isinstance(occupancy, bool) or not isinstance(occupancy, int) or occupancy < 0:
        return previous, "Invalid ESP32 occupancy; holding recording state"
    if occupancy > 0:
        return True, "Person inside cell"
    for field in ("presence_ready_for_empty", "vacancy_clear_active"):
        if field in data and not isinstance(data[field], bool):
            return previous, "Invalid clear status; holding recording state"
    if data.get("presence_ready_for_empty") is False or data.get("vacancy_clear_active") is True:
        return previous, "Waiting for confirmed clear cell"
    return False, "Cell empty"


def ensure_recorder_running():
    global recording_wanted, recording_reason
    if shutdown_event.is_set():
        return
    snapshot = esp32_monitor.snapshot() if esp32_monitor else {}
    with recorder_lock:
        recording_wanted, recording_reason = occupancy_recording_decision(snapshot, recording_wanted)
        running = recorder_running()
        if storage_over_limit:
            if running:
                stop_recorder()
            recording_reason = "Reclaiming recording storage"
            storage_wake.set()
        elif recording_wanted:
            if not running:
                start_recorder()
        elif running:
            stop_recorder()


def recorder_control_loop():
    while not shutdown_event.is_set():
        ensure_recorder_running()
        recorder_wake.wait(1)
        recorder_wake.clear()


def maintenance_loop():
    while not shutdown_event.is_set():
        cleanup_old_recordings()
        storage_wake.wait(STORAGE_CHECK_SECONDS)
        storage_wake.clear()


def list_recordings():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    active = active_recording_paths()
    files = []
    candidates = []
    for path in RECORDINGS_DIR.glob("*.mp4"):
        try:
            if path not in active and not path.is_symlink():
                candidates.append((path.stat().st_mtime, path))
        except OSError:
            pass
    for _, path in sorted(candidates, reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.name,
                "url": f"/recordings/{path.name}",
                "sizeBytes": stat.st_size,
                "modifiedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)),
            }
        )
    return files



def stream_live_mjpeg(handler):
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        RECORDING_SOURCE,
        "-an",
        "-vf",
        f"fps={LIVE_PREVIEW_FPS},scale=-2:{LIVE_PREVIEW_HEIGHT}",
        "-q:v",
        LIVE_PREVIEW_QUALITY,
        "-f",
        "mjpeg",
        "pipe:1",
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(ROOT),
        )
    except Exception as exc:
        handler.send_error(HTTPStatus.SERVICE_UNAVAILABLE, f"Could not start live preview: {exc}")
        return

    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.end_headers()

    buffer = bytearray()
    try:
        while not shutdown_event.is_set():
            chunk = process.stdout.read(8192) if process.stdout else b""
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break
                frame_bytes = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                handler.wfile.write(b"--frame\r\n")
                handler.wfile.write(b"Content-Type: image/jpeg\r\n")
                handler.wfile.write(f"Content-Length: {len(frame_bytes)}\r\n\r\n".encode("ascii"))
                handler.wfile.write(frame_bytes)
                handler.wfile.write(b"\r\n")
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except Exception:
            process.kill()

def ensure_recording_hls(name):
    with storage_lock:
        if resolve_recording_path(name) in active_recording_paths():
            return {"ok": False, "message": "Recording is still being written"}
        result = prepare_recording_hls(name)
        storage_wake.set()
        return result


def prepare_recording_hls(name):
    recording_path = resolve_recording_path(name)
    if not recording_path:
        return {"ok": False, "message": "Recording not found"}

    HLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    hls_dir = HLS_CACHE_DIR / recording_path.stem
    playlist_path = hls_dir / "index.m3u8"
    if playlist_path.exists() and playlist_path.stat().st_mtime >= recording_path.stat().st_mtime:
        return {
            "ok": True,
            "message": "HLS playlist ready",
            "playlistUrl": f"/recording-hls/{quote(recording_path.stem)}/index.m3u8",
            "name": recording_path.name,
        }

    temp_dir = HLS_CACHE_DIR / f".{recording_path.stem}.tmp"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    segment_pattern = "segment_%05d.ts"
    temp_playlist = temp_dir / "index.m3u8"
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(recording_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-f",
        "hls",
        "-hls_time",
        str(HLS_SEGMENT_SECONDS),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        segment_pattern,
        "index.m3u8",
    ]

    try:
        with subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=str(temp_dir),
        ) as process:
            deadline = time.monotonic() + 900
            while True:
                try:
                    _, stderr = process.communicate(timeout=1)
                    result = subprocess.CompletedProcess(cmd, process.returncode, stderr=stderr)
                    break
                except subprocess.TimeoutExpired:
                    # Bound cache growth during preparation, not just after it finishes.
                    cleanup_old_recordings(protected_paths={recording_path}, protected_cache_dirs={temp_dir})
                    if storage_over_limit or time.monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        raise RuntimeError("Playback preparation exceeded storage capacity or timeout")
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return {"ok": False, "message": f"Could not prepare HLS playback: {exc}"}

    if result.returncode != 0 or not temp_playlist.exists():
        error_text = result.stderr.decode("utf-8", errors="replace").strip()
        shutil.rmtree(temp_dir, ignore_errors=True)
        message = error_text[-500:] if error_text else "ffmpeg could not prepare HLS playback"
        return {"ok": False, "message": message}

    shutil.rmtree(hls_dir, ignore_errors=True)
    temp_dir.rename(hls_dir)
    return {
        "ok": True,
        "message": "HLS playlist ready",
        "playlistUrl": f"/recording-hls/{quote(recording_path.stem)}/index.m3u8",
        "name": recording_path.name,
    }


def stream_file_body(handler, safe_path, start=0, length=None):
    with safe_path.open("rb") as file:
        file.seek(start)
        remaining = length
        while remaining is None or remaining > 0:
            read_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            chunk = file.read(read_size)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            if remaining is not None:
                remaining -= len(chunk)


def serve_hls_file(handler, path):
    safe_path = Path(path).resolve()
    if (
        HLS_CACHE_DIR not in safe_path.parents
        or safe_path.suffix.lower() not in (".m3u8", ".ts")
        or not safe_path.exists()
        or not safe_path.is_file()
    ):
        handler.send_error(HTTPStatus.NOT_FOUND, "HLS file not found")
        return

    content_type = "application/vnd.apple.mpegurl" if safe_path.suffix.lower() == ".m3u8" else "video/mp2t"
    file_size = safe_path.stat().st_size
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(file_size))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    stream_file_body(handler, safe_path)

def serve_recording_file(handler, path):
    safe_path = Path(path).resolve()
    if (
        RECORDINGS_DIR not in safe_path.parents
        or safe_path.suffix.lower() != ".mp4"
        or not safe_path.exists()
        or not safe_path.is_file()
    ):
        handler.send_error(HTTPStatus.NOT_FOUND, "Recording not found")
        return

    file_size = safe_path.stat().st_size
    range_header = handler.headers.get("Range")
    start = 0
    end = file_size - 1
    status = HTTPStatus.OK

    if range_header:
        try:
            units, range_value = range_header.split("=", 1)
            if units.strip().lower() == "bytes":
                start_text, end_text = range_value.split("-", 1)
                if start_text:
                    start = int(start_text)
                    if end_text:
                        end = int(end_text)
                elif end_text:
                    suffix_length = int(end_text)
                    start = max(file_size - suffix_length, 0)
                status = HTTPStatus.PARTIAL_CONTENT
        except (ValueError, TypeError):
            handler.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return

    if file_size == 0 or start < 0 or end >= file_size or start > end:
        handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        handler.send_header("Content-Range", f"bytes */{file_size}")
        handler.end_headers()
        return

    content_length = end - start + 1
    handler.send_response(status)
    handler.send_header("Content-Type", "video/mp4")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(content_length))
    handler.send_header("Cache-Control", "no-store")
    if status == HTTPStatus.PARTIAL_CONTENT:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
    handler.end_headers()

    stream_file_body(handler, safe_path, start, content_length)

class CameraHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        parsed = urlparse(path)
        clean_path = unquote(parsed.path)

        if clean_path.startswith("/recording-hls/"):
            name = clean_path.removeprefix("/recording-hls/")
            safe_parts = [Path(part).name for part in Path(name).parts if part not in ("", ".", "..")]
            return str(HLS_CACHE_DIR.joinpath(*safe_parts))

        if clean_path.startswith("/recordings/"):
            name = clean_path.removeprefix("/recordings/")
            safe_name = Path(name).name
            return str(RECORDINGS_DIR / safe_name)

        if clean_path == "/":
            clean_path = "/index.html"

        return str(ROOT / clean_path.lstrip("/"))

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/esp32/status":
            payload = esp32_monitor.snapshot() if esp32_monitor else {"connected": False, "error": "monitor not started", "data": {}}
            json_response(self, HTTPStatus.OK, payload)
            return
        if parsed.path == "/api/detections/latest":
            payload = person_detection_monitor.snapshot() if person_detection_monitor else {
                "enabled": False,
                "running": False,
                "error": "person detection monitor not started",
                "detections": [],
            }
            json_response(self, HTTPStatus.OK, payload)
            return
        if parsed.path == "/api/recording/status":
            json_response(self, HTTPStatus.OK, recording_status())
            return
        if parsed.path == "/api/recordings":
            json_response(self, HTTPStatus.OK, {"recordings": list_recordings()})
            return
        if parsed.path == "/live.mjpg":
            stream_live_mjpeg(self)
            return
        if parsed.path.startswith("/recording-hls/"):
            serve_hls_file(self, self.translate_path(self.path))
            return
        if parsed.path.startswith("/recordings/"):
            serve_recording_file(self, self.translate_path(self.path))
            return
        if parsed.path == "/api/settings":
            settings = load_settings()
            json_response(self, HTTPStatus.OK, settings)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/recordings/hls":
            payload = read_json_body(self)
            result = ensure_recording_hls(payload.get("name"))
            status = HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST
            json_response(self, status, result)
            return
        if parsed.path == "/api/settings":
            payload = read_json_body(self)
            settings = save_settings(payload)
            cleanup_old_recordings()
            json_response(self, HTTPStatus.OK, {"ok": True, "settings": settings, "status": recording_status()})
            return
        if parsed.path in ("/api/recording/start", "/api/recording/stop"):
            json_response(
                self,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"ok": False, "message": "Recording is automatic and cannot be controlled manually."},
            )
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "message": "Not found"})

    def log_message(self, format, *args):
        print("[%s] %s" % (self.log_date_time_string(), format % args))


def shutdown_handler(signum, frame):
    shutdown_event.set()
    stop_recorder()
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    save_settings(load_settings())
    cleanup_old_recordings()
    print("Recorder waiting for ESP32 occupancy; starts when a person is inside")
    esp32_monitor = ESP32StatusMonitor(ESP32_MQTT_HOST, ESP32_MQTT_PORT, ESP32_MQTT_TOPIC)
    esp32_monitor.start()
    person_detection_monitor = PersonDetectionMonitor(
        AI_DETECTION_SOURCE,
        AI_DETECTION_BACKEND,
        AI_DETECTION_MODEL,
        AI_DETECTION_INPUT_SIZE,
        AI_DETECTION_CONFIDENCE,
        AI_DETECTION_IOU,
        AI_DETECTION_INTERVAL,
        AI_DETECTION_HOLD_SECONDS,
    )
    person_detection_monitor.start()
    maintenance = threading.Thread(target=maintenance_loop, daemon=True)
    maintenance.start()
    recorder_control = threading.Thread(target=recorder_control_loop, daemon=True)
    recorder_control.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), CameraHandler)
    print(f"Camera Monitor running at http://0.0.0.0:{PORT}")
    print(f"Recording source: {RECORDING_SOURCE}")
    print(f"ESP32 MQTT: {ESP32_MQTT_HOST}:{ESP32_MQTT_PORT} topic {ESP32_MQTT_TOPIC}")
    print(f"AI detection: {AI_DETECTION_BACKEND} enabled={AI_DETECTION_ENABLED} source={AI_DETECTION_SOURCE}")
    print(f"Recordings folder: {RECORDINGS_DIR}")
    print("Recording: occupied cell only; 35 GB archive; oldest completed recordings deleted first")
    server.serve_forever()
