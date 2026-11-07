// Socket-free logic tests. Run: node experiments/j71b-capture/test_browser_test.cjs
// These mocks do not establish Firefox/file-origin or physical microphone behavior.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'browser_test.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1);
const script = scripts[0][1];
const expected = 'J71b Microphone (conditioned)';
const tests = [];
const test = (name, run) => tests.push({ name, run });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const settle = () => new Promise(resolve => setImmediate(resolve));

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(callback);
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
    emit(type) { for (const callback of listeners.get(type) || []) callback(); }
  };
}

function makeStream(label = expected, settings = {}) {
  const track = Object.assign(eventTarget(), {
    label, readyState: 'live', stops: 0,
    getSettings: () => settings,
    stop() { this.stops++; this.readyState = 'ended'; },
    end() { this.readyState = 'ended'; this.emit('ended'); }
  });
  return { track, getAudioTracks: () => [track], getTracks: () => [track] };
}

function audioBuffer(channels = [[0, 0.25, -0.25]], rate = 48000) {
  return {
    length: channels[0].length, numberOfChannels: channels.length, sampleRate: rate,
    getChannelData: index => Float32Array.from(channels[index])
  };
}

function harness(options = {}) {
  const ids = ['record', 'cancel', 'status', 'details', 'wav', 'original'];
  const elements = Object.fromEntries(ids.map(id => [id, Object.assign(eventTarget(), {
    textContent: '', hidden: id === 'wav' || id === 'original', disabled: id === 'cancel',
    hasAttribute(name) { return this[name] !== undefined; },
    removeAttribute(name) { delete this[name]; },
    click() { if (!this.disabled) this.emit('click'); }
  })]));
  const window = eventTarget();
  const document = Object.assign(eventTarget(), {
    visibilityState: 'visible', getElementById: id => elements[id]
  });
  const timers = new Map();
  const urls = new Map();
  const revoked = [];
  const recorders = [];
  const requests = [];
  const decoders = [];
  const stream = options.stream || makeStream();
  let now = 0, nextTimer = 1, nextUrl = 1;
  class Recorder {
    static isTypeSupported(type) { return type === (options.mime || 'audio/webm;codecs=opus'); }
    constructor(input, config) {
      if (options.constructorError) throw new Error('constructor failed');
      this.stream = input;
      this.mimeType = config?.mimeType || 'audio/webm';
      this.state = 'inactive';
      this.starts = 0;
      recorders.push(this);
    }
    start() {
      if (options.startError) throw new Error('start failed');
      this.state = 'recording';
      this.starts++;
    }
    stop() {
      if (options.stopError) throw new Error('stop failed');
      this.state = 'inactive';
      // Keep queued callbacks to exercise stale-event guards after cancellation.
      const data = this.ondataavailable, stopped = this.onstop;
      queueMicrotask(() => {
        data?.({ data: new Blob(options.empty ? [] : ['encoded audio'], { type: this.mimeType }) });
        stopped?.();
      });
    }
  }
  class OfflineDecoder {
    constructor(channels, frames, rate) { decoders.push({ channels, frames, rate }); }
    decodeAudioData(bytes) {
      assert.ok(bytes.byteLength > 0);
      if (options.decodeError) return Promise.reject(new Error('decoder failed'));
      return options.decode ? options.decode.promise : Promise.resolve(audioBuffer());
    }
  }
  const context = vm.createContext({
    document, window,
    location: { protocol: options.protocol || 'file:' },
    navigator: { mediaDevices: options.noMediaDevices ? undefined : {
      getUserMedia: constraints => {
        requests.push(constraints);
        return options.permission ? options.permission.promise : Promise.resolve(stream);
      }
    } },
    MediaRecorder: options.noRecorder ? undefined : Recorder,
    OfflineAudioContext: options.noDecoder ? undefined : OfflineDecoder,
    Blob, performance: { now: () => now },
    setTimeout: (callback, delay) => {
      const id = nextTimer++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout: id => timers.delete(id),
    URL: {
      createObjectURL: blob => { const url = `blob:local-${nextUrl++}`; urls.set(url, blob); return url; },
      revokeObjectURL: url => { revoked.push(url); urls.delete(url); }
    }
  });
  vm.runInContext(script, context, { filename: 'browser_test.html' });
  return {
    context, elements, document, window, timers, urls, revoked, recorders, requests, decoders, stream,
    start: async () => { elements.record.click(); await settle(); },
    cancel: async () => { elements.cancel.click(); await settle(); },
    expire: async () => {
      assert.equal(timers.size, 1);
      const [id, timer] = [...timers][0];
      assert.equal(timer.delay, 10000);
      timers.delete(id); now = 10000; timer.callback(); await settle();
    }
  };
}

function ended(h) {
  assert.match(h.elements.status.textContent, /^Ended:/);
  assert.equal(h.elements.cancel.disabled, true);
  assert.equal(h.timers.size, 0);
  assert.equal(h.stream.track.readyState, 'ended');
}

function noDownloads(h) {
  assert.equal(h.urls.size, 0);
  for (const id of ['wav', 'original']) {
    assert.equal(h.elements[id].hidden, true);
    assert.equal(h.elements[id].hasAttribute('href'), false);
  }
}

test('local page has restrictive CSP, no networking/imports or playback path', () => {
  assert.match(html, /default-src 'none'/);
  assert.match(html, /connect-src 'none'/);
  assert.match(html, /base-uri 'none'/);
  assert.match(html, /form-action 'none'/);
  assert.doesNotMatch(html.replace(/<script>[\s\S]*?<\/script>/g, ''), /\b(?:src|href)\s*=/i);
  assert.doesNotMatch(script, /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|AudioContext|Audio|Worker|importScripts|localStorage|indexedDB)\b|sendBeacon|\.play\(|\.click\(/);
  assert.doesNotMatch(html, /<(?:audio|video|iframe|form)\b/i);
  const h = harness();
  assert.equal(h.requests.length, 0);
  assert.equal(h.recorders.length, 0);
});

test('exact source records once, stops at 10 seconds, and offers explicit local downloads', async () => {
  const settings = { sampleRate: 44100, channelCount: 2, echoCancellation: false, autoGainControl: false, noiseSuppression: true };
  const h = harness({ stream: makeStream(expected, settings) });
  await h.start();
  h.elements.record.emit('click'); // Even a synthetic repeated click cannot create a second request.
  assert.equal(h.requests.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.requests[0])), {
    audio: { echoCancellation: false, autoGainControl: false, noiseSuppression: false }, video: false
  });
  assert.equal(h.recorders[0].starts, 1);
  assert.match(h.elements.details.textContent, /Source label: J71b Microphone \(conditioned\)/);
  assert.match(h.elements.details.textContent, /noiseSuppression: true/);
  assert.match(h.elements.details.textContent, /sampleSize: not reported/);
  await h.expire();
  ended(h);
  assert.equal(h.recorders[0].state, 'inactive');
  assert.equal(h.decoders[0].rate, 44100);
  assert.match(h.elements.status.textContent, /downloads are ready/);
  assert.match(h.elements.details.textContent, /Capture wall time: 10.00 s/);
  assert.match(h.elements.details.textContent, /WAV: \d+ bytes/);
  assert.equal(h.urls.size, 2);
  assert.match(h.elements.wav.download, /^j71b-conditioned-[\dT-]+Z\.wav$/);
  assert.match(h.elements.original.download, /\.webm$/);
  assert.equal(h.elements.wav.hidden, false);
  assert.equal(h.elements.original.hidden, false);
  h.window.emit('pagehide');
  noDownloads(h);
  assert.equal(h.revoked.length, 2);
});

