/* Stub-DOM smoke test for vnote/web/app.js's live path + pcm-worklet.js.
 * No browser: fake document/fetch/AudioContext/MediaRecorder, run app.js in a vm. */

const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..', 'vnote', 'web');  // the page under test

let failures = 0;
function ok(cond, name, extra) {
  if (cond) { console.log('  PASS ' + name); }
  else { failures++; console.log('  FAIL ' + name + (extra !== undefined ? '  <' + JSON.stringify(extra) + '>' : '')); }
}
function eq(a, b, name) { ok(JSON.stringify(a) === JSON.stringify(b), name, { got: a, want: b }); }

/* ------------------------------------------------------------------ fake DOM */

class El {
  constructor(tag) {
    this.tagName = tag; this.id = ''; this.children = []; this.parentNode = null;
    this._text = ''; this.hidden = false; this.disabled = false; this.value = '';
    this.checked = false; this.title = ''; this.dataset = {}; this.style = {};
    this.listeners = {}; this.readOnly = false; this.spellcheck = false;
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
    const set = new Set();
    this.classList = {
      add: c => set.add(c), remove: c => set.delete(c),
      toggle: (c, on) => (on ? set.add(c) : set.delete(c)), contains: c => set.has(c)
    };
  }
  get childNodes() { return this.children; }
  get options() { return this.children.filter(c => c.tagName === 'option'); }
  get lastChild() { return this.children[this.children.length - 1]; }
  get textContent() {
    return this.children.length ? this.children.map(c => c.textContent).join('') : this._text;
  }
  set textContent(v) { this.children.length = 0; this._text = String(v); }
  get innerHTML() { return ''; }
  set innerHTML(v) { this.children.length = 0; this._text = ''; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  setAttribute(k, v) { this[k] = v; }
  getAttribute(k) { return k in this ? this[k] : null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, ev) {
    (this.listeners[t] || []).forEach(f => f(Object.assign(
      { currentTarget: this, preventDefault() {}, blur() {} }, ev)));
  }
  querySelector() { return new El('div'); }
  querySelectorAll() { return []; }
  select() {} setSelectionRange() {} load() {} blur() {} removeAttribute(k) { delete this[k]; }
}

// the markup, minus the explanatory comments (which name ids too)
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
const byId = {};
for (const id of ids) { const el = new El('div'); el.id = id; byId[id] = el; }

// the markup's own <option>s: only id-bearing elements are built above, and
// selectIfPresent() only sets a value the select actually offers
['light', 'edit', 'summary', 'dictation', 'raw'].forEach(v => {
  const opt = new El('option');
  opt.value = v;
  byId['pick-mode'].appendChild(opt);
});
['light', 'edit', 'summary', 'dictation'].forEach(v => {   // Regenerate cannot run "raw"
  const opt = new El('option');
  opt.value = v;
  byId['regenerate-mode'].appendChild(opt);
});

// the live text lives inside the #live scroller (livePane())
const inner = new El('div');
inner.appendChild(byId['live-committed']);
inner.appendChild(byId['live-tail']);
byId['live'].appendChild(inner);

const state = () => byId['view-note'].dataset.state;
const liveMode = () => byId['view-note'].dataset.live;
const rows = () => byId['notes-list'].children.filter(c => c.dataset && c.dataset.name !== undefined);
const days = () => byId['notes-list'].children.filter(c => c.className === 'day').map(c => c.textContent);

const docOn = {};
const document = {
  readyState: 'complete',
  body: new El('body'),
  hidden: false,
  getElementById: id => byId[id] || null,
  createElement: tag => new El(tag),
  addEventListener(t, f) { (docOn[t] = docOn[t] || []).push(f); },
  // insertText is how the paste handler puts plain text in the contenteditable
  // title — the only one on the page, so it stands in for the caret here
  execCommand: (cmd, _ui, text) => {
    if (cmd !== 'insertText') return true;
    byId['note-title'].textContent = byId['note-title'].textContent + text;
    return true;
  },
  activeElement: null
};

/* ---------------------------------------------------------------- fake audio */

const audioLog = [];
function fakeNode() { return { connect() {}, disconnect() { audioLog.push('disconnect'); } }; }

let lastCtx = null;
let workletPort = null;   // the harness feeds PCM through this
let ctxStuck = false;     // an AudioContext that refuses to leave 'suspended'
class FakeAudioContext {
  constructor(opts) {
    this.sampleRate = (opts && opts.sampleRate) || 48000;
    this.state = ctxStuck ? 'suspended' : 'running';
    this.destination = {};
    this.audioWorklet = { addModule: async (u) => { audioLog.push('addModule ' + u); } };
    lastCtx = this;
  }
  createMediaStreamSource() { return fakeNode(); }
  createGain() { return Object.assign(fakeNode(), { gain: { value: 1 } }); }
  suspend() { this.state = 'suspended'; return Promise.resolve(); }
  resume() { if (!ctxStuck) this.state = 'running'; return Promise.resolve(); }
  close() { this.state = 'closed'; audioLog.push('close'); return Promise.resolve(); }
}

// null: answer a flush the way the worklet does — one tagged, possibly empty buffer
let onFlushRequest = null;
class FakeAudioWorkletNode {
  constructor(ctx, name) {
    audioLog.push('node ' + name);
    const self = this;
    this.port = {
      onmessage: null,
      postMessage(msg) {
        audioLog.push('port ' + JSON.stringify(msg));
        if (!(msg && msg.type === 'flush')) return;
        if (onFlushRequest) return onFlushRequest(self.port);
        setTimeout(() => self.port.onmessage &&
          self.port.onmessage({ data: { type: 'flush', buffer: new ArrayBuffer(0) } }), 0);
      }
    };
    workletPort = this.port;
  }
  connect() {} disconnect() { audioLog.push('disconnect'); }
}

/* -------------------------------------------------------------- fake aborts */

class FakeAbortSignal {
  constructor() { this.aborted = false; this._on = []; }
  addEventListener(t, f) { if (t === 'abort') this._on.push(f); }
  _abort() { if (this.aborted) return; this.aborted = true; this._on.forEach(f => f()); }
}
class FakeAbortController {
  constructor() { this.signal = new FakeAbortSignal(); }
  abort() { this.signal._abort(); }
}
const FakeAbortSignalCtor = function () {};
FakeAbortSignalCtor.timeout = ms => {
  const s = new FakeAbortSignal();
  setTimeout(() => s._abort(), ms);
  return s;
};

/* ---------------------------------------------------------------- fake fetch */

let routes = {};
const calls = [];
async function fetch(url, options) {
  options = options || {};
  calls.push({ url, method: options.method || 'GET', body: options.body, signal: options.signal });
  const key = Object.keys(routes).find(k => url.split('?')[0] === k);
  const handler = key ? routes[key] : null;
  const work = handler ? handler(url, options)
                       : Promise.resolve({ status: 404, body: { error: 'no route ' + url } });
  const signal = options.signal;
  const res = signal
    ? await Promise.race([work, new Promise((_, rej) => {
        if (signal.aborted) return rej(new Error('TimeoutError'));
        signal.addEventListener('abort', () => rej(new Error('TimeoutError')));
      })])
    : await work;
  return {
    ok: !res.status || res.status < 400,
    status: res.status || 200,
    statusText: '',
    headers: { get: () => 'application/json' },
    json: async () => res.body
  };
}

let micTracksStopped = 0;
const navigator = {
  clipboard: null,
  mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { micTracksStopped++; } }] }) }
};

const store = {};
const win = {
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  setTimeout, clearTimeout, setInterval, clearInterval,
  AudioWorklet: function () {}, AudioContext: FakeAudioContext,
  addEventListener(t, f) { (win._on = win._on || {})[t] = f; },
  confirm: () => confirmAnswer
};
let confirmAnswer = true;

class FakeMediaRecorder {
  static isTypeSupported() { return true; }
  constructor() { this.state = 'inactive'; FakeMediaRecorder.made++; }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; if (this.onstop) this.onstop(); }
}
FakeMediaRecorder.made = 0;

class FakeBlob {
  constructor(parts, opts) {
    this.parts = parts || [];
    this.type = (opts && opts.type) || '';
    this.size = this.parts.reduce((n, p) => n + (p.length || p.byteLength || 1), 0);
  }
}

