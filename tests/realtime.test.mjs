import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../transcription/static/realtime-audio.js', import.meta.url), 'utf8').replaceAll('export function', 'function') + '\n' + readFileSync(new URL('../transcription/static/realtime.js', import.meta.url), 'utf8').replace(/^import .*;\n/, '');
const worklet = readFileSync(new URL('../transcription/static/realtime-worklet.js', import.meta.url), 'utf8');

function page() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', dataset: {stream: '/realtime/stream', save: '/save', worklet: '/worklet'}, checked: true,
      handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; },
      reportValidity: () => true,
      getContext: () => ({clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}}),
    });
    return elements.get(id);
  }
  const handlers = {}, requests = [];
  let capture, processor, saveFailure = false;
  const track = {enabled: true, readyState: 'live', addEventListener() {}, stop() { this.readyState = 'ended'; }};
  class Processor {
    constructor() { this.port = {postMessage(data) { queueMicrotask(() => capture.port.onmessage({data})); }}; }
  }
  vm.runInNewContext(worklet, {AudioWorkletProcessor: Processor, registerProcessor(_name, implementation) { processor = new implementation(); }});
  class Capture {
    constructor() {
      capture = this;
      this.port = {postMessage(data) { queueMicrotask(() => processor.port.onmessage({data})); }};
    }
    connect() {}
  }
  class Context {
    sampleRate = 16000;
    audioWorklet = {addModule: async () => {}};
    createAnalyser() { return {getByteTimeDomainData() {}}; }
    createMediaStreamSource() { return {connect() {}}; }
    async resume() {}
    async close() {}
  }
  let ws;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    bufferedAmount = 0;
    bytes = 0;
    constructor(url) {
      ws = this;
      queueMicrotask(() => this.onmessage?.({data: JSON.stringify({type: 'ready'})}));
    }
    send(data) {
      if (typeof data === 'string') {
        queueMicrotask(() => {
          if (this.bytes) this.onmessage?.({data: JSON.stringify({type: 'segment', text: 'Hello'})});
          this.onmessage?.({data: JSON.stringify({type: 'done'})});
        });
      } else {
        this.bytes += data.length;
        queueMicrotask(() => {
          this.onmessage?.({data: JSON.stringify({type: 'ack', bytes: data.length})});
          this.onmessage?.({data: JSON.stringify({type: 'partial', text: 'Hel'})});
        });
      }
    }
    close() { this.readyState = 3; queueMicrotask(() => this.onclose?.()); }
  }
  const sandbox = {
    document: {getElementById: element, addEventListener(name, fn) { handlers[name] = fn; }},
    window: {AudioWorkletNode: Capture, addEventListener(name, fn) { handlers[name] = fn; }, location: {href: 'https://app/realtime', protocol: 'https:', assign(url) { sandbox.destination = url; }}, confirm: () => false},
    navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => [track], getAudioTracks: () => [track]})}},
    AudioContext: Context, AudioWorkletNode: Capture, Blob, FormData, WebSocket: Socket, URL, setTimeout, clearTimeout,
    Date: class extends Date {
      constructor() { super(2026, 8, 18, 14, 20); }
    },
    requestAnimationFrame() {}, cancelAnimationFrame() {},
    async fetch(url, {body}) {
      requests.push({url, body});
      return {url: 'https://app/transcription/realtime/save', ok: !saveFailure, status: saveFailure ? 503 : 201, json: async () => saveFailure ? {error: 'Save unavailable'} : {url: './../jobs/saved'}};
    },
  };
  vm.runInNewContext(script, sandbox);
  return {
    element, handlers, requests, sandbox, track,
    click: id => element(id).handlers.click(),
    disconnect() { ws.onclose(); },
    receive(event) { ws.onmessage({data: JSON.stringify(event)}); },
    failSave(value) { saveFailure = value; },
    samples(count) { processor.process([[new Float32Array(count).fill(0.25)]]); },
    settle: () => new Promise(resolve => setImmediate(resolve)),
  };
}

test('pause flushes tail, resume excludes paused audio, save includes edited text and PCM', async () => {
  const app = page();
  assert.equal(app.element('recording-title').value, 'Untitled_Recordings_2026-09-18 14:20');
  let initiallyDirty = false;
  app.handlers.beforeunload({preventDefault() { initiallyDirty = true; }});
  assert.equal(initiallyDirty, false);
  await app.click('record-start');
  app.samples(800); // Below worklet buffer size: must flush before saving.
  await app.click('record-pause');
  await app.settle();
  assert.equal(app.element('recording-transcript').value, 'Hello');
  app.samples(400); // Paused samples must not be recorded.
  await app.click('record-start');
  app.samples(1600);
  await app.click('record-pause');
  await app.settle();
  app.element('recording-transcript').value = 'Edited transcript';
  await app.click('record-save');
  const saved = app.requests.find(request => request.url === '/save').body;
  assert.equal(saved.get('text'), 'Edited transcript');
  assert.equal(saved.get('incomplete'), 'false');
  const bytes = await saved.get('audio').arrayBuffer();
  assert.equal(bytes.byteLength, 44 + 2400 * 2);
  const header = new DataView(bytes);
  assert.equal(header.getUint32(24, true), 16000);
  assert.equal(header.getUint32(40, true), 4800);
  assert.equal(header.getInt16(44, true), 8191);
  assert.equal(app.sandbox.destination, 'https://app/transcription/jobs/saved');
  let warned = false;
  app.handlers.beforeunload({preventDefault() { warned = true; }});
  assert.equal(warned, false);
});