test('wrong or ambiguous source is stopped before MediaRecorder is constructed', async () => {
  for (const label of ['Default', 'J71b Microphone', `Default - ${expected}`, '', expected]) {
    const stream = makeStream(label);
    if (label === expected) stream.getAudioTracks = () => [stream.track, stream.track];
    const h = harness({ stream });
    await h.start();
    ended(h);
    assert.match(h.elements.status.textContent, /Source refused/);
    assert.equal(h.recorders.length, 0);
    noDownloads(h);
  }
});

test('missing recorder, missing capture API or nonlocal origin never asks for microphone', async () => {
  for (const option of [{ noRecorder: true }, { noMediaDevices: true }, { protocol: 'https:' }]) {
    const h = harness(option);
    await h.start();
    assert.equal(h.requests.length, 0);
    assert.match(h.elements.status.textContent, /^Ended: Could not record/);
    assert.equal(h.elements.record.disabled, false);
    noDownloads(h);
  }
});

test('cancelling pending permission immediately invalidates a late stream', async () => {
  const permission = deferred();
  const h = harness({ permission });
  await h.start();
  await h.cancel();
  assert.match(h.elements.status.textContent, /late permission result will be stopped/);
  permission.resolve(h.stream);
  await settle();
  ended(h);
  assert.equal(h.recorders.length, 0);
  noDownloads(h);
});