const sandbox = {
  window: win, document, navigator, fetch, console,
  performance: { now: () => Date.now() },
  setTimeout, clearTimeout, setInterval, clearInterval,
  AudioContext: FakeAudioContext, AudioWorkletNode: FakeAudioWorkletNode,
  MediaRecorder: FakeMediaRecorder,
  AbortController: FakeAbortController, AbortSignal: FakeAbortSignalCtor,
  Blob: FakeBlob,
  Promise, JSON, Math, Date, Error, Number, String, Array, Object,
  Uint8Array, Int16Array, Float32Array, ArrayBuffer, DataView, isNaN, parseInt
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const G = sandbox;
const $ = id => byId[id];
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ------------------------------------------------------------------- routes */

let appendReply = { partial: '', committed: [], tail: '', seconds: 0 };
let appendFail = false;
let appendStatus = 500;
let appendHang = false;
const NOTE = { name: 'n1', title: 'A note', note: '# A note', transcript: 't', meta: {}, live_transcript: 'live' };

const iso = (daysAgo, h, m) => {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};
const NOTES = [
  { name: 'n1', title: 'A note', created: iso(0, 14, 12), duration_s: 401, mode: 'edit' },
  { name: 'n2', title: 'Standup notes', created: iso(0, 9, 58), duration_s: 182, mode: 'summary' },
  { name: 'n3', title: 'Older thing', created: iso(8, 16, 47), duration_s: 269, mode: null }
];
let noteDetail = () => ({
  name: 'n1', title: 'A note', created: iso(0, 14, 12), duration_s: 401,
  mode: 'edit', backend: 'ollama',
  note: '# A note\n\nthe body', transcript: 'raw words here',
  meta: {
    whisper_model: 'large-v3-turbo', language: 'en', cleanup_mode: 'edit',
    cleanup_backend: 'ollama', cleanup_model: 'qwen2.5:14b',
    transcribe_seconds: 11.4, cleanup_seconds: 18.9
  },
  audio_url: '/api/notes/n1/audio', path: '/x/voice-notes/n1',
  versions: [
    { n: 1, op: 'clean', mode: 'edit', created: iso(0, 14, 19) },
    { n: 2, op: 'revise', instructions: 'shorter', created: iso(0, 14, 32) }
  ]
});
function baseRoutes() {
  return {
    '/health': async () => ({ body: { version: '0.5.0', whisper_model: 'large-v3-turbo', device: 'cuda',
                                     warm: true, ollama: 'ready' } }),
    '/api/settings': async () => ({ body: { settings: [] } }),
    '/api/note': async () => ({ body: NOTE }),
    '/stream/start': async () => ({ body: { session_id: 'SID1' } }),
    '/stream/append': async () => {
      if (appendHang) return new Promise(() => {});   // never answers: the timeout must
      return appendFail ? { status: appendStatus, body: { error: 'boom' } } : { body: appendReply };
    },
    '/stream/ping': async () => ({ body: { ok: true } }),
    '/stream/cancel': async () => ({ body: { cancelled: true } }),
    '/stream/finish': async () => ({ body: NOTE }),
    '/api/notes': async () => ({ body: { notes: NOTES } }),
    '/api/notes/n1': async () => ({ body: noteDetail() }),
    '/api/notes/n1/note': async () => ({ body: { version: 3 } }),
    '/api/notes/n1/versions/1': async () => ({ body: { text: '# A note\n\nv1 body' } }),
    '/api/notes/n1/restore': async () => ({ body: { version: 4 } }),
    '/api/notes/n1/revise': async () => ({ body: { title: 'A note', note: '# A note\n\nshorter', version: 3 } }),
    '/api/vocab': async () => ({ body: { text: 'vnote\nollama' } })
  };
}

function feed(bytes) { workletPort.onmessage({ data: new Uint8Array(bytes).buffer }); }
const appendCalls = () => calls.filter(c => c.url.startsWith('/stream/append'));

/* -------------------------------------------------------------------- tests */

(async function run() {
  routes = baseRoutes();          // app.js boots itself on load: /health must answer
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8'), sandbox, { filename: 'app.js' });
  G.LIVE_MIN_REQUEST_MS = 0;      // no need to wait a real second per append
  G.LIVE_FLUSH_WAIT_MS = 50;
  await sleep(10);

  console.log('\n1. toggle default + persistence');
  ok($('live-toggle').checked === true, 'live default on when AudioWorklet exists');
  ok($('live-toggle').disabled === false, 'live toggle enabled');
  $('live-toggle').fire('change');
  ok(JSON.parse(store['vnote.picks']).live === true, 'live state saved with the picks');
  ok($('process-toggle').checked === true, 'process-on-stop defaults on');
  $('process-toggle').fire('change');
  ok(JSON.parse(store['vnote.picks']).process === true, 'and it is saved with the picks too');
  store['vnote.picks'] = JSON.stringify({ mode: 'edit', backend: 'ollama', language: '', live: true, process: false });
  G.initProcessToggle();
  ok($('process-toggle').checked === false, 'a remembered "off" comes back');
  $('process-toggle').checked = true;
  $('process-toggle').fire('change');   // back to the default for the sections below
  ok($('retry-wrap').hidden === true, 'retry banner hidden with nothing to retry');
  ok(state() === 'idle', 'the stage starts idle', state());
  ok($('rec-status').textContent === 'New note', 'topbar label', $('rec-status').textContent);

  console.log('\n2. paragraph join rule (stream.py _committed_text)');
  eq(G.committedParagraphs([
    { text: 'one', trailing_silence_s: 0.3 },
    { text: '', trailing_silence_s: 9 },          // silence-only segment: skipped, no gap change
    { text: 'two', trailing_silence_s: 2.5 },
    { text: 'three', trailing_silence_s: 0 }
  ]), ['one two', 'three'], 'joins on 2 s silence, skips empty segments');
  eq(G.committedParagraphs([]), [], 'no committed segments -> no paragraphs');

  console.log('\n3. start a live recording');
  $('pick-language').value = '';
  $('pick-mode').value = 'edit';
  $('pick-backend').value = 'ollama';
  calls.length = 0;
  const startPromise = G.startRecording();
  await startPromise;
  const startCall = calls.find(c => c.url === '/stream/start');
  ok(!!startCall, 'POST /stream/start');
  eq(JSON.parse(startCall.body), { language: null }, 'blank language -> null');
  ok(lastCtx && lastCtx.sampleRate === 16000, 'AudioContext requested at 16 kHz');
  ok(audioLog.includes('addModule /static/pcm-worklet.js'), 'worklet module added');
  ok(audioLog.includes('node pcm-capture'), 'AudioWorkletNode("pcm-capture")');
  ok(state() === 'recording', 'stage: recording', state());
  ok($('rec-status').textContent === 'Recording', 'topbar label', $('rec-status').textContent);
  ok($('process-status').textContent === '', 'nothing to report on the status line');
  ok($('record').disabled && !$('stop').disabled && !$('pause').disabled, 'transport in recording state');
  ok($('stop').textContent === 'Stop', 'stop labelled Stop once recording', $('stop').textContent);
  ok($('pick-mode').disabled && $('live-toggle').disabled, 'picks locked while recording');
  ok(liveMode() === 'on', 'the live transcript is the stage', liveMode());

  console.log('\n4. a second Record click cannot start a second session');
  const before = calls.filter(c => c.url === '/stream/start').length;
  await G.startRecording();
  ok(calls.filter(c => c.url === '/stream/start').length === before, 'no second /stream/start');

  console.log('\n5. PCM is queued, coalesced and appended one request at a time');
  appendReply = { partial: 'hello there', committed: [{ text: 'hello there', trailing_silence_s: 0.2, start_s: 0, end_s: 1 }], tail: 'and', seconds: 1 };
  G.LIVE_MIN_REQUEST_MS = 5000;   // hold the pump so both chunks queue up
  feed([1, 2, 3, 4]);
  feed([5, 6]);
  await sleep(20);
  ok(appendCalls().length === 0, 'rate limit holds the append back');
  G.LIVE_MIN_REQUEST_MS = 0;
  await sleep(300);               // the 200 ms pump tick
  const appends = appendCalls();
  ok(appends.length === 1, 'one coalesced append for two chunks', appends.length);
  eq(Array.from(appends[0].body), [1, 2, 3, 4, 5, 6], 'bytes coalesced in order');
  ok(appends[0].url === '/stream/append?sid=SID1', 'sid in the query');
  eq($('live-committed').children.map(p => p.textContent), ['hello there'], 'committed paragraph rendered');
  ok($('live-tail').textContent === 'and', 'tail rendered');

  console.log('\n6. diff renderer keeps settled paragraphs as the same nodes');
  const firstNode = $('live-committed').children[0];
  appendReply = {
    partial: '', tail: 'still going', seconds: 3,
    committed: [
      { text: 'hello there', trailing_silence_s: 2.4, start_s: 0, end_s: 1 },
      { text: 'second para', trailing_silence_s: 0.1, start_s: 2, end_s: 3 }
    ]
  };
  feed([7]);
  await sleep(30);
  eq($('live-committed').children.map(p => p.textContent), ['hello there', 'second para'], 'new paragraph appended');
  ok($('live-committed').children[0] === firstNode, 'paragraph 0 node untouched (selection survives)');
  const secondNode = $('live-committed').children[1];
  appendReply = {
    partial: '', tail: '', seconds: 4,
    committed: [
      { text: 'hello there', trailing_silence_s: 2.4, start_s: 0, end_s: 1 },
      { text: 'second para extended', trailing_silence_s: 0.1, start_s: 2, end_s: 4 }
    ]
  };
  feed([8]);
  await sleep(30);
  ok($('live-committed').children[0] === firstNode, 'unchanged paragraph still the same node');
  ok($('live-committed').children[1] !== secondNode, 'changed paragraph replaced');
  ok($('live-tail').textContent === '', 'tail cleared when the reply has none');

  console.log('\n7. copy joins committed + tail');
  appendReply.tail = 'tail bit';
  feed([9]);
  await sleep(30);
  ok(G.liveText() === 'hello there\n\nsecond para extended tail bit', 'liveText()', G.liveText());

  console.log('\n8. pause / resume');
  const t0 = $('timer').textContent;
  G.togglePause();
  ok(lastCtx.state === 'suspended', 'AudioContext suspended');
  ok(state() === 'paused', 'stage: paused', state());
  ok($('rec-status').textContent === 'Paused', 'topbar label', $('rec-status').textContent);
  await sleep(30);
  ok($('timer').textContent === t0, 'timer frozen while paused', $('timer').textContent);
  G.togglePause();
  await sleep(5);
  ok(lastCtx.state === 'running', 'AudioContext resumed');
  ok(state() === 'recording', 'stage back to recording', state());
  ok($('pause').textContent === 'Pause', 'pause label back', $('pause').textContent);

  console.log('\n9. append failures keep the bytes, warn after 3, then back off');
  appendFail = true;
  G.LIVE_BACKOFF_MS = 200;
  feed([10, 11]); await sleep(20);
  feed([12]); await sleep(20);
  feed([13]); await sleep(20);
  ok(/live transcript stopped updating/.test($('process-status').textContent), 'warning after 3 failures', $('process-status').textContent);
  appendFail = false;
  calls.length = 0;
  feed([14]); await sleep(30);
  ok(appendCalls().length === 0, 'the backoff holds the next attempt back', appendCalls().length);
  await sleep(400);
  const retried = appendCalls()[0];
  eq(Array.from(retried.body), [10, 11, 12, 13, 14], 'failed bytes retried, in order');
  ok($('process-status').textContent === '', 'the warning is withdrawn', $('process-status').textContent);

  console.log('\n10. stop -> flush -> final append -> finish -> result');
  calls.length = 0;
  micTracksStopped = 0;
  appendReply = { partial: '', committed: [{ text: 'hello there', trailing_silence_s: 2.4 }, { text: 'second para extended', trailing_silence_s: 0 }], tail: 'tail bit', seconds: 9 };
  G.LIVE_MIN_REQUEST_MS = 5000;   // hold it back: the tail must go out on Stop
  feed([15]);                     // queued; stop must still send it
  await G.stopLive();
  G.LIVE_MIN_REQUEST_MS = 0;
  ok(audioLog.includes('port {"type":"flush"}'), 'flush posted to the worklet');
  const finalAppend = appendCalls().pop();
  ok(!!finalAppend && Array.from(finalAppend.body).includes(15), 'the last queued bytes were sent');
  ok(!!finalAppend.signal, 'the final append carries an abort signal');
  ok(lastCtx.state === 'closed', 'AudioContext closed');
  ok(micTracksStopped === 1, 'microphone released');
  const fin = calls.find(c => c.url.startsWith('/stream/finish'));
  ok(fin.url === '/stream/finish?sid=SID1&note=1&mode=edit&backend=ollama&language=auto', 'finish query', fin.url);
  ok(!fin.signal, 'no timeout on finish — transcription is slow on purpose');
  ok(state() === 'note', 'the finished note took the stage', state());
  ok($('note-editor').value === '# A note\n\nthe body', 'the note is open on the stage', $('note-editor').value);
  ok($('note-title').textContent === 'A note', 'title from the "# " heading', $('note-title').textContent);
  ok($('note-raw').value === 'raw words here', 'raw transcript in the drawer');
  ok($('note').dataset.raw === 'shown', 'the raw drawer starts open');
  ok(!!calls.find(c => c.url === '/api/notes/n1'), 'the note was opened the way the sidebar opens it');
  ok(rows().length === 3, 'the sidebar was refreshed', rows().length);
  ok($('retry-wrap').hidden === true, 'nothing to retry after a clean finish');
  ok(!$('record').disabled && $('stop').disabled, 'transport back to ready',
     { record: $('record').disabled, stop: $('stop').disabled, blocked: $('record').dataset.blocked });
  ok(G.liveText() === 'hello there\n\nsecond para extended tail bit', 'live text still copyable after stop');

  console.log('\n10b. a batch that arrives before the flush reply is not the reply');
  calls.length = 0;
  G.LIVE_FLUSH_WAIT_MS = 400;
  onFlushRequest = port => {
    setTimeout(() => port.onmessage({ data: new Uint8Array([90, 91]).buffer }), 0);   // a plain batch
    setTimeout(() => port.onmessage({ data: { type: 'flush', buffer: new Uint8Array([92]).buffer } }), 40);
  };
  await G.startRecording();
  G.LIVE_MIN_REQUEST_MS = 5000;   // keep everything queued for the final append
  feed([89]);
  await G.stopLive();
  const crossing = appendCalls().pop();
  eq(Array.from(crossing.body), [89, 90, 91, 92], 'stop waited for the tagged flush reply');
  onFlushRequest = null;
  G.LIVE_MIN_REQUEST_MS = 0;
  G.LIVE_FLUSH_WAIT_MS = 50;

  console.log('\n11. raw mode + explicit language in the finish query');
  $('pick-mode').value = 'raw';
  $('pick-language').value = 'de';
  ok(G.finishQuery('S2') === '?sid=S2&note=1&raw=1&language=de', 'raw finish query', G.finishQuery('S2'));
  $('pick-mode').value = 'edit';
  $('pick-language').value = '';

  console.log('\n11b. process-on-stop off: a raw note whatever the mode says');
  $('process-toggle').checked = false;
  ok(G.finishQuery('S3') === '?sid=S3&note=1&raw=1&language=auto',
     'no mode, no backend, raw=1', G.finishQuery('S3'));
  ok(G.noteQuery('webm') === '?format=webm&raw=1&language=auto',
     'the upload path says the same', G.noteQuery('webm'));
  ok(/no cleanup/.test(G.processingCopy()), 'and the stop line promises none', G.processingCopy());
  $('process-toggle').checked = true;
  ok(G.finishQuery('S3') === '?sid=S3&note=1&mode=edit&backend=ollama&language=auto',
     'back on: the mode pick counts again', G.finishQuery('S3'));

  console.log('\n12. /stream/start failing falls back to MediaRecorder');
  routes['/stream/start'] = async () => ({ status: 500, body: { error: 'nope' } });
  FakeMediaRecorder.made = 0;
  await G.startRecording();
  ok(FakeMediaRecorder.made === 1, 'MediaRecorder took over');
  ok(/live transcript unavailable/.test($('process-status').textContent),
     'the fallback is named in the status', $('process-status').textContent);
  ok($('process-status').textContent.indexOf('nope') !== -1,
     'the reason survives the switch to recording', $('process-status').textContent);
  ok(liveMode() === 'off', 'the big timer is the stage for a fallback take', liveMode());
  G.stopRecording();
  await sleep(30);

  console.log('\n13. live off -> MediaRecorder, pane hidden');
  routes = baseRoutes();
  $('live-toggle').checked = false;
  FakeMediaRecorder.made = 0;
  calls.length = 0;
  await G.startRecording();
  ok(FakeMediaRecorder.made === 1, 'MediaRecorder path');
  ok(calls.filter(c => c.url === '/stream/start').length === 0, 'no stream session');
  ok(liveMode() === 'off', 'live pane off', liveMode());
  ok($('process-status').textContent === '', 'no stale fallback reason', $('process-status').textContent);
  ok($('timer').textContent === $('big-timer').textContent, 'the big timer tracks the small one');
  G.stopRecording();
  await sleep(30);

  console.log('\n14. 500 from finish shows audio_kept');
  $('live-toggle').checked = true;
  routes['/stream/finish'] = async () => ({ status: 500, body: { error: 'RuntimeError: gpu', audio_kept: '/x/voice-notes/failed/live-1.wav' } });
  await G.startRecording();
  feed([1]);
  await sleep(20);
  await G.stopLive();
  ok(/kept the audio/.test($('process-status').textContent) &&
     $('process-status').textContent.includes('/x/voice-notes/failed/live-1.wav'),
     'the kept path is shown', $('process-status').textContent);
  ok($('retry-wrap').hidden === true, 'no Retry when the daemon kept the audio');
  ok(!$('record').disabled, 'record re-enabled after a failed finish');

  console.log('\n15. Stop while still requesting the microphone (via the button)');
  routes = baseRoutes();
  let release;
  navigator.mediaDevices.getUserMedia = () => new Promise(r => { release = () => r({ getTracks: () => [{ stop() { micTracksStopped++; } }] }); });
  const pending = G.startRecording();
  ok(!$('stop').disabled, 'Stop is reachable while starting');
  ok($('stop').textContent === 'Cancel', 'Stop is labelled Cancel while starting', $('stop').textContent);
  ok($('view-note').dataset.starting === 'true', 'the starting flag reaches the CSS');
  $('stop').fire('click');
  ok($('process-status').textContent === 'cancelling…', 'cancel acknowledged');
  release();
  await pending;
  ok(state() === 'idle' && $('process-status').textContent === '', 'back to the idle stage', state());
  ok($('view-note').dataset.starting === undefined, 'the starting flag is gone');
  ok($('stop').textContent === 'Stop', 'label back to Stop');
  ok(!$('record').disabled, 'record enabled again');
  ok(G.live === null, 'no live session left behind');
  navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [{ stop() { micTracksStopped++; } }] });

  console.log('\n16. an AudioContext that will not resume falls back and cancels the session');
  ctxStuck = true;
  calls.length = 0;
  FakeMediaRecorder.made = 0;
  await G.startRecording();
  ok(FakeMediaRecorder.made === 1, 'MediaRecorder took over');
  ok(/audio context stayed suspended/.test($('process-status').textContent),
     'the suspended context is the stated reason', $('process-status').textContent);
  const cancelled = calls.find(c => c.url.startsWith('/stream/cancel'));
  ok(!!cancelled && cancelled.url === '/stream/cancel?sid=SID1' && cancelled.method === 'POST',
     'the unused session is cancelled, not finished', cancelled && cancelled.url);
  ok(!calls.some(c => c.url.startsWith('/stream/finish')), 'no finish for an abandoned session');
  ok(liveMode() === 'off', 'the live pane is off for the fallback take', liveMode());
  G.stopRecording();
  await sleep(30);
  ctxStuck = false;

  console.log('\n17. a 404 from append ends the take and keeps the audio for Retry');
  await G.startRecording();
  ok(G.live !== null, 'live session running');
  micTracksStopped = 0;
  appendFail = true; appendStatus = 404;
  feed([1, 2, 3]);
  await sleep(60);
  ok(G.live === null, 'the take stopped');
  ok(/daemon lost this live session/.test($('retry-detail').textContent),
     'session-lost detail in the banner', $('retry-detail').textContent);
  ok(micTracksStopped === 1, 'microphone released');
  ok(lastCtx.state === 'closed', 'AudioContext closed');
  ok($('retry-wrap').hidden === false && $('retry').disabled === false, 'retry banner shown');
  ok(state() === 'idle', 'and the stage is idle again', state());
  ok(!$('record').disabled && $('stop').disabled, 'transport back to ready');
  appendFail = false; appendStatus = 500;

  console.log('\n18. Retry uploads the safety copy as a 16 kHz mono WAV');
  calls.length = 0;
  $('retry').fire('click');
  await sleep(30);
  const up = calls.find(c => c.url.startsWith('/api/note'));
  ok(!!up && up.url === '/api/note?format=wav&mode=edit&backend=ollama&language=auto',
     'posted as a wav note', up && up.url);
  const wav = up.body.parts[0];
  ok(up.body.type === 'audio/wav', 'blob type');
  ok(wav.length === 44 + 3, '44-byte header + the kept PCM', wav.length);
  const tag = (at, n) => Array.from(wav.slice(at, at + n)).map(b => String.fromCharCode(b)).join('');
  const u32 = at => wav[at] | (wav[at + 1] << 8) | (wav[at + 2] << 16) | (wav[at + 3] << 24);
  const u16 = at => wav[at] | (wav[at + 1] << 8);
  ok(tag(0, 4) === 'RIFF' && tag(8, 4) === 'WAVE' && tag(12, 4) === 'fmt ' && tag(36, 4) === 'data', 'RIFF/WAVE tags');
  ok(u32(4) === 36 + 3 && u32(16) === 16 && u32(40) === 3, 'sizes', { riff: u32(4), fmt: u32(16), data: u32(40) });
  ok(u16(20) === 1 && u16(22) === 1 && u16(34) === 16, 'PCM, mono, 16-bit');
  ok(u32(24) === 16000 && u32(28) === 32000 && u16(32) === 2, 'rate 16 kHz, byte rate, block align');
  eq(Array.from(wav.slice(44)), [1, 2, 3], 'the kept PCM rides along');
  ok($('retry-wrap').hidden === true && state() === 'note', 'a successful Retry clears it and opens the note', state());

  console.log('\n19. Stop when the daemon never gets the tail: no finish, honest status');
  await G.startRecording();
  appendReply = { partial: '', committed: [], tail: '', seconds: 4 };
  feed([20]);
  await sleep(30);
  G.LIVE_MIN_REQUEST_MS = 5000;   // hold the tail back so Stop has to send it
  feed([21]);
  await sleep(10);
  calls.length = 0;
  appendHang = true;              // and the daemon never answers
  G.LIVE_STOP_TIMEOUT_MS = 40;
  G.recElapsedMs = 12000;         // 12 s recorded, 4 s acknowledged
  await G.stopLive();
  ok(appendCalls().length === 2, 'two attempts at the tail', appendCalls().length);
  ok(!calls.some(c => c.url.startsWith('/stream/finish')), 'no finish for a truncated recording');
  ok(/did not receive the last 8 s/.test($('retry-detail').textContent),
     'the missing seconds are named', $('retry-detail').textContent);
  ok($('retry-wrap').hidden === false, 'Retry offered');
  ok(!!G.lastUpload && !!G.lastUpload.pcm && G.lastUpload.pcm.length === 2, 'the whole take is the safety copy');
  ok(G.live === null && $('stop').disabled, 'transport back to ready');
  appendHang = false;
  G.LIVE_MIN_REQUEST_MS = 0;

  console.log('\n19b. the same, on browsers without AbortSignal.timeout');
  const savedAbortSignal = G.AbortSignal;
  G.AbortSignal = undefined;      // only AbortController is left
  await G.startRecording();
  G.LIVE_MIN_REQUEST_MS = 5000;   // again, keep the tail for Stop
  feed([22]);
  await sleep(10);
  calls.length = 0;
  appendHang = true;
  await G.stopLive();
  ok(appendCalls().length === 2 && !!appendCalls()[0].signal, 'the manual AbortController still times out');
  ok(/did not receive the last/.test($('retry-detail').textContent), 'same message', $('retry-detail').textContent);
  appendHang = false;
  G.LIVE_MIN_REQUEST_MS = 0;
  G.AbortSignal = savedAbortSignal;

  console.log('\n20. finish failing without audio_kept offers Retry');
  routes['/stream/finish'] = async () => ({ status: 500, body: { error: 'RuntimeError: gpu' } });
  await G.startRecording();
  feed([30, 31]);
  await sleep(30);
  await G.stopLive();
  ok(/RuntimeError: gpu/.test($('retry-detail').textContent),
     'Retry offered when the daemon kept nothing', $('retry-detail').textContent);
  ok($('retry-wrap').hidden === false, 'retry shown');
  ok(!!G.lastUpload && !!G.lastUpload.pcm, 'the safety copy is what Retry would send');
  routes = baseRoutes();

  console.log('\n21. Retry is out of reach while a take is running');
  await G.startRecording();
  ok($('retry-wrap').hidden === true && $('retry').disabled === true, 'hidden and disabled while recording');
  G.togglePause();
  ok($('retry-wrap').hidden === true, 'still hidden while paused');
  G.togglePause();
  await sleep(5);
  await G.stopLive();
  ok($('retry-wrap').hidden === true, 'and gone for good after a clean finish');

  console.log('\n22. a live take blocks the tab from closing');
  const guard = win._on && win._on.beforeunload;
  ok(typeof guard === 'function', 'beforeunload guard registered');
  let prevented = 0;
  const ev = { preventDefault() { prevented++; }, returnValue: null };
  guard(ev);
  ok(prevented === 0, 'nothing running, nothing unsaved: no prompt');
  await G.startRecording();
  guard(ev);
  ok(prevented === 1 && ev.returnValue === '', 'a live take prompts', { prevented, rv: ev.returnValue });
  G.stopRecording();
  await sleep(50);

  console.log('\n23. no PCM within the watchdog window says so, and takes it back');
  G.LIVE_FIRST_PCM_MS = 60;
  await G.startRecording();
  await sleep(120);
  ok(/no audio is arriving from the microphone/.test($('process-status').textContent),
     'silent microphone reported', $('process-status').textContent);
  ok(G.live !== null, 'but the take keeps running');
  appendReply = { partial: '', committed: [], tail: '', seconds: 1 };
  feed([40]);
  await sleep(30);
  ok($('process-status').textContent === '', 'the warning is withdrawn once audio arrives', $('process-status').textContent);
  await G.stopLive();
  G.LIVE_FIRST_PCM_MS = 3000;

  console.log('\n24. the queue is capped per request');
  await G.startRecording();
  G.LIVE_MIN_REQUEST_MS = 5000;
  const big = new Uint8Array(20000);
  for (let i = 0; i < 4; i++) feed(big);   // 80 000 bytes queued
  await sleep(20);
  calls.length = 0;
  G.LIVE_MAX_BODY_BYTES = 30000;
  G.LIVE_MIN_REQUEST_MS = 0;
  await sleep(300);
  const capped = appendCalls()[0];
  ok(!!capped && capped.body.length === 20000, 'the body stops at the cap, not the whole backlog', capped && capped.body.length);
  ok(G.live.queue.length === 3, 'the rest stays queued for the next requests', G.live.queue.length);
  G.LIVE_MAX_BODY_BYTES = 16000 * 2 * 30;
  await G.stopLive();


  console.log('\n24b. the sidebar: day groups, rows, tags, stats');
  ok(state() === 'note', 'a note is on the stage', state());
  eq(days(), ['Today', days()[1]], 'today heads the list');
  ok(days().length === 2 && days()[1] !== 'Yesterday', 'the older note gets a dated header', days());
  ok(rows().length === 3, 'one row per note', rows().length);
  ok(rows()[0].children[0].textContent === 'A note', 'the title leads the row');
  ok(rows()[0].children[1].children.pop().textContent === 'edit', 'the mode is the row tag');
  ok(rows()[2].children[1].children.pop().textContent === 'raw', 'a note with no mode reads raw');
  ok(rows()[0].classList.contains('is-active'), 'the open note is the active row');
  ok($('stats-notes').textContent === '2 notes this week', 'stats: last 7 days only', $('stats-notes').textContent);
  ok($('stats-minutes').textContent === '10 min', 'stats: minutes recorded', $('stats-minutes').textContent);
  ok($('notes-empty').hidden === true && $('notes-list').hidden === false, 'the empty state is out of the way');

  console.log('\n24c. search filters the rows, and a click opens a note');
  $('notes-search').value = 'stand';
  $('notes-search').fire('input');
  ok(rows().length === 1 && rows()[0].dataset.name === 'n2', 'filtered to the matching title', rows().length);
  $('notes-search').value = 'zzz';
  $('notes-search').fire('input');
  ok(rows().length === 0, 'no match -> no rows');
  $('notes-search').value = '';
  $('notes-search').fire('input');
  ok(rows().length === 3, 'clearing the box brings them back');
  calls.length = 0;
  rows()[0].fire('click');
  await sleep(20);
  ok(!!calls.find(c => c.url === '/api/notes/n1'), 'the row opened that note');
  ok(state() === 'note' && $('app').dataset.view === 'note', 'on the note stage');

  console.log('\n24d. the title is the note\u2019s "# " heading line');
  ok($('note-title').contentEditable === 'true', 'editable when there is a heading');
  $('note-title').textContent = 'Renamed';
  $('note-title').fire('input');
  ok($('note-editor').value === '# Renamed\n\nthe body', 'the heading follows the title', $('note-editor').value);
  ok($('note-save').disabled === false, 'and that counts as an edit');

  console.log('\n24e. a note with no heading keeps a read-only title');
  const detail = noteDetail;
  noteDetail = () => Object.assign(detail(), { note: 'plain dictation text', versions: [] });
  await G.loadNote('n1');
  ok($('note-title').contentEditable === 'false', 'contenteditable off', $('note-title').contentEditable);
  ok($('note-title').title.indexOf('no heading line') !== -1, 'and it says why', $('note-title').title);
  ok($('note-title').textContent === 'A note', 'the title falls back to the folder title');
  ok($('version-select').disabled === true, 'no history -> the picker is off');

  console.log('\n24f. a failed cleanup shows the warning; a raw take does not');
  noteDetail = () => ({ name: 'n1', title: 'x', note: null, transcript: 'raw only',
                        meta: { cleanup_mode: 'edit' }, versions: [] });
  await G.loadNote('n1');
  ok($('note-warning').hidden === false, 'the cleanup-failed strip is shown');
  ok($('note').dataset.processed === undefined,
     'and not the calm state — the two are exclusive', $('note').dataset.processed);
  ok($('note-editor').value === 'raw only', 'the editor holds the transcript', $('note-editor').value);
  noteDetail = () => ({ name: 'n1', title: 'x', note: null, transcript: 'raw only',
                        meta: { cleanup_mode: null }, versions: [] });
  await G.loadNote('n1');
  ok($('note-warning').hidden === true, 'a deliberate raw note is not a failure');
  noteDetail = detail;
  await G.loadNote('n1');

  console.log('\n24f2. a raw note opens calm, with Regenerate ready');
  noteDetail = () => ({ name: 'n1', title: 'x', note: null, transcript: 'raw only',
                        meta: { cleanup_mode: null }, transcript_edited: false, versions: [] });
  $('pick-mode').value = 'summary';
  G.setRaw(false);                  // a raw note must open the drawer itself
  await G.loadNote('n1');
  ok($('note').dataset.processed === 'no', 'the not-processed state, not the failed banner',
     $('note').dataset.processed);
  ok($('note').dataset.raw === 'shown', 'and it opens on the transcript pane', $('note').dataset.raw);
  ok($('note-warning').hidden === true, 'and the failure strip stays away');
  ok($('regenerate-mode').value === 'summary', 'Regenerate offers the record panel\u2019s mode',
     $('regenerate-mode').value);
  $('pick-mode').value = 'raw';
  await G.loadNote('n1');
  ok($('regenerate-mode').value === 'edit', 'unless that pick is raw \u2014 then the daemon default',
     $('regenerate-mode').value);
  $('pick-mode').value = 'edit';
  noteDetail = detail;
  await G.loadNote('n1');
  ok($('note').dataset.processed === undefined, 'a processed note clears the state');
  ok($('regenerate-mode').value === 'edit', 'and keeps the mode it was made with');

  console.log('\n24f3. the raw pane is an editor; Save rewrites transcript.txt');
  let putTranscript = null;
  routes['/api/notes/n1/transcript'] = async (url, opts) => {
    putTranscript = JSON.parse(opts.body);
    return { body: { transcript: putTranscript.text, transcript_edited: true } };
  };
  ok($('note-raw').value === 'raw words here', 'the transcript fills the pane', $('note-raw').value);
  ok($('note-raw-save').disabled === true, 'nothing to save on a fresh load');
  $('note-raw').value = 'raw words, fixed';
  $('note-raw').fire('input');
  ok($('note-raw-save').disabled === false, 'an edit enables Save');
  calls.length = 0;
  await G.saveTranscript();
  const putRaw = calls.find(c => c.url === '/api/notes/n1/transcript');
  ok(!!putRaw && putRaw.method === 'PUT' && putTranscript.text === 'raw words, fixed',
     'PUT the edited transcript', putRaw && putRaw.url);
  ok(G.rawDirty() === false && $('note-raw-save').disabled === true,
     'and nothing is pending afterwards');
  await G.showVersion(1);
  ok($('note-raw-save').disabled === true, 'Save is off while an old version is previewed');
  await G.loadNote('n1');
  noteDetail = () => Object.assign(detail(), { transcript_edited: true });
  await G.loadNote('n1');
  ok($('note-raw-status').textContent === 'edited', 'a transcript that was edited says so',
     $('note-raw-status').textContent);
  noteDetail = detail;
  await G.loadNote('n1');
  ok($('note-raw-status').textContent === '', 'an untouched one says nothing');

  console.log('\n24f4. an unsaved transcript edit survives a note save; Regenerate offers to use it');
  await G.loadNote('n1');
  $('note-raw').value = 'transcript in progress';
  $('note-raw').fire('input');
  $('note-editor').value = '# A note\n\nedited body';
  $('note-editor').fire('input');
  await G.saveNote();
  ok($('note-raw').value === 'transcript in progress',
     'the reload after Save did not drop the raw edit', $('note-raw').value);
  ok(G.rawDirty() === true && $('note-raw-save').disabled === false, 'and it is still pending');

  routes['/api/notes/n1/reclean'] = async () => ({
    body: { title: 'A note', note: '# A note\n\nregenerated', version: 5 } });
  calls.length = 0;
  confirmAnswer = false;
  await G.regenerateNote();
  ok(!calls.find(c => c.url === '/api/notes/n1/reclean'), 'Cancel regenerates nothing');
  ok($('note-raw').value === 'transcript in progress', 'and keeps the edit');
  confirmAnswer = true;
  calls.length = 0;
  await G.regenerateNote();
  eq(calls.filter(c => /transcript$|reclean$/.test(c.url)).map(c => c.url),
     ['/api/notes/n1/transcript', '/api/notes/n1/reclean'],
     'OK saves the transcript first, then regenerates from it');
  ok(putTranscript.text === 'transcript in progress', 'and what it saved is the edit', putTranscript);

  console.log('\n24g. the raw drawer and the phone tabs');
  ok($('note').dataset.raw === 'shown' && $('note-raw-toggle').textContent === 'Hide', 'the drawer starts open');
  $('note-raw-toggle').fire('click');
  ok($('note').dataset.raw === 'hidden' && $('note-raw-toggle').textContent === 'Show', 'Hide collapses it');
  $('note-tab-raw').fire('click');
  ok($('note').dataset.raw === 'shown', 'the Raw tab opens it again');
  ok($('note-tab-raw').classList.contains('is-active') &&
     !$('note-tab-note').classList.contains('is-active'), 'one active tab at a time');
  ok($('note').classList.contains('tab-raw'), 'the article carries the tab class');
  $('note-tab-note').fire('click');
  ok($('note').dataset.raw === 'hidden' && $('note').classList.contains('tab-note'), 'back to the note pane');

  console.log('\n24h. versions: labels, preview, restore');
  await G.loadNote('n1');
  const labels = $('version-select').children.map(o => o.textContent);
  ok(labels.length === 2 && labels[0].indexOf('v2 \u00b7 revise: "shorter" \u00b7 ') === 0,
     'newest first, with the op and the instruction', labels);
  ok(labels[1].indexOf('v1 \u00b7 original \u00b7 ') === 0, 'v1 reads as the original', labels);
  ok($('version-select').value === '2', 'the current version is selected');
  await G.showVersion(1);
  ok($('note-editor').value === '# A note\n\nv1 body', 'the old version is previewed', $('note-editor').value);
  ok($('note-editor').readOnly === true && $('note-save').disabled === true, 'a preview is read-only');
  ok($('version-restore').disabled === false, 'restore is offered');
  calls.length = 0;
  await G.restoreVersion();
  ok(!!calls.find(c => c.url === '/api/notes/n1/restore'), 'POST restore');
  ok(!!calls.find(c => c.url === '/api/notes'), 'the sidebar is refreshed after a write');
  ok($('note-editor').readOnly === false && $('note-editor').value === '# A note\n\nthe body',
     'the editor is live again, on the current version');

  console.log('\n24i. save and revise both write a version and refresh the list');
  $('note-editor').value = '# A note\n\nedited';
  $('note-editor').fire('input');
  ok($('note-save').disabled === false, 'dirty -> Save enabled');
  calls.length = 0;
  await G.saveNote();
  const put = calls.find(c => c.url === '/api/notes/n1/note');
  ok(!!put && put.method === 'PUT' && JSON.parse(put.body).text === '# A note\n\nedited', 'PUT the editor');
  ok(/^saved \d\d:\d\d \u00b7 v3$/.test($('note-save-status').textContent),
     'the design\u2019s inline confirmation', $('note-save-status').textContent);
  ok($('note-save').disabled === true, 'and nothing is pending afterwards');

  $('revise-instructions').value = 'shorter';
  $('revise-instructions').fire('input');
  ok($('revise').disabled === false, 'an instruction enables Revise');
  calls.length = 0;
  await G.reviseNote();
  const rev = calls.find(c => c.url === '/api/notes/n1/revise');
  ok(!!rev && JSON.parse(rev.body).instructions === 'shorter', 'POST revise with the instruction');
  ok(/done \u2014 v3/.test($('process-status').textContent), 'the new version is reported', $('process-status').textContent);
  ok($('revise-instructions').value === '' && $('revise').disabled === true, 'the instruction box is cleared');

  console.log('\n24j. New note clears the stage; a running take keeps it');
  G.newNote();
  ok(state() === 'idle', 'back to the idle stage', state());
  ok($('note-editor').value === '' && $('note-title').textContent === '', 'nothing of the note is left');
  ok($('timer').textContent === '0:00' && $('big-timer').textContent === '0:00', 'the timer is back to zero');
  await G.startRecording();
  G.newNote();
  ok(state() === 'recording', 'New note is ignored while a take runs', state());
  rows()[0].fire('click');
  await sleep(20);
  ok(state() === 'recording', 'and so is a history row');
  await G.stopLive();
  ok(state() === 'note', 'the take still ends on its note', state());

  console.log('\n24k. daemon health: polled, and honest when it stops answering');
  const health = routes['/health'];
  delete routes['/health'];
  await G.checkHealth();
  ok($('app').dataset.daemon === 'down', 'the shell is marked down');
  ok($('daemon-info').textContent === 'daemon unreachable', 'and the sidebar says so', $('daemon-info').textContent);
  ok($('record').disabled === true, 'Record is off while it is down');
  calls.length = 0;
  await G.startRecording();
  ok(calls.length === 0, 'and starts nothing', calls.length);
  routes['/health'] = health;
  await G.checkHealth();
  ok($('app').dataset.daemon === undefined, 'recovery clears it');
  ok(/large-v3-turbo on cuda/.test($('daemon-info').textContent), 'the health line is back', $('daemon-info').textContent);
  ok($('record').disabled === false, 'Record is back');

  console.log('\n24k2. a warming daemon: the strip says so and Record stays on');
  routes['/health'] = async () => ({ body: { version: '0.6.0', whisper_model: 'large-v3-turbo',
                                             device: 'cpu', warm: false, ollama: 'starting' } });
  await G.checkHealth();
  ok($('daemon-info').textContent === 'warming large-v3-turbo …', 'the strip names the model', $('daemon-info').textContent);
  ok($('app').dataset.daemon === undefined, 'a warming daemon is not a down daemon');
  ok($('record').disabled === false, 'Record stays enabled while warming');
  routes['/health'] = async () => ({ body: { version: '0.6.0', whisper_model: 'large-v3-turbo',
                                             device: 'cuda', warm: true, ollama: 'starting' } });
  await G.checkHealth();
  ok(/on cuda · ollama starting$/.test($('daemon-info').textContent), 'then Whisper lands, Ollama still loading', $('daemon-info').textContent);
  routes['/health'] = async () => ({ body: { version: '0.6.0', whisper_model: 'large-v3-turbo',
                                             device: 'cpu', warm: false, ollama: 'skipped',
                                             warm_error: 'no such model: tiny-typo' } });
  await G.checkHealth();
  ok($('daemon-info').textContent === 'whisper failed: no such model: tiny-typo',
     'a load that failed says so instead of warming forever', $('daemon-info').textContent);
  routes['/health'] = health;
  await G.checkHealth();
  ok($('daemon-info').textContent === 'vnote 0.5.0 · large-v3-turbo on cuda', 'and a fully warm daemon reads plainly', $('daemon-info').textContent);

  console.log('\n24l. settings rows and the vocabulary editor');
  G.renderSettings([
    { key: 'default_mode', env: 'VNOTE_MODE', value: 'edit', kind: 'choice',
      choices: ['light', 'edit'], source: 'file', description: 'the default mode', editable: true },
    { key: 'whisper_model', env: 'VNOTE_WHISPER_MODEL', value: 'large-v3-turbo', kind: 'str',
      source: 'env', description: 'loaded at start-up', editable: false }
  ]);
  const trs = $('settings-table').children;
  ok(trs.length === 2, 'one row per setting', trs.length);
  ok(trs[0].children[0].textContent === 'Default modeVNOTE_MODE', 'name over the env var', trs[0].children[0].textContent);
  ok(trs[0].children[2].children[0].tagName === 'select', 'an editable choice is a select');
  ok(trs[1].children[2].textContent.indexOf('restart the daemon') !== -1,
     'a start-up setting says what to do instead', trs[1].children[2].textContent);
  ok(trs[1].children[3].textContent === 'env', 'the source badge');
  ok(G.changedSettings() === null, 'nothing changed -> nothing to save');
  trs[0].children[2].children[0].value = 'light';
  eq(G.changedSettings(), { default_mode: 'light' }, 'only the changed, editable rows are sent');
  G.setView('settings');
  await sleep(20);
  ok($('app').dataset.view === 'settings', 'the settings view is selected');
  ok($('vocab').value === 'vnote\nollama', 'the vocabulary loads on the first visit', $('vocab').value);
  G.setView('note');

  console.log('\n24m. a held recording survives the next Record');
  routes['/stream/finish'] = async () => ({ status: 500, body: { error: 'RuntimeError: gpu' } });
  await G.startRecording();
  feed([50, 51]);
  await sleep(30);
  await G.stopLive();
  routes = baseRoutes();
  ok(!!G.lastUpload && !!G.lastUpload.pcm, 'the take is held for Retry');
  ok($('retry-wrap').hidden === false, 'the banner is up');
  ok($('view-note').dataset.retry === 'true', 'and the live text stays on the stage', $('view-note').dataset.retry);
  ok(liveMode() === 'on', 'which is what data-live says it is showing', liveMode());
  const held = G.lastUpload;

  const grantMic = navigator.mediaDevices.getUserMedia;
  navigator.mediaDevices.getUserMedia = async () => { throw new Error('NotAllowedError'); };
  await G.startRecording();
  navigator.mediaDevices.getUserMedia = grantMic;
  ok($('mic-help').hidden === false, 'the microphone banner is up');
  ok(G.lastUpload === held, 'a refused microphone does not drop the held recording');
  ok($('retry-wrap').hidden === false, 'the banner is back with the stage');
  ok($('view-note').dataset.retry === 'true', 'and so is the live text');

  console.log('\n24n. New note asks before throwing it away');
  confirmAnswer = false;
  G.newNote();
  ok(G.lastUpload === held, 'declining keeps the recording');
  ok($('retry-wrap').hidden === false && $('mic-help').hidden === false, 'and the stage with it');
  confirmAnswer = true;
  G.newNote();
  ok(G.lastUpload === null, 'accepting drops it');
  ok($('retry-wrap').hidden === true && $('retry-detail').textContent === '', 'the banner is cleared');
  ok($('view-note').dataset.retry === undefined, 'and the live text with it', $('view-note').dataset.retry);
  ok($('mic-help').hidden === true, 'New note still clears the microphone banner');

  console.log('\n24o. a daemon that vanishes mid-take leaves Stop alone');
  const healthRoute = routes['/health'];
  await G.startRecording();
  delete routes['/health'];
  await G.checkHealth();
  ok($('app').dataset.daemon === undefined, 'the shell is not marked down while recording', $('app').dataset.daemon);
  ok($('stop').disabled === false, 'so Stop keeps working');
  routes['/health'] = healthRoute;
  await G.stopLive();
  delete routes['/health'];
  await G.checkHealth();
  ok($('app').dataset.daemon === 'down', 'the next poll after the take says so');
  routes['/health'] = healthRoute;
  await G.checkHealth();
  ok($('app').dataset.daemon === undefined, 'and recovery clears it');

  console.log('\n24p. the language picker: settings, the last pick, then the common ones');
  const savedPicks = store['vnote.picks'];
  const SETTINGS_SV = [
    { key: 'language', value: 'sv', kind: 'str' },
    { key: 'notes_dir', value: '/home/x/voice-notes', kind: 'path' },
    { key: 'backend', value: 'ollama', choices: ['ollama'], kind: 'choice' }
  ];
  delete store['vnote.picks'];
  G.applySettingsToPicks(SETTINGS_SV);
  let langs = $('pick-language').children.map(o => [o.value, o.textContent]);
  eq(langs[0], ['sv', 'sv (settings)'], 'the configured language leads, and says where it comes from');
  eq(langs[1], ['en', 'en'], 'then the common ones');
  eq(langs[langs.length - 1], ['', 'auto'], 'auto last');
  ok(langs.filter(l => l[0] === 'sv').length === 1, 'the configured language is not repeated');
  ok(langs.length === 14, '1 configured + 13 common (sv deduped) + auto', langs.length);
  ok($('pick-language').value === 'sv', 'and it is the selection', $('pick-language').value);
  ok($('notes-empty-dir').textContent === '/home/x/voice-notes', 'the empty state names the notes folder');

  store['vnote.picks'] = JSON.stringify({ mode: 'edit', backend: 'ollama', language: 'nb', live: true });
  G.applySettingsToPicks(SETTINGS_SV);
  langs = $('pick-language').children.map(o => [o.value, o.textContent]);
  eq(langs.map(l => l[0]), ['sv', 'nb', 'en', 'de', 'fr', 'es', 'it', 'pt', 'nl', 'pl', 'ru', 'ja', 'zh', 'ko', ''],
     'a remembered pick outside the common list is offered too');
  ok($('pick-language').value === 'nb', 'and localStorage still wins the selection', $('pick-language').value);

  store['vnote.picks'] = JSON.stringify({ mode: 'edit', backend: 'ollama', language: '', live: true });
  G.applySettingsToPicks(SETTINGS_SV);
  ok($('pick-language').value === '', 'a remembered auto stays auto', $('pick-language').value);
  delete store['vnote.picks'];
  G.applySettingsToPicks([{ key: 'language', value: '', kind: 'str' }]);
  eq($('pick-language').children.map(o => o.value), ['en', 'de', 'fr', 'es', 'it', 'pt', 'nl', 'pl', 'sv', 'ru', 'ja', 'zh', 'ko', ''],
     'no configured language: the common list, then auto');
  ok($('pick-language').value === '', 'and the picker is on auto', $('pick-language').value);
  ok($('notes-empty-dir').textContent === '/home/x/voice-notes',
     'a daemon that says nothing about notes_dir leaves the sentence alone');
  if (savedPicks === undefined) delete store['vnote.picks'];
  else store['vnote.picks'] = savedPicks;

  console.log('\n24q. settings that will not load still pick the daemon default mode');
  $('pick-mode').value = 'summary';
  routes['/api/settings'] = async () => ({ status: 500, body: { error: 'nope' } });
  await G.boot();
  ok($('pick-mode').value === 'edit', 'edit, not the first option', $('pick-mode').value);
  ok($('settings-status').textContent.indexOf('nope') !== -1, 'and the settings view says why', $('settings-status').textContent);
  routes = baseRoutes();
  await G.boot();

  console.log('\n24r. notes made outside this page are picked up');
  await G.loadNote('n1');
  const listCalls = () => calls.filter(c => c.url === '/api/notes').length;
  calls.length = 0;
  for (let i = 0; i < 12; i++) await G.checkHealth();
  ok(listCalls() === 2, 'twice in 12 polls — every ~30 s', listCalls());
  ok(rows()[0].classList.contains('is-active'), 'and the open note keeps the row');
  $('note-editor').value = '# A note\n\nunsaved';
  $('note-editor').fire('input');
  calls.length = 0;
  for (let i = 0; i < 12; i++) await G.checkHealth();
  ok(listCalls() === 0, 'never over unsaved edits', listCalls());
  G.revertEditor();
  await G.startRecording();
  calls.length = 0;
  for (let i = 0; i < 12; i++) await G.checkHealth();
  ok(listCalls() === 0, 'nor during a take', listCalls());
  await G.stopLive();
  calls.length = 0;
  ok(Array.isArray(docOn.visibilitychange), 'a visibilitychange listener is registered');
  docOn.visibilitychange.forEach(f => f());
  await sleep(20);
  ok(listCalls() === 1, 'a tab brought back to the front refreshes once', listCalls());

  console.log('\n24s. what is selected is announced, and the title stays one plain line');
  ok(rows()[0].getAttribute('aria-current') === 'true', 'the open note is the current row');
  ok(rows()[1].getAttribute('aria-current') === null, 'and it is the only one', rows()[1].getAttribute('aria-current'));
  G.pickNoteTab('raw');
  ok($('note-tab-raw').getAttribute('aria-selected') === 'true' &&
     $('note-tab-note').getAttribute('aria-selected') === 'false', 'the phone tabs report the selection');
  G.pickNoteTab('note');
  $('sidebar-toggle').fire('click');
  ok($('app').dataset.sidebar === 'collapsed', 'the toggle collapses the sidebar');
  ok($('sidebar-toggle').getAttribute('aria-expanded') === 'false' &&
     $('sidebar-toggle').getAttribute('aria-label') === 'Show sidebar',
     'and says what it would do next', $('sidebar-toggle').getAttribute('aria-label'));
  $('sidebar-toggle').fire('click');
  ok($('app').dataset.sidebar === undefined &&
     $('sidebar-toggle').getAttribute('aria-expanded') === 'true' &&
     $('sidebar-toggle').getAttribute('aria-label') === 'Hide sidebar', 'and back again');

  await G.loadNote('n1');
  let stopped = 0;
  let blurred = 0;
  $('note-title').blur = () => { blurred++; };
  $('note-title').fire('keydown', { key: 'Enter', preventDefault() { stopped++; } });
  ok(stopped === 1 && blurred === 1, 'Enter commits the title instead of opening a <div>', { stopped, blurred });
  $('note-title').fire('keydown', { key: 'a', preventDefault() { stopped++; } });
  ok(stopped === 1 && blurred === 1, 'any other key is left alone');
  $('note-title').fire('paste', {
    clipboardData: { getData: () => 'Pasted\n  title ' },
    preventDefault() { stopped++; }
  });
  ok(stopped === 2, 'a paste is intercepted');
  ok($('note-title').textContent === 'A notePasted title', 'and arrives as one plain line', $('note-title').textContent);
  ok($('note-editor').value === '# A notePasted title\n\nthe body', 'the heading followed it', $('note-editor').value);
  await G.loadNote('n1');

  console.log('\n24t. no empty path chip when a note cannot be opened');
  ok($('note-path').hidden === false && $('note-path').textContent === '/x/voice-notes/n1', 'the path is a chip');
  routes['/api/notes/n1'] = async () => ({ status: 404, body: { error: 'gone' } });
  await G.loadNote('n1');
  ok($('note-path').hidden === true, 'a note that would not open shows no empty chip');
  ok($('note-title').textContent === 'could not open n1', 'and says so where the title was');
  routes = baseRoutes();
  await G.loadNote('n1');
  ok($('note-path').hidden === false, 'the chip is back with the note');

  /* ------------------------------------------------------ the worklet itself */
  console.log('\n25. pcm-worklet resampling');
  const posted = [];
  function makeWorklet(rate) {
    const s = {
      sampleRate: rate,
      AudioWorkletProcessor: class {
        constructor() { this.port = { onmessage: null, postMessage: (m) => posted.push(m) }; }
      },
      registerProcessor: (name, cls) => { s._name = name; s._cls = cls; },
      Int16Array, Float32Array, Math, console
    };
    vm.createContext(s);
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'pcm-worklet.js'), 'utf8'), s, { filename: 'pcm-worklet.js' });
    return s;
  }
  const pcmOf = m => new Int16Array(m && m.type === 'flush' ? m.buffer : m);
  const allPcm = () => Int16Array.from(posted.flatMap(m => Array.from(pcmOf(m))));

  const wsand = makeWorklet(48000);
  ok(wsand._name === 'pcm-capture', 'registerProcessor("pcm-capture")');
  const proc = new wsand._cls();
  // 48 kHz -> 16 kHz: 4800 input samples (0.1 s) must yield ~1600 output samples
  const blocks = 4800 / 128 | 0;   // 37 whole blocks = 4736 samples
  for (let b = 0; b < blocks; b++) {
    const ch = new Float32Array(128);
    for (let i = 0; i < 128; i++) ch[i] = Math.sin(2 * Math.PI * 440 * (b * 128 + i) / 48000);
    proc.process([[ch]]);
  }
  proc.port.onmessage({ data: { type: 'flush' } });
  const total = posted.reduce((n, m) => n + pcmOf(m).length, 0);
  ok(Math.abs(total - 4736 / 3) <= 2, '4736 in @48k -> ~1579 out @16k', total);
  const all = allPcm();
  const lo = Math.min(...all), hi = Math.max(...all);
  ok(hi > 30000 && hi <= 32767 && lo >= -32768, 'amplitude preserved and clamped', { lo, hi });
  // a 440 Hz sine over 4736/48000 s crosses zero ~2 * 440 * 0.0987 = 87 times
  let crossings = 0;
  for (let i = 1; i < all.length; i++) if ((all[i - 1] < 0) !== (all[i] < 0)) crossings++;
  ok(Math.abs(crossings - 87) <= 3, 'frequency preserved (zero crossings ~87)', crossings);
  posted.length = 0;
  proc.port.onmessage({ data: { type: 'flush' } });
  ok(posted.length === 1 && posted[0] && posted[0].type === 'flush' && pcmOf(posted[0]).length === 0,
     'an empty flush still answers, tagged');

  console.log('\n26. 44.1 kHz: a fractional ratio stays continuous across blocks');
  posted.length = 0;
  const w441 = makeWorklet(44100);
  const p441 = new w441._cls();
  const N441 = 128 * 400;          // 51 200 samples, ~1.16 s
  for (let b = 0; b < 400; b++) {
    const ch = new Float32Array(128);
    for (let i = 0; i < 128; i++) ch[i] = 0.9 * Math.sin(2 * Math.PI * 50 * (b * 128 + i) / 44100);
    p441.process([[ch]]);
  }
  p441.port.onmessage({ data: { type: 'flush' } });
  const s441 = allPcm();
  const want441 = N441 / (44100 / 16000);
  ok(Math.abs(s441.length - want441) <= 2, '51 200 in @44.1k -> ~18 576 out @16k', { got: s441.length, want: Math.round(want441) });
  // a 50 Hz sine at 16 kHz moves at most 0.9 * 2π * 50/16000 ≈ 0.0177 full-scale per
  // sample (~580 in int16); a lost or repeated sample at a block boundary jumps further.
  let worst = 0, worstAt = -1;
  for (let i = 1; i < s441.length; i++) {
    const d = Math.abs(s441[i] - s441[i - 1]);
    if (d > worst) { worst = d; worstAt = i; }
  }
  ok(worst < 800, 'no discontinuity at any block boundary', { worst, worstAt });
  ok(posted.some(m => !(m && m.type === 'flush')), 'a full batch is posted untagged, as a bare buffer');
  let crossings441 = 0;
  for (let i = 1; i < s441.length; i++) if ((s441[i - 1] < 0) !== (s441[i] < 0)) crossings441++;
  ok(Math.abs(crossings441 - 2 * 50 * (N441 / 44100)) <= 2, 'frequency preserved (~116 crossings)', crossings441);

  console.log('\n27. passthrough and downmix');
  posted.length = 0;
  const w16 = makeWorklet(16000);
  const p16 = new w16._cls();
  for (let b = 0; b < 10; b++) p16.process([[new Float32Array(128).fill(0.5)]]);
  p16.port.onmessage({ data: { type: 'flush' } });
  const total2 = posted.reduce((n, m) => n + pcmOf(m).length, 0);
  ok(Math.abs(total2 - 1280) <= 2, 'passthrough at 16 kHz keeps the sample count', total2);

  posted.length = 0;
  const p16b = new (makeWorklet(16000))._cls();
  const left = new Float32Array(128).fill(0.5);
  const right = new Float32Array(128).fill(-0.1);
  for (let b = 0; b < 4; b++) p16b.process([[left, right]]);
  p16b.port.onmessage({ data: { type: 'flush' } });
  const mixed = allPcm();
  const mid = mixed[Math.floor(mixed.length / 2)];
  ok(Math.abs(mid - Math.round(0.2 * 32768)) <= 2, 'stereo is averaged, not truncated to channel 0', mid);

  console.log('\n' + (failures ? failures + ' FAILURES' : 'all checks passed'));
  process.exit(failures ? 1 : 0);
}()).catch(e => { console.error('harness error:', e); process.exit(2); });
