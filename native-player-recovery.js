/* Supervise the existing MediaMTX iframe. The player and its signaling remain on port 8889. */
class NativePlayerRecovery {
  constructor(frame, url, onWaiting) {
    this.frame = frame; this.url = new URL(url); this.onWaiting = onWaiting;
    this.url.pathname = this.url.pathname.replace(/\/?$/, "/");
    this.active = false; this.destroyed = false; this.failures = 0;
    this.retryTimer = null; this.serial = 0;
    this.message = (event) => this.receive(event);
    this.online = () => { if (this.active) this.load(); };
    this.loaded = () => { if (this.active && !this.observed) this.onWaiting(false); };
    this.error = () => this.retry("player page failed to load");
    window.addEventListener("message", this.message);
    window.addEventListener("online", this.online);
    frame.addEventListener("load", this.loaded);
    frame.addEventListener("error", this.error);
    this.timer = setInterval(() => this.check(), 1000);
  }
  start() {
    if (this.active || this.destroyed) return;
    this.active = true; this.load();
  }
  load() {
    if (!this.active || this.destroyed) return;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.started = performance.now(); this.lastFrame = this.started;
    this.frames = 0; this.observed = false; this.hasFrames = false; this.warned = false;
    this.token = `${Date.now()}-${++this.serial}`;
    const url = new URL(this.url);
    url.searchParams.set("tm_native_watch", this.token);
    this.onWaiting(true); this.frame.src = url.href;
  }
  receive(event) {
    if (!this.active || this.retryTimer !== null || event.source !== this.frame.contentWindow
        || event.origin !== this.url.origin) return;
    const data = event.data;
    if (!data || data.kind !== "tm-native-progress" || data.token !== this.token
        || !Number.isSafeInteger(data.frames) || data.frames < 0) return;
    if (!this.observed) console.info("TM_NATIVE_OBSERVER_READY");
    this.observed = true;
    if (data.frames > this.frames) {
      if (!this.hasFrames) console.info("TM_NATIVE_VIDEO_PROGRESS");
      this.lastFrame = performance.now(); this.hasFrames = true;
      this.onWaiting(false);
      if (this.lastFrame - this.started >= 30000) this.failures = 0;
    }
    this.frames = data.frames;
  }
  check() {
    if (!this.active || this.destroyed || this.retryTimer !== null || document.hidden) return;
    const now = performance.now();
    if (!this.observed) {
      // An unavailable extension must not break the confirmed working player.
      if (!this.warned && now - this.started >= 30000) {
        this.warned = true;
        console.error("Native frame observer unavailable: automatic stall recovery is disabled; original player retained");
      }
      return;
    }
    if ((!this.hasFrames && now - this.started >= 20000)
        || (this.hasFrames && now - this.lastFrame >= 10000)) this.retry("native video frames stopped");
  }
  retry(reason) {
    if (!this.active || this.destroyed || this.retryTimer !== null) return;
    const delay = Math.min(30000, 2000 * 2 ** Math.min(this.failures++, 4));
    console.warn(`Reloading native MediaMTX player in ${delay / 1000}s: ${reason}`);
    this.onWaiting(true);
    this.token = null; this.frame.src = "about:blank";
    this.retryTimer = setTimeout(() => { this.retryTimer = null; this.load(); }, delay);
  }
  stop() {
    this.active = false; this.token = null;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null; this.frame.src = "about:blank";
  }
  destroy() {
    this.stop(); this.destroyed = true; clearInterval(this.timer);
    window.removeEventListener("message", this.message);
    window.removeEventListener("online", this.online);
    this.frame.removeEventListener("load", this.loaded);
    this.frame.removeEventListener("error", this.error);
  }
}
window.NativePlayerRecovery = NativePlayerRecovery;