test('late cancelled permission cannot overwrite a newer explicit attempt', async () => {
  const permission = deferred();
  const h = harness({ permission });
  await h.start(); await h.cancel();
  const current = makeStream();
  h.context.navigator.mediaDevices.getUserMedia = async () => current;
  await h.start();
  permission.resolve(h.stream); await settle();
  assert.equal(h.stream.track.readyState, 'ended');
  assert.equal(current.track.readyState, 'live');
  assert.equal(h.recorders.length, 1);
  assert.match(h.elements.status.textContent, /^Recording/);
  await h.cancel();
  assert.equal(current.track.readyState, 'ended');
});

test('active cancellation discards queued final data and removes the timer', async () => {
  const h = harness();
  await h.start(); await h.cancel();
  ended(h);
  assert.equal(h.recorders[0].state, 'inactive');
  assert.equal(h.decoders.length, 0);
  noDownloads(h);
});

test('permission rejection leaves a retryable ended state without automatic retry', async () => {
  const permission = deferred();
  const h = harness({ permission });
  await h.start();
  permission.reject(new Error('permission denied')); await settle();
  assert.match(h.elements.status.textContent, /permission denied/);
  assert.equal(h.elements.record.disabled, false);
  assert.equal(h.requests.length, 1);
  assert.equal(h.recorders.length, 0);
  noDownloads(h);
});

test('constructor, start, stop, empty-data and ended-track failures release capture', async () => {
  for (const option of [{ constructorError: true }, { startError: true }, { stopError: true }, { empty: true }, { endedTrack: true }]) {
    const h = harness(option);
    if (option.endedTrack) h.stream.track.readyState = 'ended';
    await h.start();
    if (option.stopError || option.empty) await h.expire();
    ended(h);
    noDownloads(h);
  }
});

test('unexpected track end, recorder error and unsolicited recorder stop discard and clean up', async () => {
  for (const event of ['track', 'error', 'stop']) {
    const h = harness();
    await h.start();
    if (event === 'track') h.stream.track.end();
    if (event === 'error') h.recorders[0].onerror({ error: { name: 'UnknownError' } });
    if (event === 'stop') h.recorders[0].stop();
    await settle();
    ended(h);
    noDownloads(h);
  }
});

test('pagehide stops active capture and late permission; pageshow does not restart', async () => {
  for (const pending of [true, false]) {
    const permission = pending ? deferred() : undefined;
    const h = harness({ permission });
    await h.start();
    h.window.emit('pagehide');
    if (pending) permission.resolve(h.stream);
    await settle();
    ended(h);
    assert.equal(h.elements.record.disabled, true);
    assert.equal(h.requests.length, 1);
    noDownloads(h);
    h.window.emit('pageshow');
    assert.equal(h.elements.record.disabled, false);
    assert.equal(h.requests.length, 1);
  }
});

test('missing/failed offline decoding retains original Ogg without recapture', async () => {
  for (const option of [{ noDecoder: true }, { decodeError: true }]) {
    const h = harness({ ...option, mime: 'audio/ogg;codecs=opus' });
    await h.start(); await h.expire();
    ended(h);
    assert.match(h.elements.status.textContent, /WAV unavailable/);
    assert.match(h.elements.original.download, /\.ogg$/);
    assert.equal(h.elements.wav.hidden, true);
    assert.equal(h.urls.size, 1);
    assert.equal(h.requests.length, 1);
  }
});

