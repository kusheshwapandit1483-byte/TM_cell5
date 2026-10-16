const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = (name) => fs.readFileSync(path.join(__dirname, '..', name), 'utf8');

function fixture() {
  let now = 0, serial = 0;
  const timers = new Map(), events = new Map(), frameEvents = new Map();
  const loads = [], log = [], waiting = [];
  const frame = { contentWindow: {},
    addEventListener: (key, fn) => frameEvents.set(key, fn),
    removeEventListener: (key) => frameEvents.delete(key),
    set src(value) { loads.push(value); },
  };
  const context = { URL, Date, performance: { now: () => now }, document: { hidden: false },
    console: { info: (v) => log.push(v), warn: (v) => log.push(v), error: (v) => log.push(v) },
    window: { addEventListener: (key, fn) => events.set(key, fn), removeEventListener: (key) => events.delete(key) },
    setInterval: (fn, delay) => { const id = ++serial; timers.set(id, { fn, due: now + delay, delay }); return id; },
    setTimeout: (fn, delay) => { const id = ++serial; timers.set(id, { fn, due: now + delay }); return id; },
    clearTimeout: (id) => timers.delete(id), clearInterval: (id) => timers.delete(id),
  };
  vm.createContext(context); vm.runInContext(read('native-player-recovery.js'), context);
  const player = new context.window.NativePlayerRecovery(frame, 'http://127.0.0.1:8889/pramacam', (v) => waiting.push(v));
  function advance(ms) {
    const end = now + ms;
    for (;;) {
      const next = [...timers].filter(([, t]) => t.due <= end).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      const [id, timer] = next; now = timer.due;
      if (timer.delay) timer.due += timer.delay; else timers.delete(id);
      timer.fn();
    }
    now = end;
  }
  function progress(frames, extra = {}) {
    player.receive({ source: frame.contentWindow, origin: 'http://127.0.0.1:8889',
      data: { kind: 'tm-native-progress', token: player.token, frames }, ...extra });
  }
  return { player, frame, loads, log, waiting, progress, advance, timers, events, frameEvents, context };
}

// Healthy playback for 72 simulated hours is never periodically reloaded.
{
  const f = fixture(); f.player.start();
  assert.equal(new URL(f.loads[0]).pathname, '/pramacam/');
  f.progress(0);
  for (let i = 1; i <= 72 * 3600; i++) { f.advance(1000); f.progress(i); }
  assert.equal(f.loads.length, 1);
  assert.ok(f.log.includes('TM_NATIVE_OBSERVER_READY'));
  assert.ok(f.log.includes('TM_NATIVE_VIDEO_PROGRESS'));
  f.player.destroy(); assert.equal(f.timers.size, 0); assert.equal(f.events.size, 0);
}
// A frozen picture and a dead child script both cause a frame reload; stale messages cannot revive it.
{
  const f = fixture(); f.player.start(); f.progress(0); f.advance(1000); f.progress(1);
  const old = f.player.token;
  f.advance(10000); assert.equal(f.loads.at(-1), 'about:blank');
  const count = f.loads.length;
  f.progress(2); f.advance(1999); assert.equal(f.loads.length, count);
  f.advance(1); assert.notEqual(f.player.token, old);
  f.progress(100, { data: { kind: 'tm-native-progress', token: old, frames: 100 } });
  assert.equal(f.player.observed, false);
  f.progress(0); f.advance(20000); assert.equal(f.loads.at(-1), 'about:blank');
  f.player.stop(); f.advance(60000); assert.equal(f.loads.at(-1), 'about:blank');
  f.player.destroy(); assert.equal(f.timers.size, 0);
}
// Unrelated windows, origins and invalid counters never qualify as a healthy picture.
{
  const f = fixture(); f.player.start();
  f.progress(10, { source: {} }); f.progress(10, { origin: 'https://elsewhere.invalid' });
  f.progress(NaN); f.progress(-1); f.progress('10'); assert.equal(f.player.observed, false);
  f.advance(120000); assert.equal(f.loads.length, 1); // Missing extension must preserve working playback.
  assert.ok(f.log.some((v) => v.includes('automatic stall recovery is disabled')));
  f.player.destroy();
}
// Repeated failures back off, cancel cleanly, and never accumulate timers.
{
  const f = fixture(); f.player.start();
  for (let i = 0; i < 100; i++) {
    f.progress(0); f.player.retry('test disconnect'); f.player.retry('duplicate');
    assert.equal(f.timers.size, 2);
    f.advance(30000); assert.equal(f.timers.size, 1);
  }
  f.player.stop(); const count = f.loads.length; f.advance(60000); assert.equal(f.loads.length, count);
  f.player.start(); f.progress(0); f.context.document.hidden = true;
  f.advance(60000); assert.notEqual(f.loads.at(-1), 'about:blank');
  f.player.destroy(); assert.equal(f.timers.size, 0);
}

