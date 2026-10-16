/* Preview connection only: no recording, occupancy or relay controls. */
class RecoveringPreview {
  constructor(video, url, onWaiting) {
    this.video = video;
    this.url = `${url.replace(/\/$/, "")}/whep`;
    this.onWaiting = onWaiting;
    this.active = false;
    this.destroyed = false;
    this.reader = null;
    this.generation = 0;
    this.retryTimer = null;
    this.frameCallback = null;
    this.failures = 0;
    this.requests = new Set();
    this.interval = setInterval(() => this.check(), 1000);
    this.onOnline = () => { if (this.active) this.connect(); };
    window.addEventListener("online", this.onOnline);
  }

  start() {
    if (this.destroyed || this.active) return;
    this.active = true;
    this.connect();
  }

  clearConnection() {
    this.generation++;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.frameCallback !== null && this.video.cancelVideoFrameCallback) {
      this.video.cancelVideoFrameCallback(this.frameCallback);
    }
    this.frameCallback = null;
    for (const request of this.requests) request.abort();
    this.requests.clear();
    if (this.reader) this.reader.close();
    this.reader = null;
    const stream = this.video.srcObject;
    this.video.pause();
    this.video.srcObject = null;
    if (stream && stream.getTracks) stream.getTracks().forEach((track) => track.stop());
  }

  async request(url, options) {
    const controller = new AbortController();
    this.requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      // Include the small SDP/error body in the timeout, not only HTTP headers.
      const body = await response.arrayBuffer();
      return new Response(body.byteLength ? body : null, {
        status: response.status, statusText: response.statusText, headers: response.headers,
      });
    } finally {
      clearTimeout(timeout);
      this.requests.delete(controller);
    }
  }

  connect() {
    if (!this.active || this.destroyed) return;
    this.clearConnection();
    const generation = this.generation;
    this.startedAt = performance.now();
    this.lastFrameAt = this.startedAt;
    this.hasFrames = false;
    this.lastFrameCount = 0;
    this.lastMediaTime = this.video.currentTime;
    this.onWaiting(true);
    try {
      this.reader = new window.MediaMTXWebRTCReader({
        url: this.url,
        fetchRequest: (url, options) => this.request(url, options),
        onError: (error) => {
          if (generation === this.generation) this.retry(String(error));
        },
        onTrack: (event) => {
          if (!this.active || generation !== this.generation) return;
          this.video.srcObject = event.streams[0];
          this.video.play().catch(() => {
            if (generation === this.generation) this.retry("playback did not start");
          });
          if (this.video.requestVideoFrameCallback && this.frameCallback === null) {
            const frame = () => {
              if (!this.active || generation !== this.generation) return;
              this.progress();
              this.frameCallback = this.video.requestVideoFrameCallback(frame);
            };
            this.frameCallback = this.video.requestVideoFrameCallback(frame);
          }
        },
      });
    } catch (error) {
      if (generation === this.generation) this.retry(String(error));
    }
  }

  progress() {
    this.lastFrameAt = performance.now();
    this.hasFrames = true;
    if (this.lastFrameAt - this.startedAt >= 30000) this.failures = 0;
    this.onWaiting(false);
  }

  retry(reason) {
    if (!this.active || this.destroyed || this.retryTimer !== null) return;
    this.clearConnection();
    this.onWaiting(true);
    const delay = Math.min(30000, 2000 * (2 ** Math.min(this.failures, 4)));
    this.failures++;
    console.warn(`Live preview reconnecting in ${delay / 1000}s: ${reason}`);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  check() {
    if (!this.active || this.destroyed || this.retryTimer !== null || document.hidden) return;
    // Decoded frames advance even in a motionless empty cell. Motion is not used.
    if (!this.video.requestVideoFrameCallback && this.video.readyState >= 2) {
      const quality = this.video.getVideoPlaybackQuality?.();
      const count = quality ? quality.totalVideoFrames - quality.droppedVideoFrames : null;
      const advanced = count === null ? this.video.currentTime > this.lastMediaTime : count > this.lastFrameCount;
      if (advanced) this.progress();
      this.lastFrameCount = count;
      this.lastMediaTime = this.video.currentTime;
    }
    const now = performance.now();
    if (!this.hasFrames && now - this.startedAt >= 20000) {
      this.retry("no video frames during startup");
    } else if (this.hasFrames && now - this.lastFrameAt >= 10000) {
      this.retry("video frames stopped");
    }
  }

  stop() {
    this.active = false;
    this.clearConnection();
  }

  destroy() {
    this.stop();
    this.destroyed = true;
    clearInterval(this.interval);
    window.removeEventListener("online", this.onOnline);
  }
}

window.RecoveringPreview = RecoveringPreview;