test('hidden tab discards pending permission, recording or decoding and never auto resumes', async () => {
  for (const phase of ['permission', 'recording', 'decoding']) {
    const permission = phase === 'permission' ? deferred() : undefined;
    const decode = phase === 'decoding' ? deferred() : undefined;
    const h = harness({ permission, decode });
    await h.start();
    if (decode) await h.expire();
    h.document.visibilityState = 'hidden';
    h.document.emit('visibilitychange');
    if (permission) permission.resolve(h.stream);
    if (decode) decode.resolve(audioBuffer());
    await settle();
    ended(h);
    noDownloads(h);
    assert.match(h.elements.status.textContent, /tab hidden/);
    await h.start(); // Even synthetic clicks cannot acquire a hidden tab's microphone.
    assert.equal(h.requests.length, 1);
    h.document.visibilityState = 'visible';
    h.document.emit('visibilitychange');
    assert.equal(h.requests.length, 1);
  }
});

test('cancel/pagehide during decoding revokes original and ignores late WAV', async () => {
  for (const action of ['cancel', 'pagehide']) {
    const decode = deferred();
    const h = harness({ decode });
    await h.start(); await h.expire();
    assert.equal(h.stream.track.readyState, 'ended');
    assert.equal(h.urls.size, 1);
    if (action === 'cancel') await h.cancel();
    else h.window.emit('pagehide');
    decode.resolve(audioBuffer()); await settle();
    ended(h);
    noDownloads(h);
    assert.equal(h.revoked.length, 1);
  }
});

test('an explicit new take revokes old download URLs, without auto recording', async () => {
  const h = harness();
  await h.start(); await h.expire();
  assert.equal(h.requests.length, 1);
  assert.equal(h.urls.size, 2);
  const next = makeStream();
  h.context.navigator.mediaDevices.getUserMedia = async () => next;
  await h.start();
  noDownloads(h);
  assert.equal(h.revoked.length, 2);
  assert.equal(h.recorders.length, 2);
  await h.cancel();
  assert.equal(next.track.readyState, 'ended');
});

test('PCM16 WAV has valid little-endian headers, interleaving and no normalization', () => {
  const h = harness();
  const buffer = audioBuffer([[-1, -0.5, 0, 0.5, 1, 2, NaN], [1, 0.5, 0, -0.5, -1, -2, Infinity]], 44100);
  const wav = h.context.encodeWav(buffer);
  const view = new DataView(wav);
  const text = (start, end) => Buffer.from(wav).subarray(start, end).toString('ascii');
  assert.equal(text(0, 4), 'RIFF'); assert.equal(text(8, 12), 'WAVE');
  assert.equal(text(12, 16), 'fmt '); assert.equal(text(36, 40), 'data');
  assert.equal(view.getUint32(4, true), wav.byteLength - 8);
  assert.equal(view.getUint32(16, true), 16); assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 2); assert.equal(view.getUint32(24, true), 44100);
  assert.equal(view.getUint32(28, true), 176400); assert.equal(view.getUint16(32, true), 4);
  assert.equal(view.getUint16(34, true), 16); assert.equal(view.getUint32(40, true), 28);
  const samples = Array.from({ length: 14 }, (_, i) => view.getInt16(44 + i * 2, true));
  assert.deepEqual(samples, [-32768, 32767, -16384, 16384, 0, 0, 16384, -16384, 32767, -32768, 32767, -32768, 0, 0]);
  for (const overrides of [{ length: 0 }, { numberOfChannels: 0 }, { sampleRate: 0 }, { length: 2 ** 32 }]) {
    assert.throws(() => h.context.encodeWav({ ...buffer, ...overrides }), /Unsupported WAV dimensions/);
  }
});

(async () => {
  for (const { name, run } of tests) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { console.error(`FAIL ${name}`); throw error; }
  }
  console.log(`${tests.length} socket-free tests passed. Browser and device execution were not performed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
