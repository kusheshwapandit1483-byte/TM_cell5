const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture({ callbacks = true } = {}) {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  const frames = new Map();
  const readers = [];
  const events = new Map();
  const waiting = [];
  let decoded = 0;
  let stopped = 0;
  let closed = 0;
  class Reader {
    constructor(conf) { this.conf = conf; readers.push(this); }
    close() { this.closed = true; closed++; }
  }
  const video = {
    currentTime: 0, readyState: 4, srcObject: null,
    play: async () => {}, pause: () => {},
    getVideoPlaybackQuality: () => ({ totalVideoFrames: decoded, droppedVideoFrames: 0 }),
  };
  if (callbacks) {
    video.requestVideoFrameCallback = (fn) => { const id = nextId++; frames.set(id, fn); return id; };
    video.cancelVideoFrameCallback = (id) => frames.delete(id);
  }
  const context = {
    AbortController, Response,
    fetch: (url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
    window: { MediaMTXWebRTCReader: Reader,
      addEventListener: (name, fn) => events.set(name, fn),
      removeEventListener: (name) => events.delete(name) },
    document: { hidden: false }, performance: { now: () => now },
    console: { warn: () => {} },
    setInterval: (fn, delay) => { const id = nextId++; timers.set(id, { fn, due: now + delay, interval: delay }); return id; },
    setTimeout: (fn, delay) => { const id = nextId++; timers.set(id, { fn, due: now + delay }); return id; },
    clearInterval: (id) => timers.delete(id), clearTimeout: (id) => timers.delete(id),
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../preview-recovery.js'), 'utf8'), context);
  const player = new context.window.RecoveringPreview(video, 'http://localhost:8889/pramacam', (v) => waiting.push(v));
  function advance(ms) {
    const target = now + ms;
    for (;;) {
      const entry = [...timers.entries()].filter(([, t]) => t.due <= target).sort((a, b) => a[1].due - b[1].due)[0];
      if (!entry) break;
      const [id, timer] = entry;
      now = timer.due;
      if (timer.interval) timer.due += timer.interval;
      else timers.delete(id);
      timer.fn();
    }
    now = target;
  }
  function attach() {
    readers.at(-1).conf.onTrack({ streams: [{ getTracks: () => [{ stop: () => stopped++ }] }] });
  }
  function frame() {
    decoded++;
    video.currentTime += 1;
    for (const [id, fn] of [...frames]) { frames.delete(id); fn(); }
  }
  return { player, readers, events, timers, frames, waiting, video, advance, attach, frame, context,
    get stopped() { return stopped; }, get closed() { return closed; } };
}

{
  const f = fixture();
  f.player.start();
  assert.equal(f.readers[0].conf.url, 'http://localhost:8889/pramacam/whep');
  f.attach(); f.frame();
  assert.equal(f.waiting.at(-1), false);
  f.advance(10000);
  assert.equal(f.waiting.at(-1), true, 'frozen picture immediately shows reconnecting');
  assert.equal(f.closed, 1);
  assert.equal(f.stopped, 1, 'release the old media tracks');
  assert.equal(f.frames.size, 0, 'cancel old frame callbacks');
  f.advance(2000);
  assert.equal(f.readers.length, 2, 'reconnect without refreshing the whole page');
  f.readers[0].conf.onError('obsolete');
  assert.equal(f.readers[1].closed, undefined, 'old errors cannot kill the new connection');
  f.attach(); f.frame();
  assert.equal(f.waiting.at(-1), false, 'a recovered picture clears the overlay');
  f.player.destroy();
  assert.equal(f.timers.size, 0);
  assert.equal(f.events.size, 0);
}

{
  const f = fixture();
  f.player.start();
  f.advance(19000);
  assert.equal(f.readers.length, 1, 'give startup time for negotiation and a keyframe');
  f.advance(3000);
  assert.equal(f.readers.length, 2, 'recover a startup which never delivers frames');
  for (let i = 0; i < 200; i++) {
    f.readers.at(-1).conf.onError('camera offline');
    f.advance(30000);
    assert.ok(f.timers.size <= 2, 'one watchdog and at most one retry timer');
    assert.ok(f.readers.filter((r) => !r.closed).length <= 1, 'only one reader survives retries');
  }
  f.player.stop();
  const attempts = f.readers.length;
  f.advance(3600000);
  assert.equal(f.readers.length, attempts, 'no reconnects while live view is stopped');
  f.player.destroy();
}

{
  const f = fixture();
  f.player.start(); f.attach(); f.frame();
  // Three days of simulated decoded-frame progress in a motionless room.
  for (let second = 0; second < 3 * 86400; second++) { f.frame(); f.advance(1000); }
  assert.equal(f.readers.length, 1, 'healthy long-running video is not periodically interrupted');
  assert.equal(f.timers.size, 1);
  assert.equal(f.frames.size, 1);
  f.player.stop();
  assert.equal(f.frames.size, 0);
  f.player.start();
  assert.equal(f.readers.length, 2, 'returning to Live creates a fresh connection');
  f.events.get('online')();
  assert.equal(f.readers.length, 3, 'network restoration reconnects immediately');
  f.player.destroy();
}

{
  const f = fixture({ callbacks: false });
  f.player.start(); f.attach(); f.frame(); f.advance(1000);
  f.video.currentTime += 30; // Audio/media clock can continue while decoded video freezes.
  f.advance(10000);
  assert.equal(f.waiting.at(-1), true, 'fallback monitors decoded video, not audio progress');
  f.player.destroy();
}

async function requestChecks() {
  const f = fixture();
  const first = f.player.request('/whep', { method: 'OPTIONS' });
  f.advance(8000);
  await assert.rejects(first, /aborted/, 'a hung signaling request times out');
  assert.equal(f.player.requests.size, 0);
  assert.equal(f.timers.size, 1);
  const second = f.player.request('/whep', { method: 'POST' });
  f.player.stop();
  await assert.rejects(second, /aborted/, 'closing the preview aborts pending requests');
  f.context.fetch = async (url, options) => new Response(new ReadableStream({
    start(controller) { options.signal.addEventListener('abort', () => controller.error(new Error('body aborted'))); },
  }));
  const body = f.player.request('/whep', { method: 'POST' });
  await new Promise((resolve) => setImmediate(resolve));
  f.advance(8000);
  await assert.rejects(body, /body aborted/, 'the body cannot hang after headers arrive');
  assert.equal(f.player.requests.size, 0);
  f.player.destroy();
  assert.equal(f.timers.size, 0);
  console.log('Preview recovery checks passed (72 simulated hours, 200 reconnects, request timeouts)');
}
requestChecks().catch((error) => { console.error(error); process.exitCode = 1; });
