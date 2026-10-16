const video = document.querySelector("#cameraVideo");
const frame = document.querySelector("#cameraFrame");
const emptyState = document.querySelector("#emptyState");
const navButtons = document.querySelectorAll("[data-view-target]");
const viewPanels = document.querySelectorAll(".view-panel");
const refreshRecordings = document.querySelector("#refreshRecordings");
const recordingsList = document.querySelector("#recordingsList");
const recordingPlayer = document.querySelector("#recordingPlayer");
const recordingEmptyState = document.querySelector("#recordingEmptyState");
const personCount = document.querySelector("#personCount");
const entryCount = document.querySelector("#entryCount");
const exitCount = document.querySelector("#exitCount");
const conditionTile = document.querySelector("#conditionTile");
const conditionStatus = document.querySelector("#conditionStatus");

const previewHost = window.location.hostname || "127.0.0.1";
const fixedPreviewUrl = `http://${previewHost}:8889/pramacam`;
const directVideoExtensions = [".mp4", ".webm", ".ogg", ".mov"];
let hlsPlayer = null;
let livePreview = null;
let recordingHlsPlayer = null;
let activeView = "liveView";
let recordingFallbackClip = null;
let recordingFallbackTried = false;
let recordingFallbackInProgress = false;

function formatCount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(number) : "--";
}


function getCondition(data) {
  const state = String(data.state || "").toUpperCase();
  const occupancy = Number(data.occupancy || 0);
  const relayClear = data.relay === true;

  if (state === "FAULT") {
    return { label: "Fault", className: "condition-fault" };
  }
  if (state === "STARTUP") {
    return { label: "Restart", className: "condition-waiting" };
  }
  if (state === "AWAIT_START") {
    return { label: "Await Start", className: "condition-waiting" };
  }
  if (state === "RUN" && occupancy > 0) {
    return { label: "Occupied", className: "condition-occupied" };
  }
  if (state === "RUN" && occupancy === 0 && !relayClear) {
    return { label: "Await Clear", className: "condition-clearing" };
  }
  if (state === "RUN" && occupancy === 0 && relayClear) {
    return { label: "Clear", className: "condition-clear" };
  }
  return { label: state || "Waiting", className: "condition-waiting" };
}

function updateCondition(data) {
  const condition = getCondition(data);
  conditionStatus.textContent = condition.label;
  conditionTile.classList.remove("condition-waiting", "condition-clear", "condition-clearing", "condition-occupied", "condition-fault");
  conditionTile.classList.add(condition.className);
}
async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }

  return response.json();
}

async function refreshEsp32Status() {
  try {
    const status = await apiRequest("/api/esp32/status");
    const data = status.data || {};
    if (typeof data.occupancy !== "undefined") {
      personCount.textContent = formatCount(data.occupancy);
    }
    if (typeof data.total_in !== "undefined") {
      entryCount.textContent = formatCount(data.total_in);
    }
    if (typeof data.total_out !== "undefined") {
      exitCount.textContent = formatCount(data.total_out);
    }
    if (data.state) {
      updateCondition(data);
    }
  } catch (error) {
    // Keep the last valid count visible during short ESP32/network delays.
  }
}
function cleanUrl(url) {
  return url.split("?")[0].toLowerCase();
}

function isHlsUrl(url) {
  return cleanUrl(url).endsWith(".m3u8");
}

function isDirectVideoUrl(url) {
  return directVideoExtensions.some((extension) => cleanUrl(url).endsWith(extension));
}

function showEmptyState(show) {
  emptyState.classList.toggle("is-hidden", !show);
}

function destroyHls() {
  if (hlsPlayer) {
    hlsPlayer.destroy();
    hlsPlayer = null;
  }
}

function loadHls(url) {
  destroyHls();
  frame.hidden = true;
  video.hidden = false;

  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    video.load();
    video.play().catch(() => undefined);
    return;
  }

  if (window.Hls && window.Hls.isSupported()) {
    hlsPlayer = new window.Hls({ lowLatencyMode: true, backBufferLength: 30 });
    hlsPlayer.loadSource(url);
    hlsPlayer.attachMedia(video);
    hlsPlayer.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => undefined));
    hlsPlayer.on(window.Hls.Events.ERROR, () => showEmptyState(true));
  }
}

function loadDirectVideo(url) {
  destroyHls();
  frame.hidden = true;
  video.hidden = false;
  video.src = url;
  video.load();
  video.play().catch(() => undefined);
}

function loadPreviewPage(url) {
  destroyHls();
  video.pause();
  video.removeAttribute("src");
  video.load();
  frame.hidden = true;
  frame.removeAttribute("src");
  video.hidden = false;
  if (!livePreview) livePreview = new window.RecoveringPreview(video, url, showEmptyState);
  livePreview.start();
}

function loadFixedPreview() {
  showEmptyState(false);
  if (isHlsUrl(fixedPreviewUrl)) {
    loadHls(fixedPreviewUrl);
    return;
  }
  if (isDirectVideoUrl(fixedPreviewUrl)) {
    loadDirectVideo(fixedPreviewUrl);
    return;
  }
  loadPreviewPage(fixedPreviewUrl);
}

function formatBytes(bytes) {
  if (!bytes) {
    return "0 MB";
  }
  const mb = bytes / (1024 * 1024);
  if (mb < 1024) {
    return `${mb.toFixed(1)} MB`;
  }
  return `${(mb / 1024).toFixed(2)} GB`;
}

