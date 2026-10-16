/* Isolated Chromium content script. Does not create or modify a media connection. */
(() => {
  if (location.port !== "8889" || !/^\/pramacam\/?$/.test(location.pathname)
      || window.parent === window) return;
  const params = new URLSearchParams(location.search);
  const token = params.get("tm_native_watch");
  if (!token) return;
  let current = null, callback = null, frames = 0, previous = null, previousTime = null;
  const origin = `http://${location.hostname}:8080`;
  const sent = new Set();
  function verify(stage) {
    const port = params.get("tm_verify_port"), nonce = params.get("tm_verify_nonce");
    if (!/^\d{1,5}$/.test(port || "") || Number(port) < 1 || Number(port) > 65535
        || !/^[A-Za-z0-9_-]{20,64}$/.test(nonce || "") || sent.has(stage)) return;
    sent.add(stage);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    fetch(`http://127.0.0.1:${port}/verify/${nonce}/${stage}`, {
      mode: "no-cors", cache: "no-store", signal: controller.signal,
    }).catch(() => {}).finally(() => clearTimeout(timer));
  }
  verify("observer-ready");
  function release() {
    if (callback !== null && current?.cancelVideoFrameCallback) current.cancelVideoFrameCallback(callback);
    callback = null;
  }
  function follow(video) {
    release(); current = video; previous = null; previousTime = null;
    if (!video?.requestVideoFrameCallback) return;
    function frame() {
      if (current !== video) return;
      frames++;
      callback = video.requestVideoFrameCallback(frame);
    }
    callback = video.requestVideoFrameCallback(frame);
  }
  function report() {
    const video = document.querySelector("video");
    if (video !== current) follow(video);
    if (video && !video.requestVideoFrameCallback && video.readyState >= 2) {
      const quality = video.getVideoPlaybackQuality?.();
      const count = quality ? quality.totalVideoFrames - quality.droppedVideoFrames : null;
      if (count !== null ? previous !== null && count > previous
        : previousTime !== null && video.currentTime > previousTime) frames++;
      previous = count; previousTime = video.currentTime;
    }
    window.parent.postMessage({ kind: "tm-native-progress", token, frames }, origin);
    if (frames > 0) verify("observer-frames");
  }
  const timer = setInterval(report, 1000);
  report();
  window.addEventListener("pagehide", () => { clearInterval(timer); release(); current = null; }, { once: true });
})();