test('partial revisions replace the preview and only the final segment is appended', async () => {
  const app = page();
  await app.click('record-start');
  app.samples(1600); await app.settle();
  const first = '今天早上，我到附近的公园。';
  const expanded = '今天早上，我到附近的公园散步。阳光穿过树叶，微风吹来。';
  app.receive({type: 'partial', text: first});
  app.receive({type: 'partial', text: expanded});
  app.receive({type: 'partial', text: expanded});
  assert.equal(app.element('recording-preview').textContent, expanded);
  assert.equal(app.element('recording-transcript').value, '');
  app.receive({type: 'segment', text: ' ' + expanded});
  app.receive({type: 'segment', text: '让人觉得十分舒服。'});
  assert.equal(app.element('recording-transcript').value, expanded + '\n让人觉得十分舒服。');
  await app.click('record-save');
  const saved = app.requests.at(-1).body;
  assert.equal(saved.get('text'), expanded + '\n让人觉得十分舒服。\nHello');
  assert.equal(saved.get('live_text'), saved.get('text'));
});

test('separate utterances preserve repeated speech, shared openings, and edits', async () => {
  const app = page();
  await app.click('record-start');
  const first = '今天早上，我到附近的公园。';
  for (const text of [first, first, '今天早上，我到附近的公园散步。', '今天早上。']) {
    app.receive({type: 'segment', text});
  }
  assert.equal(app.element('recording-transcript').value,
    [first, first, '今天早上，我到附近的公园散步。', '今天早上。'].join('\n'));
  app.receive({type: 'segment', text: first});
  app.element('recording-transcript').value = 'Manual correction';
  app.element('recording-transcript').handlers.input();
  app.receive({type: 'segment', text: '今天早上，我到附近的公园散步。'});
  assert.equal(app.element('recording-transcript').value, 'Manual correction\n今天早上，我到附近的公园散步。');
  await app.click('record-pause');
});

test('a resumed recording does not replace text from the previous capture interval', async () => {
  const app = page();
  await app.click('record-start');
  const first = '今天早上，我到附近的公园。';
  app.receive({type: 'segment', text: first});
  await app.click('record-pause');
  await app.click('record-start');
  app.receive({type: 'segment', text: '今天早上，我到附近的公园散步。'});
  assert.equal(app.element('recording-transcript').value, first + '\n今天早上，我到附近的公园散步。');
  await app.click('record-pause');
});

test('audio marks page dirty before first transcript; navigation can be cancelled', async () => {
  const app = page();
  await app.click('record-start');
  app.samples(2048);
  await app.settle();
  let warned = false;
  app.handlers.beforeunload({preventDefault() { warned = true; }});
  assert.equal(warned, true);
  let prevented = false;
  app.handlers.click({target: {closest: () => ({})}, preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
});

test('disconnect preserves audio and provisional text and marks saved transcript incomplete', async () => {
  const app = page();
  await app.click('record-start'); app.samples(1600); await app.settle();
  assert.equal(app.element('recording-transcript').value, '');
  assert.equal(app.element('recording-preview').textContent, 'Hel');
  app.disconnect(); await app.settle();
  await app.click('record-save');
  const saved = app.requests.at(-1).body;
  assert.equal(saved.get('incomplete'), 'true');
  assert.equal(saved.get('text'), 'Hel');
  assert.equal(saved.get('audio').size, 3244);
});

test('saving during recording flushes audio and queues final pass with edit protection', async () => {
  const app = page();
  await app.click('record-start'); app.samples(1600); await app.settle();
  app.element('recording-transcript').value = 'My correction';
  app.element('recording-transcript').handlers.input();
  app.samples(100);
  await app.click('record-save');
  const saved = app.requests.at(-1).body;
  assert.equal(saved.get('incomplete'), 'false');
  assert.equal(saved.get('audio').size, 44 + 1700 * 2);
  assert.equal(saved.get('final_transcription'), 'true');
  assert.equal(saved.get('edited'), 'true');
  assert.equal(saved.get('live_text'), 'Hello');
  assert.equal(saved.get('text'), 'My correction\nHello');
  assert.equal(app.track.enabled, false);
});

test('failed save retains recording, edits, and unload protection for retry', async () => {
  const app = page();
  await app.click('record-start'); app.samples(1600); await app.settle();
  app.failSave(true);
  await app.click('record-save');
  assert.equal(app.element('recording-error').textContent, 'Save unavailable');
  let warned = false;
  app.handlers.beforeunload({preventDefault() { warned = true; }});
  assert.equal(warned, true);
  assert.equal(app.sandbox.destination, undefined);
  app.failSave(false);
  await app.click('record-save');
  assert.equal(app.requests.at(-1).body.get('audio').size, 3244);
  assert.equal(app.requests.at(-1).body.get('text'), 'Hello');
  assert.equal(app.sandbox.destination, 'https://app/transcription/jobs/saved');
});