function formatDate(value) {
  if (!value) {
    return "Unknown time";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString([], {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderEmptyRecordings(message) {
  recordingsList.textContent = "";
  const empty = document.createElement("div");
  empty.className = "empty-list";
  empty.textContent = message;
  recordingsList.appendChild(empty);
}

function showRecordingMessage(title, message) {
  const titleNode = recordingEmptyState.querySelector("strong");
  const messageNode = recordingEmptyState.querySelector("span");
  titleNode.textContent = title;
  messageNode.textContent = message;
  recordingEmptyState.classList.remove("is-hidden");
}

function destroyRecordingHls() {
  if (recordingHlsPlayer) {
    recordingHlsPlayer.destroy();
    recordingHlsPlayer = null;
  }
}

function playRecordingSource(url) {
  destroyRecordingHls();
  recordingPlayer.pause();
  recordingPlayer.removeAttribute("src");
  recordingPlayer.load();

  if (isHlsUrl(url) && recordingPlayer.canPlayType("application/vnd.apple.mpegurl")) {
    recordingPlayer.src = url;
    recordingPlayer.load();
    recordingEmptyState.classList.add("is-hidden");
    recordingPlayer.play().catch(() => undefined);
    return;
  }

  if (isHlsUrl(url) && window.Hls && window.Hls.isSupported()) {
    recordingHlsPlayer = new window.Hls({ backBufferLength: 60 });
    recordingHlsPlayer.loadSource(url);
    recordingHlsPlayer.attachMedia(recordingPlayer);
    recordingHlsPlayer.on(window.Hls.Events.MANIFEST_PARSED, () => {
      recordingEmptyState.classList.add("is-hidden");
      recordingPlayer.play().catch(() => undefined);
    });
    recordingHlsPlayer.on(window.Hls.Events.ERROR, () => {
      showRecordingMessage("Playback unavailable", "Could not play this prepared recording. Try refresh, then select it again.");
    });
    return;
  }

  recordingPlayer.src = url;
  recordingPlayer.load();
  recordingEmptyState.classList.add("is-hidden");
  recordingPlayer.play().catch(() => undefined);
}
async function prepareRecordingHlsFallback() {
  if (!recordingFallbackClip || recordingFallbackInProgress) {
    return;
  }
  if (recordingFallbackTried) {
    showRecordingMessage("Playback unavailable", "This recording could not be played in the browser.");
    return;
  }

  recordingFallbackTried = true;
  recordingFallbackInProgress = true;
  showRecordingMessage("Preparing recording", "Large video needs browser playback preparation. Please wait.");

  try {
    const result = await apiRequest("/api/recordings/hls", {
      method: "POST",
      body: JSON.stringify({ name: recordingFallbackClip.name }),
    });
    recordingFallbackInProgress = false;
    playRecordingSource(result.playlistUrl);
  } catch (error) {
    recordingFallbackInProgress = false;
    showRecordingMessage("Playback unavailable", "This recording could not be prepared for browser playback. Try refresh, then select it again.");
  }
}

async function playRecordingClip(clip) {
  recordingFallbackClip = clip;
  recordingFallbackTried = false;
  recordingFallbackInProgress = false;
  showRecordingMessage("Loading recording", "Opening the recorded video.");
  playRecordingSource(clip.url);
}
async function refreshRecordingsList() {
  try {
    const data = await apiRequest("/api/recordings");
    const recordings = data.recordings || [];

    if (!recordings.length) {
      renderEmptyRecordings("No completed recordings yet. Clips are saved while someone is inside the cell and appear after the cell clears or a segment finishes.");
      return;
    }

    recordingsList.innerHTML = "";
    recordings.forEach((clip) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "clip-button";

      const clipName = document.createElement("span");
      clipName.className = "clip-name";
      clipName.textContent = clip.name;

      const clipMeta = document.createElement("span");
      clipMeta.className = "clip-meta";
      clipMeta.textContent = `${formatDate(clip.modifiedAt)} - ${formatBytes(clip.sizeBytes)}`;

      button.append(clipName, clipMeta);
      button.addEventListener("click", () => {
        document.querySelectorAll(".clip-button").forEach((item) => item.classList.remove("is-selected"));
        button.classList.add("is-selected");
        playRecordingClip(clip);
      });
      recordingsList.appendChild(button);
    });
  } catch (error) {
    renderEmptyRecordings("Could not load recordings. Open the app through python server.py, not directly as a file.");
  }
}

function showView(targetId) {
  activeView = targetId;
  navButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.viewTarget === targetId);
  });
  viewPanels.forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === targetId);
  });

  if (targetId === "recordingsView") {
    if (livePreview) livePreview.stop();
    refreshRecordingsList();
  } else if (targetId === "liveView" && !document.hidden) {
    loadFixedPreview();
  }
}

navButtons.forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.viewTarget));
});

refreshRecordings.addEventListener("click", refreshRecordingsList);
recordingPlayer.addEventListener("error", prepareRecordingHlsFallback);

frame.addEventListener("load", () => showEmptyState(false));
frame.addEventListener("error", () => showEmptyState(true));
video.addEventListener("playing", () => showEmptyState(false));
video.addEventListener("error", () => showEmptyState(true));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (livePreview) livePreview.stop();
  } else if (activeView === "liveView") {
    loadFixedPreview();
  }
});
window.addEventListener("pagehide", () => {
  if (livePreview) livePreview.stop();
});
window.addEventListener("pageshow", () => {
  if (activeView === "liveView" && !document.hidden) loadFixedPreview();
});

recordingEmptyState.classList.remove("is-hidden");
loadFixedPreview();
refreshEsp32Status();
setInterval(refreshEsp32Status, 1000);
setInterval(() => {
  if (activeView === "recordingsView") {
    refreshRecordingsList();
  }
}, 15000);