function observerFixture(callbacks = true) {
  const messages = [], intervals = new Map(), events = new Map(), callbacksById = new Map();
  let serial = 0, count = 0;
  const video = { readyState: 4, currentTime: 0,
    getVideoPlaybackQuality: () => ({ totalVideoFrames: count, droppedVideoFrames: 0 }) };
  if (callbacks) {
    video.requestVideoFrameCallback = (fn) => { const id = ++serial; callbacksById.set(id, fn); return id; };
    video.cancelVideoFrameCallback = (id) => callbacksById.delete(id);
  }
  const context = { URLSearchParams,
    location: { port: '8889', pathname: '/pramacam/', hostname: '127.0.0.1', search: '?tm_native_watch=test' },
    document: { querySelector: () => video },
    window: { parent: { postMessage: (data, origin) => messages.push({ data, origin }) },
      addEventListener: (key, fn) => events.set(key, fn) },
    setInterval: (fn) => { const id = ++serial; intervals.set(id, fn); return id; },
    clearInterval: (id) => intervals.delete(id),
  };
  vm.createContext(context); vm.runInContext(read('native-player-observer/observer.js'), context);
  return { messages, intervals, events, callbacksById, video,
    report: () => [...intervals.values()].forEach((fn) => fn()),
    decode: () => {
      count++;
      for (const [id, fn] of [...callbacksById]) { callbacksById.delete(id); fn(); }
    } };
}
// The isolated observer reads frames but does not touch srcObject, play(), tracks, or peers.
for (const callbacks of [true, false]) {
  const f = observerFixture(callbacks);
  assert.equal(f.messages[0].data.frames, 0);
  f.decode(); f.report(); const frames = f.messages.at(-1).data.frames;
  assert.equal(frames, 1); assert.equal(f.messages.at(-1).origin, 'http://127.0.0.1:8080');
  f.report(); assert.equal(f.messages.at(-1).data.frames, frames); // Frozen picture is not progress.
  f.events.get('pagehide')(); assert.equal(f.callbacksById.size, 0); assert.equal(f.intervals.size, 0);
}
// Dashboard navigation really stops the iframe and resumes the same player on returning to Live.
{
  const elements = new Map(), native = [];
  function element(key) {
    if (!elements.has(key)) elements.set(key, { hidden: false, dataset: {},
      classList: { remove() {}, add() {}, toggle() {} },
      addEventListener() {}, removeAttribute() {}, pause() {}, load() {} });
    return elements.get(key);
  }
  const context = { console, URL, URLSearchParams,
    document: { hidden: false, querySelector: element, querySelectorAll: () => [], addEventListener() {} },
    window: { location: { hostname: '127.0.0.1', search: '' }, addEventListener() {},
      NativePlayerRecovery: class {
        constructor(frame, url) { this.url = url; this.starts = 0; this.stops = 0; native.push(this); }
        start() { this.starts++; }
        stop() { this.stops++; }
      } },
    fetch: () => new Promise(() => {}), setInterval() {},
  };
  vm.createContext(context); vm.runInContext(read('app.js'), context);
  assert.equal(native.length, 1); assert.equal(native[0].starts, 1);
  vm.runInContext('showView("recordingsView")', context); assert.equal(native[0].stops, 1);
  vm.runInContext('showView("liveView")', context); assert.equal(native[0].starts, 2);
  assert.equal(native.length, 1); assert.equal(native[0].url, 'http://127.0.0.1:8889/pramacam');
}
console.log('Native player tests passed: 72 simulated healthy hours, frozen frames, 100 retries, original-player observation, dashboard navigation and cleanup');
