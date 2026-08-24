/* vnote web UI — vanilla JS, no build step, no external requests.
 *
 * Contract between markup (index.html, replaceable) and behaviour (this file).
 * Every id below must exist exactly once in the document.
 *
 *   Tabs / chrome
 *     #tab-record #tab-notes #tab-settings   buttons; toggle the views below
 *     #view-record #view-notes #view-settings sections; one visible at a time
 *     #daemon-info                            one line from /health
 *
 *   Record view
 *     #record #pause #stop      transport buttons (#pause label: Pause/Resume,
 *                               #stop label: Stop/Cancel while starting)
 *     #timer                    "0:00" of recorded time (frozen while paused)
 *     #rec-status               ready / recording / paused / processing / error
 *     #retry                    re-send the last recording after a failed upload
 *                               (hidden otherwise, and during a recording)
 *     #pick-mode                select: light edit summary dictation raw
 *     #pick-backend             select, filled from the `backend` setting
 *     #pick-language            text input, placeholder "auto"
 *     #live-toggle              checkbox: stream PCM and show the transcript live
 *     #live                     container, hidden unless a live recording is
 *                               running or has just finished
 *     #live-committed           the settled text (one <p> per paragraph)
 *     #live-tail                <span>: the still-changing tail
 *     #live-copy #live-copy-status
 *     #result                   container, hidden until a note exists
 *     #result-title #result-text (readonly textarea) #result-warning
 *     #copy #copy-status #result-open
 *
 *   Notes view
 *     #notes-refresh #notes-list
 *     #note-detail              hidden until a note is opened
 *     #note-title #note-meta #note-audio (<audio>)
 *     #note-editor              editable textarea holding the note's Markdown
 *     #note-save #note-save-status    PUT the editor as a new version
 *     #note-copy #note-copy-status
 *     #regenerate-mode          select: edit light summary dictation
 *     #regenerate               re-run cleanup from the raw transcript
 *     #revise-instructions      one-line instruction for #revise (and #regenerate)
 *     #revise                   apply that instruction to the current note
 *     #process-status           shared status for #regenerate and #revise
 *     #note-versions            container; hidden when the note has no versions
 *     #version-select #version-restore   version history, newest first
 *     #note-folder              container; hidden when the note has no path
 *     #note-path #note-path-copy #note-reveal #note-path-status
 *     #note-continue            disabled affordance ("Continue recording (coming)")
 *     #note-transcript          <details> containing a <pre>
 *
 *   Settings view
 *     #settings-table           <table>; this file fills its <tbody>
 *     #settings-save #settings-status
 *     #vocab #vocab-save #vocab-status
 */

'use strict';

/* ------------------------------------------------------------------ helpers */

function $(id) { return document.getElementById(id); }

var LS_TAB = 'vnote.tab';
var LS_PICKS = 'vnote.picks';

function lsGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}
function lsSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
}

function errText(e) {
  if (!e) return 'unknown error';
  return e.message ? e.message : String(e);
}

/* Every request goes through here: checks res.ok, parses JSON, surfaces
 * {"error": "..."} from the server as an Error with that message. */
async function api(url, options) {
  var res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    throw new Error('cannot reach the daemon (' + errText(e) + ')');
  }
  var data = null;
  var ctype = res.headers.get('content-type') || '';
  if (ctype.indexOf('json') !== -1) {
    try { data = await res.json(); } catch (e) { data = null; }
  }
  if (!res.ok) {
    var err;
    if (data && data.error) {
      err = new Error(data.error);
      err.data = data;   // extra fields travel with the message (e.g. audio_kept)
    } else {
      err = new Error('HTTP ' + res.status + ' ' + (res.statusText || ''));
    }
    err.status = res.status;   // callers that treat 404 as terminal look at this
    throw err;
  }
  if (data === null) throw new Error('unexpected non-JSON response from ' + url);
  return data;
}

function getJSON(url) { return api(url, { method: 'GET' }); }

function putJSON(url, body) {
  return api(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

function postJSON(url, body) {
  return api(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/* Format helpers ---------------------------------------------------------- */

function pad2(n) { return (n < 10 ? '0' : '') + n; }

/* ISO string -> local "YYYY-MM-DD HH:MM" */
function fmtCreated(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
    ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/* ISO string -> local "HH:MM" */
function fmtTime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/* seconds -> "m:ss" */
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return '';
  var total = Math.max(0, Math.round(Number(seconds)));
  return Math.floor(total / 60) + ':' + pad2(total % 60);
}

/* Transient status text ("copied", "saved", …) */
var flashTimers = {};
function flash(el, text, ms) {
  if (!el) return;
  el.textContent = text;
  if (flashTimers[el.id]) window.clearTimeout(flashTimers[el.id]);
  flashTimers[el.id] = window.setTimeout(function () {
    el.textContent = '';
    flashTimers[el.id] = null;
  }, ms || 2000);
}

/* `stillWanted`, when given, is re-checked after the clipboard call: callers
 * whose status line belongs to a note drop the flash once that note is gone. */
async function copyText(text, statusEl, stillWanted) {
  var msg;
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('no clipboard api');
    await navigator.clipboard.writeText(text);
    msg = 'copied';
  } catch (e) {
    msg = copyFallback(text) ? 'copied' : 'copy failed';
  }
  if (stillWanted && !stillWanted()) return;
  flash(statusEl, msg);
}

/* Fallback: put the text in a textarea, select it, execCommand('copy'). */
function copyFallback(text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', 'readonly');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  document.body.appendChild(ta);
  var ok = false;
  try {
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

/* --------------------------------------------------------------------- tabs */

var TABS = [
  { name: 'record', tab: 'tab-record', view: 'view-record' },
  { name: 'notes', tab: 'tab-notes', view: 'view-notes' },
  { name: 'settings', tab: 'tab-settings', view: 'view-settings' }
];

var notesLoaded = false;
var vocabLoaded = false;
var currentTab = null;

function showTab(name) {
  var known = false;
  TABS.forEach(function (t) { if (t.name === name) known = true; });
  if (!known) name = 'record';

  TABS.forEach(function (t) {
    var active = t.name === name;
    var tabEl = $(t.tab);
    var viewEl = $(t.view);
    if (tabEl) {
      tabEl.classList.toggle('active', active);
      tabEl.setAttribute('aria-selected', active ? 'true' : 'false');
    }
    if (viewEl) viewEl.hidden = !active;
  });
  lsSet(LS_TAB, name);
  currentTab = name;

  if (name === 'notes' && !notesLoaded) {
    notesLoaded = true;
    loadNotes();
  }
  if (name === 'settings' && !vocabLoaded) {
    vocabLoaded = true;
    loadVocab();
  }
}

/* --------------------------------------------------------------------- boot */

var settingsRows = [];   // [{setting, control}]
var backendChoices = [];

function readPicks() {
  var raw = lsGet(LS_PICKS);
  if (!raw) return null;
  try {
    var obj = JSON.parse(raw);
    return obj && typeof obj === 'object' ? obj : null;
  } catch (e) {
    return null;
  }
}

function savePicks() {
  lsSet(LS_PICKS, JSON.stringify({
    mode: $('pick-mode').value,
    backend: $('pick-backend').value,
    language: $('pick-language').value,
    live: $('live-toggle').checked
  }));
}

/* Set a <select> to `value` only when that option actually exists. */
function selectIfPresent(sel, value) {
  if (!sel || value === null || value === undefined || value === '') return;
  for (var i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === String(value)) {
      sel.value = String(value);
      return;
    }
  }
}

function findSetting(settings, key) {
  for (var i = 0; i < settings.length; i++) {
    if (settings[i].key === key) return settings[i];
  }
  return null;
}

function applySettingsToPicks(settings) {
  var backend = findSetting(settings, 'backend');
  var backendSel = $('pick-backend');
  backendSel.innerHTML = '';
  backendChoices = (backend && backend.choices) ? backend.choices.slice() : [];
  if (!backendChoices.length && backend && backend.value) backendChoices = [backend.value];
  backendChoices.forEach(function (choice) {
    var opt = document.createElement('option');
    opt.value = choice;
    opt.textContent = choice;
    backendSel.appendChild(opt);
  });
  if (backend) selectIfPresent(backendSel, backend.value);

  var mode = findSetting(settings, 'default_mode');
  if (mode) selectIfPresent($('pick-mode'), mode.value);

  var lang = findSetting(settings, 'language');
  if (lang && lang.value) $('pick-language').value = lang.value;

  // localStorage wins over the daemon's defaults.
  var picks = readPicks();
  if (picks) {
    selectIfPresent($('pick-mode'), picks.mode);
    selectIfPresent($('pick-backend'), picks.backend);
    if (typeof picks.language === 'string') $('pick-language').value = picks.language;
  }
}

async function boot() {
  var healthPromise = getJSON('/health');
  var settingsPromise = getJSON('/api/settings');

  try {
    var health = await healthPromise;
    $('daemon-info').textContent =
      'vnote ' + (health.version || '?') + ' · ' +
      (health.whisper_model || '?') + ' on ' + (health.device || '?');
  } catch (e) {
    $('daemon-info').textContent = 'daemon unreachable — ' + errText(e);
    $('daemon-info').classList.add('bad');
    $('record').dataset.blocked = '1';   // keeps setTransport() from re-enabling it
    $('record').disabled = true;
    $('rec-status').textContent = 'daemon unreachable';
  }

  try {
    var data = await settingsPromise;
    var settings = (data && data.settings) || [];
    applySettingsToPicks(settings);
    renderSettings(settings);
  } catch (e) {
    $('settings-status').textContent = errText(e);
    var tbody = $('settings-table').querySelector('tbody');
    tbody.innerHTML = '';
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = 'could not load settings: ' + errText(e);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

/* ---------------------------------------------------------------- recording */

var MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4'
];

var recorder = null;
var recStream = null;
var recChunks = [];
var recStarting = false;   // covers the whole start: getUserMedia, /stream/start, the worklet
var recCancelled = false;  // Stop pressed while still starting
/* What #retry would send: {blob, format} from the MediaRecorder path, or {pcm:
 * [Uint8Array, …]} — the live path's safety copy, turned into a WAV on the click.
 * null means there is nothing to retry and the button stays hidden. */
var lastUpload = null;
var recMime = '';
var recElapsedMs = 0;      // accumulated recorded time (excludes pauses)
var recSegmentStart = 0;   // performance.now() of the current running segment
var recTicker = null;
var lastNoteName = null;

/* Live capture: `live` is the running session (see startLive), null otherwise.
 * The rendered text outlives it so the pane stays copyable after Stop. */
var live = null;
var liveAvailable = false;
var liveParas = [];      // the paragraphs currently in #live-committed
var liveTail = '';       // the text currently in #live-tail
var liveFallbackReason = '';   // why this take is on MediaRecorder instead ('' = it isn't)

var LIVE_SAMPLE_RATE = 16000;         // what the worklet sends, and the Retry WAV's rate
var LIVE_PARAGRAPH_SILENCE_S = 2.0;   // stream.py:_PARAGRAPH_SILENCE_S
var LIVE_MIN_REQUEST_MS = 950;        // at most ~1 append/second
var LIVE_PING_MS = 30000;             // keepalive while paused
var LIVE_FLUSH_WAIT_MS = 300;         // how long Stop waits for the worklet's last batch
var LIVE_FIRST_PCM_MS = 3000;         // no PCM by then: say so, but keep recording
var LIVE_BACKOFF_MS = 1000;           // first pause after 3 failed appends (doubles, capped)
var LIVE_BACKOFF_MAX_MS = 8000;
var LIVE_STOP_TIMEOUT_MS = 10000;     // a final append that hangs must not hang Stop
var LIVE_MAX_BODY_BYTES = LIVE_SAMPLE_RATE * 2 * 30;   // 30 s of s16le mono per request

function liveSupported() {
  return typeof window.AudioWorklet !== 'undefined' && typeof window.AudioContext !== 'undefined';
}

/* The toggle is a pick like the others (remembered in localStorage), except that a
 * browser without AudioWorklet pins it off. Default on where it works. */
function initLiveToggle() {
  var el = $('live-toggle');
  liveAvailable = liveSupported();
  if (!liveAvailable) {
    el.checked = false;
    el.disabled = true;
    el.title = 'not supported in this browser';
    return;
  }
  var picks = readPicks();
  el.checked = (picks && typeof picks.live === 'boolean') ? picks.live : true;
}

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  for (var i = 0; i < MIME_CANDIDATES.length; i++) {
    try {
      if (MediaRecorder.isTypeSupported(MIME_CANDIDATES[i])) return MIME_CANDIDATES[i];
    } catch (e) { /* keep looking */ }
  }
  return '';
}

/* "audio/webm;codecs=opus" -> "webm" */
function formatFromMime(mime) {
  var m = String(mime || '').toLowerCase();
  if (m.indexOf('webm') !== -1) return 'webm';
  if (m.indexOf('ogg') !== -1) return 'ogg';
  if (m.indexOf('mp4') !== -1 || m.indexOf('m4a') !== -1 || m.indexOf('aac') !== -1) return 'mp4';
  if (m.indexOf('wav') !== -1) return 'wav';
  return 'webm';
}

/* True while audio is actually being captured (either path) — the timer's clock. */
function capturing() {
  if (live) return live.state === 'recording';
  return !!recorder && recorder.state === 'recording';
}

/* True while a recording exists at all, running or paused. */
function capturePaused() {
  if (live) return live.state === 'paused';
  return !!recorder && recorder.state === 'paused';
}

function recordedMs() {
  var ms = recElapsedMs;
  if (capturing()) ms += performance.now() - recSegmentStart;
  return ms;
}

function paintTimer() {
  $('timer').textContent = fmtDuration(recordedMs() / 1000);
}

function startTicker() {
  if (recTicker) return;
  recTicker = window.setInterval(paintTimer, 250);
}

function stopTicker() {
  if (recTicker) window.clearInterval(recTicker);
  recTicker = null;
}

/* States: ready, starting (microphone/session being opened — Stop cancels),
 * recording, paused, processing. */
function setTransport(state) {
  var recording = state === 'recording';
  var paused = state === 'paused';
  var busy = state === 'processing';
  var starting = state === 'starting';
  var active = recording || paused || busy || starting;

  $('record').disabled = active || $('record').dataset.blocked === '1';
  $('pause').disabled = !(recording || paused);
  $('stop').disabled = !(recording || paused || starting);
  $('pause').textContent = paused ? '▶ Resume' : '⏸ Pause';
  $('stop').textContent = starting ? '■ Cancel' : '■ Stop';

  // Retrying mid-take would upload one recording while another runs.
  $('retry').hidden = !lastUpload || active;
  $('retry').disabled = $('retry').hidden;

  ['pick-mode', 'pick-backend', 'pick-language'].forEach(function (id) {
    $(id).disabled = active;
  });
  $('live-toggle').disabled = active || !liveAvailable;

  document.body.classList.toggle('is-recording', recording);
  document.body.classList.toggle('is-paused', paused);
  document.body.classList.toggle('is-processing', busy);
}

function releaseStream() {
  if (recStream) {
    recStream.getTracks().forEach(function (t) {
      try { t.stop(); } catch (e) { /* already stopped */ }
    });
  }
  recStream = null;
}

function noMediaRecorder() {
  releaseStream();
  $('rec-status').textContent = 'this browser has no MediaRecorder — recording is not possible';
  setTransport('ready');
}

/* Stop pressed during the start: give the microphone back and stay ready. */
function startCancelled() {
  if (!recCancelled) return false;
  recCancelled = false;
  recStarting = false;
  releaseStream();
  $('rec-status').textContent = 'ready';
  setTransport('ready');
  return true;
}

async function startRecording() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $('rec-status').textContent =
      'Microphone access needs a secure context — open this page at ' +
      'http://localhost:8760 or http://127.0.0.1:8760';
    return;
  }
  var wantLive = liveAvailable && $('live-toggle').checked;
  if (!wantLive && typeof MediaRecorder === 'undefined') {
    $('rec-status').textContent = 'this browser has no MediaRecorder — recording is not possible';
    return;
  }

  // a second click must not start a second recorder — nor a second live session
  if (recorder || recStream || recStarting || live) return;
  recStarting = true;
  recCancelled = false;
  liveFallbackReason = '';
  setTransport('starting');   // Stop is live from here on: it cancels the start
  $('rec-status').textContent = 'requesting the microphone…';
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    recStarting = false;
    $('rec-status').textContent = 'microphone unavailable — ' + errText(e);
    setTransport('ready');
    return;
  }
  if (startCancelled()) return;

  resetLivePane(wantLive);
  if (wantLive) {
    var started = await startLive();
    recStarting = false;
    if (started) {
      if (recCancelled) { recCancelled = false; cancelLive(); }
      return;
    }
    resetLivePane(false);      // this take has no live text: don't show an empty pane
    if (startCancelled()) return;
    if (typeof MediaRecorder === 'undefined') return noMediaRecorder();
    // startLive() left the reason in liveFallbackReason; MediaRecorder takes over
  }
  recStarting = false;
  startMediaRecorder();
}

function startMediaRecorder() {
  var mime = pickMimeType();
  try {
    recorder = mime ? new MediaRecorder(recStream, { mimeType: mime })
                    : new MediaRecorder(recStream);
  } catch (e) {
    releaseStream();
    $('rec-status').textContent = 'could not start the recorder — ' + errText(e);
    setTransport('ready');
    return;
  }
  recMime = recorder.mimeType || mime || '';

  recChunks = [];
  recElapsedMs = 0;
  recSegmentStart = performance.now();

  recorder.ondataavailable = function (ev) {
    if (ev.data && ev.data.size > 0) recChunks.push(ev.data);
  };
  recorder.onerror = function (ev) {
    var err = (ev && ev.error) ? ev.error : ev;
    stopTicker();
    releaseStream();
    recorder = null;
    $('rec-status').textContent = 'recording error — ' + errText(err);
    setTransport('ready');
  };
  recorder.onstop = function () {
    stopTicker();
    releaseStream();
    var mimeForBlob = recMime || 'audio/webm';
    var blob = new Blob(recChunks, { type: mimeForBlob });
    recChunks = [];
    recorder = null;
    paintTimer();
    uploadRecording(blob, formatFromMime(mimeForBlob));
  };

  try {
    recorder.start(1000);
  } catch (e) {
    releaseStream();
    recorder = null;
    $('rec-status').textContent = 'could not start the recorder — ' + errText(e);
    setTransport('ready');
    return;
  }

  paintTimer();
  startTicker();
  setTransport('recording');
  $('rec-status').textContent = recordingStatus();
}

/* Don't paper over a live-transcript failure: while a fallback take runs, the
 * status line keeps saying why the live pane is missing. */
function recordingStatus() {
  return liveFallbackReason
    ? 'recording — live transcript unavailable (' + liveFallbackReason + ')'
    : 'recording';
}

function togglePause() {
  if (live) return toggleLivePause();
  if (!recorder) return;
  if (recorder.state === 'recording') {
    recElapsedMs += performance.now() - recSegmentStart;
    try { recorder.pause(); } catch (e) { /* unsupported */ }
    stopTicker();
    paintTimer();
    setTransport('paused');
    $('rec-status').textContent = 'paused';
  } else if (recorder.state === 'paused') {
    recSegmentStart = performance.now();
    try { recorder.resume(); } catch (e) { /* unsupported */ }
    startTicker();
    setTransport('recording');
    $('rec-status').textContent = recordingStatus();
  }
}

function stopRecording() {
  if (recStarting) {          // nothing to stop yet: startRecording() unwinds instead
    recCancelled = true;
    $('rec-status').textContent = 'cancelling…';
    return;
  }
  if (live) return stopLive();
  if (!recorder) return;
  if (recorder.state === 'recording') recElapsedMs += performance.now() - recSegmentStart;
  stopTicker();
  setTransport('processing');
  $('rec-status').textContent = 'finishing the recording…';
  try {
    recorder.stop();   // the rest happens in recorder.onstop
  } catch (e) {
    releaseStream();
    recorder = null;
    $('rec-status').textContent = 'could not stop the recorder — ' + errText(e);
    setTransport('ready');
  }
}

function noteQuery(format) {
  var mode = $('pick-mode').value;
  var backend = $('pick-backend').value;
  var language = $('pick-language').value.trim();
  var params = [];
  function add(k, v) {
    if (v === null || v === undefined || v === '') return;
    params.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
  }
  add('format', format);
  if (mode === 'raw') {
    add('raw', '1');           // raw => no LLM, no mode
  } else {
    add('mode', mode);
    add('backend', backend);
  }
  add('language', language || 'auto');  // blank field = auto-detect, even if a language is saved in settings
  return params.length ? '?' + params.join('&') : '';
}

async function uploadRecording(blob, format) {
  if (!blob || blob.size === 0) {
    $('rec-status').textContent = 'nothing was recorded';
    setTransport('ready');
    return;
  }

  setTransport('processing');
  $('rec-status').textContent =
    'processing… transcribing, then cleaning — this can take a minute; no progress is reported';

  try {
    var data = await api('/api/note' + noteQuery(format), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob
    });
    lastUpload = null;
    showResult(data);
    $('rec-status').textContent = 'ready';
  } catch (e) {
    lastUpload = { blob: blob, format: format };  // the browser holds the only copy until the daemon has it
    $('rec-status').textContent = errText(e) + ' — the recording is still here: Retry upload, or Record to start over';
  } finally {
    setTransport('ready');   // and with it #retry, from lastUpload
  }
}

function showResult(data) {
  lastNoteName = data.name || null;
  $('result-title').textContent = data.title || data.name || 'note';
  $('result-text').value = data.note || data.transcript || '';
  var warn = $('result-warning');
  if (data.cleanup_error) {
    warn.textContent = 'cleanup failed — this is the raw transcript: ' + data.cleanup_error;
    warn.hidden = false;
  } else {
    warn.textContent = '';
    warn.hidden = true;
  }
  $('result-open').disabled = !lastNoteName;
  $('result').hidden = false;
  notesLoaded = false;   // the list is stale now
}

/* ---------------------------------------------------------- live transcript
 *
 * The optional path: an AudioWorklet turns the microphone into s16le 16 kHz PCM,
 * an uploader loop POSTs it to /stream/append (one request in flight, at most one
 * a second, coalescing whatever queued meanwhile) and every reply repaints the
 * pane. Stop is /stream/finish?note=1: the daemon transcribes what it has.
 *
 * This tab keeps every PCM chunk of the take as well (`session.all`, ~1.9 MB a
 * minute) so that a daemon that never received the tail — or lost the session, or
 * failed the finish without keeping the audio — is not the end of the recording:
 * #retry wraps the safety copy in a WAV header and uploads it as a normal note.
 */

/* Clear the pane; `visible` keeps it on screen for a live recording. */
function resetLivePane(visible) {
  liveParas = [];
  liveTail = '';
  $('live-committed').textContent = '';
  $('live-tail').textContent = '';
  $('live').hidden = !visible;
}

/* The scroller around the text; no id of its own. */
function livePane() { return $('live-committed').parentNode; }

function liveAtBottom() {
  var pane = livePane();
  if (!pane || typeof pane.scrollHeight !== 'number') return true;
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight < 24;
}

/* The committed segments as paragraphs — the join rule of stream.py's
 * _committed_text(): a pause of 2 s or more after a segment starts a new one. */
function committedParagraphs(committed) {
  var text = '';
  var gap = '';
  (committed || []).forEach(function (seg) {
    if (!seg || !seg.text) return;   // a silence-only segment carries no words
    if (text) text += gap;
    text += seg.text;
    gap = (seg.trailing_silence_s >= LIVE_PARAGRAPH_SILENCE_S) ? '\n\n' : ' ';
  });
  return text ? text.split('\n\n') : [];
}

/* Repaint from an /stream/append reply. Settled paragraphs are stable DOM: only
 * the ones that actually changed are replaced, so a selection over the text above
 * them survives every update. */
function renderLive(data) {
  $('live').hidden = false;
  var atBottom = liveAtBottom();

  var paras = committedParagraphs(data && data.committed);
  var box = $('live-committed');
  var first = 0;
  while (first < paras.length && first < liveParas.length && paras[first] === liveParas[first]) {
    first += 1;
  }
  if (first < liveParas.length || paras.length !== liveParas.length) {
    while (box.children.length > first) box.removeChild(box.lastChild);
    for (var i = first; i < paras.length; i++) {
      var p = document.createElement('p');
      p.textContent = paras[i];
      box.appendChild(p);
    }
  }
  liveParas = paras;

  var tail = (data && data.tail) ? data.tail : '';
  if (tail !== liveTail) {
    liveTail = tail;
    $('live-tail').textContent = tail;
  }

  if (atBottom) {
    var pane = livePane();
    if (pane && typeof pane.scrollHeight === 'number') pane.scrollTop = pane.scrollHeight;
  }
}

function liveText() {
  var committed = liveParas.join('\n\n');
  if (!liveTail) return committed;
  return committed ? committed + ' ' + liveTail : liveTail;
}

/* Blank language field = auto-detect, as in noteQuery(). */
function liveLanguage() {
  var language = $('pick-language').value.trim();
  return language ? language : null;
}

function finishQuery(sid) {
  var mode = $('pick-mode').value;
  var backend = $('pick-backend').value;
  var language = $('pick-language').value.trim();
  var params = ['sid=' + encodeURIComponent(sid), 'note=1'];
  function add(k, v) {
    if (v === null || v === undefined || v === '') return;
    params.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
  }
  if (mode === 'raw') {
    add('raw', '1');           // raw => no LLM, no mode
  } else {
    add('mode', mode);
    add('backend', backend);
  }
  add('language', language || 'auto');
  return '?' + params.join('&');
}

/* Returns true once PCM is flowing; false means "fall back to MediaRecorder",
 * with the reason in liveFallbackReason (and in #rec-status meanwhile). */
function liveUnavailable(reason) {
  liveFallbackReason = reason;
  $('rec-status').textContent =
    'live transcript unavailable — recording without it (' + reason + ')';
  return false;
}

/* ctx.suspend()/resume()/close() answer with a promise that can reject (a context
 * torn down under us). Nothing here can act on that; swallow it so it is not an
 * unhandled rejection. */
function audioCall(ctx, name) {
  try {
    var p = ctx[name]();
    if (p && typeof p.catch === 'function') {
      return p.catch(function () { /* the state stays whatever it was */ });
    }
  } catch (e) { /* older browsers throw synchronously */ }
  return Promise.resolve();
}

/* `recStream` is already open. */
async function startLive() {
  var sid;
  try {
    var started = await postJSON('/stream/start', { language: liveLanguage() });
    sid = started && started.session_id;
    if (!sid) throw new Error('no session id');
  } catch (e) {
    return liveUnavailable(errText(e));
  }

  var ctx = null;
  try {
    ctx = new AudioContext({ sampleRate: 16000 });
  } catch (e) {
    try { ctx = new AudioContext(); } catch (e2) { ctx = null; }   // the worklet resamples anyway
  }
  var session = null;
  try {
    if (!ctx) throw new Error('no AudioContext');
    await ctx.audioWorklet.addModule('/static/pcm-worklet.js');
    var src = ctx.createMediaStreamSource(recStream);
    var node = new AudioWorkletNode(ctx, 'pcm-capture');
    // Chrome only pulls a worklet that reaches the destination; a muted gain keeps
    // the loop turning without playing the microphone back.
    var gain = ctx.createGain();
    gain.gain.value = 0;
    src.connect(node);
    node.connect(gain);
    gain.connect(ctx.destination);

    // An autoplay-blocked context is suspended: no PCM would ever arrive.
    if (ctx.state !== 'running') await audioCall(ctx, 'resume');
    if (ctx.state !== 'running') {
      throw new Error('the audio context stayed ' + (ctx.state || 'unusable'));
    }

    session = {
      sid: sid, ctx: ctx, src: src, node: node, gain: gain,
      state: 'recording',
      queue: [], all: [], sending: false, inflight: null, lastSend: performance.now(),
      seconds: 0,                      // audio the daemon has acknowledged
      failures: 0, warned: false, retryAt: 0,
      gotPcm: false, silentWarned: false, watchdog: null,
      stopping: false, flushWaiter: null,
      pumpTimer: null, pingTimer: null
    };
    node.port.onmessage = function (ev) { onLivePcm(session, ev.data); };
  } catch (e) {
    if (ctx) audioCall(ctx, 'close');
    cancelSession(sid);
    return liveUnavailable(errText(e));
  }

  live = session;
  session.pumpTimer = window.setInterval(function () { pumpLive(session); }, 200);
  session.watchdog = window.setTimeout(function () { liveNoAudio(session); }, LIVE_FIRST_PCM_MS);
  recElapsedMs = 0;
  recSegmentStart = performance.now();
  paintTimer();
  startTicker();
  setTransport('recording');
  $('rec-status').textContent = 'recording';
  return true;
}

/* Nothing from the worklet after a few seconds: the take is running, but say so
 * — a muted or misrouted input is otherwise invisible until Stop. */
function liveNoAudio(session) {
  session.watchdog = null;
  if (live !== session || session.gotPcm || session.stopping) return;
  session.silentWarned = true;
  $('rec-status').textContent = 'live transcript: no audio is arriving from the microphone';
}

/* Best effort: drop a session we started but whose audio we no longer want.
 * /stream/cancel forgets the session and its audio; a 404 means it is gone. */
function cancelSession(sid) {
  api('/stream/cancel?sid=' + encodeURIComponent(sid), { method: 'POST' })
    .catch(function () { /* it expires on its own */ });
}

/* Stop arrived while starting: unwind without asking for a note. */
function cancelLive() {
  var session = live;
  live = null;
  teardownLive(session);
  cancelSession(session.sid);
  releaseStream();
  stopTicker();
  recElapsedMs = 0;
  paintTimer();
  resetLivePane(false);
  $('rec-status').textContent = 'ready';
  setTransport('ready');
}

/* A message from the worklet: a bare ArrayBuffer is a batch, {type: 'flush'} is
 * the answer to our flush (and only that resolves Stop's waiter — a batch that
 * crosses the flush must not pass for the reply). Either can carry audio. */
function onLivePcm(session, data) {
  var isFlush = !!(data && data.type === 'flush');
  var buffer = isFlush ? data.buffer : data;
  if (buffer && buffer.byteLength) {
    var chunk = new Uint8Array(buffer);
    session.queue.push(chunk);
    session.all.push(chunk);            // the safety copy; takeLiveQueue() copies out
    if (!session.gotPcm) {
      session.gotPcm = true;
      if (session.silentWarned) {       // audio after all: take the warning back
        session.silentWarned = false;
        if (live === session && !session.stopping) {
          $('rec-status').textContent = session.state === 'paused' ? 'paused' : 'recording';
        }
      }
    }
  }
  if (isFlush && session.flushWaiter) session.flushWaiter();
  if (live === session) pumpLive(session);
}

/* Everything queued, as one body — but at most `limit` bytes (one chunk always
 * goes, however big): after an outage the backlog rides out over several
 * requests instead of one unbounded body. */
function takeLiveQueue(session, limit) {
  var take = 0;
  var total = 0;
  while (take < session.queue.length) {
    var len = session.queue[take].length;
    if (take > 0 && limit && total + len > limit) break;
    total += len;
    take += 1;
  }
  var out = new Uint8Array(total);
  var at = 0;
  for (var i = 0; i < take; i++) {
    out.set(session.queue[i], at);
    at += session.queue[i].length;
  }
  session.queue = session.queue.slice(take);
  return out;
}

function appendLive(session, body, signal) {
  var options = {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: body
  };
  if (signal) options.signal = signal;
  return api('/stream/append?sid=' + encodeURIComponent(session.sid), options);
}

/* The daemon has forgotten this session (it restarted, or half an hour of pings
 * failed): appending more is pointless and finish would 404 too. End the take
 * here — the tab's copy of the audio is what Retry sends. */
function liveSessionLost(session, e) {
  if (session.state === 'recording') recElapsedMs += performance.now() - recSegmentStart;
  session.stopping = true;
  session.state = 'lost';
  live = null;
  stopTicker();
  paintTimer();
  teardownLive(session);
  releaseStream();
  lastUpload = { pcm: session.all };
  $('rec-status').textContent =
    'the daemon lost this live session (' + errText(e) + ') — ' +
    'Retry uploads the recording it has kept in this tab';
  setTransport('ready');
}

/* One request at a time, at most one a second; a failed body goes back to the
 * front of the queue and rides along with the next attempt, with a growing pause
 * once the failures start piling up. */
function pumpLive(session) {
  if (live !== session || session.sending || session.stopping) return;
  if (!session.queue.length) return;
  if (performance.now() - session.lastSend < LIVE_MIN_REQUEST_MS) return;
  if (session.retryAt && performance.now() < session.retryAt) return;

  var body = takeLiveQueue(session, LIVE_MAX_BODY_BYTES);
  session.sending = true;
  session.lastSend = performance.now();
  session.inflight = (async function () {
    try {
      var data = await appendLive(session, body);
      if (live !== session) return;
      session.failures = 0;
      session.retryAt = 0;
      if (data && typeof data.seconds === 'number') session.seconds = data.seconds;
      renderLive(data);
      if (session.warned) {
        session.warned = false;
        $('rec-status').textContent = session.state === 'paused' ? 'paused' : 'recording';
      }
    } catch (e) {
      if (live !== session) return;
      session.queue.unshift(body);        // keep the audio: the next tick retries it
      if (e && e.status === 404 && !session.stopping) return liveSessionLost(session, e);
      session.failures += 1;
      if (session.failures >= 3) {
        var backoff = LIVE_BACKOFF_MS * Math.pow(2, session.failures - 3);
        session.retryAt = performance.now() + Math.min(backoff, LIVE_BACKOFF_MAX_MS);
        if (!session.warned) {
          session.warned = true;
          $('rec-status').textContent =
            'still recording — the live transcript stopped updating (' + errText(e) + ')';
        }
      }
    } finally {
      session.sending = false;
    }
  }());
}

function startLivePing(session) {
  if (session.pingTimer) return;
  session.pingTimer = window.setInterval(function () {
    api('/stream/ping?sid=' + encodeURIComponent(session.sid), { method: 'POST' })
      .catch(function () { /* the next append reports it */ });
  }, LIVE_PING_MS);
}

function stopLivePing(session) {
  if (session.pingTimer) window.clearInterval(session.pingTimer);
  session.pingTimer = null;
}

function toggleLivePause() {
  var session = live;
  if (!session || session.stopping) return;
  if (session.state === 'recording') {
    recElapsedMs += performance.now() - recSegmentStart;
    session.state = 'paused';
    audioCall(session.ctx, 'suspend');   // nothing arrives either way
    stopTicker();
    paintTimer();
    startLivePing(session);              // a long pause must not expire the session
    setTransport('paused');
    $('rec-status').textContent = 'paused';
  } else if (session.state === 'paused') {
    recSegmentStart = performance.now();
    session.state = 'recording';
    audioCall(session.ctx, 'resume');    // the worklet picks up on its own
    stopLivePing(session);
    startTicker();
    setTransport('recording');
    $('rec-status').textContent = 'recording';
  }
}

/* Ask the worklet for its last partial batch; give up after 300 ms. */
function flushWorklet(session) {
  return new Promise(function (resolve) {
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      session.flushWaiter = null;
      window.clearTimeout(timer);
      resolve();
    }
    var timer = window.setTimeout(finish, LIVE_FLUSH_WAIT_MS);
    session.flushWaiter = finish;
    try {
      session.node.port.postMessage({ type: 'flush' });
    } catch (e) {
      finish();
    }
  });
}

function teardownLive(session) {
  if (session.pumpTimer) window.clearInterval(session.pumpTimer);
  session.pumpTimer = null;
  if (session.watchdog) window.clearTimeout(session.watchdog);
  session.watchdog = null;
  stopLivePing(session);
  try { session.node.port.onmessage = null; } catch (e) { /* ignore */ }
  [session.src, session.node, session.gain].forEach(function (n) {
    try { n.disconnect(); } catch (e) { /* already gone */ }
  });
  audioCall(session.ctx, 'close');
}

/* An abort signal that fires after `ms`, with the manual fallback for browsers
 * without AbortSignal.timeout. `cancel()` drops the timer once the request is
 * done; there is nothing to cancel on the native path. */
function timeoutSignal(ms) {
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    try {
      return { signal: AbortSignal.timeout(ms), cancel: function () { /* self-clearing */ } };
    } catch (e) { /* fall through to the manual one */ }
  }
  if (typeof AbortController === 'undefined') {
    return { signal: null, cancel: function () { /* no way to time out */ } };
  }
  var ac = new AbortController();
  var timer = window.setTimeout(function () { ac.abort(); }, ms);
  return { signal: ac.signal, cancel: function () { window.clearTimeout(timer); } };
}

/* The live path's safety copy as a WAV: a 44-byte RIFF/WAVE header (PCM, mono,
 * 16 kHz, 16-bit) in front of the s16le chunks the worklet sent. */
function wavFromPcm(chunks) {
  var i;
  var total = 0;
  for (i = 0; i < chunks.length; i++) total += chunks[i].length;
  var out = new Uint8Array(44 + total);
  var view = new DataView(out.buffer);
  function tag(at, s) {
    for (var j = 0; j < s.length; j++) out[at + j] = s.charCodeAt(j);
  }
  tag(0, 'RIFF');
  view.setUint32(4, 36 + total, true);          // file size after this field
  tag(8, 'WAVE');
  tag(12, 'fmt ');
  view.setUint32(16, 16, true);                 // fmt chunk size
  view.setUint16(20, 1, true);                  // PCM, uncompressed
  view.setUint16(22, 1, true);                  // channels
  view.setUint32(24, LIVE_SAMPLE_RATE, true);
  view.setUint32(28, LIVE_SAMPLE_RATE * 2, true);   // byte rate
  view.setUint16(32, 2, true);                  // block align
  view.setUint16(34, 16, true);                 // bits per sample
  tag(36, 'data');
  view.setUint32(40, total, true);
  var at = 44;
  for (i = 0; i < chunks.length; i++) {
    out.set(chunks[i], at);
    at += chunks[i].length;
  }
  return new Blob([out], { type: 'audio/wav' });
}

async function stopLive() {
  var session = live;
  if (!session || session.stopping) return;
  if (session.state === 'recording') recElapsedMs += performance.now() - recSegmentStart;
  session.stopping = true;
  session.state = 'stopping';
  stopTicker();
  paintTimer();
  stopLivePing(session);
  setTransport('processing');
  $('rec-status').textContent = 'finishing the recording…';

  await flushWorklet(session);
  if (session.inflight) { try { await session.inflight; } catch (e) { /* reported already */ } }

  // The tail: two tries, each with a network timeout so Stop cannot hang.
  var unsent = null;
  var rest = takeLiveQueue(session);
  if (rest.length) {
    for (var attempt = 0; attempt < 2; attempt++) {
      var t = timeoutSignal(LIVE_STOP_TIMEOUT_MS);
      try {
        var reply = await appendLive(session, rest, t.signal);
        if (reply && typeof reply.seconds === 'number') session.seconds = reply.seconds;
        renderLive(reply);
        unsent = null;
        break;
      } catch (e) {
        unsent = e;
      } finally {
        t.cancel();
      }
    }
  }

  teardownLive(session);
  releaseStream();
  live = null;

  // Finishing now would transcribe a recording that is missing its end — say so
  // instead, and leave Retry pointing at the copy this tab kept.
  if (unsent) {
    var missing = Math.max(0, Math.round(recordedMs() / 1000 - (session.seconds || 0)));
    lastUpload = { pcm: session.all };
    $('rec-status').textContent =
      'the daemon did not receive the last ' + missing + ' s (' + errText(unsent) + ') — ' +
      'Retry uploads the recording this tab kept';
    setTransport('ready');
    return;
  }

  $('rec-status').textContent =
    'processing… transcribing the whole recording in one pass, then cleaning — ' +
    'the live text below is copyable meanwhile';
  try {
    var data = await api('/stream/finish' + finishQuery(session.sid), { method: 'POST' });
    lastUpload = null;
    showResult(data);
    $('rec-status').textContent = 'ready';
  } catch (e) {
    var kept = (e && e.data) ? e.data.audio_kept : null;
    if (kept) {
      $('rec-status').textContent = errText(e) + ' — the daemon kept the audio: ' + kept;
    } else {
      lastUpload = { pcm: session.all };   // this tab holds the only copy left
      $('rec-status').textContent =
        errText(e) + ' — Retry uploads the recording this tab kept';
    }
  } finally {
    setTransport('ready');
  }
}

/* -------------------------------------------------------------------- notes */

var currentNote = null;     // the last /api/notes/<name> payload
var loadedText = '';        // #note-editor as it was loaded — the dirty baseline
var viewingVersion = null;  // n while an older version is previewed, else null
var processing = false;     // a write (save/restore/regenerate/revise) is in flight
var noteGeneration = 0;     // bumped by loadNote(); see noteToken()

/* Reprocessing a note takes tens of seconds, and the user can open another note
 * meanwhile. Every async note operation captures a token first and re-checks it
 * before touching the DOM, so a late reply never lands on the wrong note. */
function noteToken() {
  return { gen: noteGeneration, name: currentNote ? currentNote.name : null };
}

function stillCurrent(token) {
  return token.gen === noteGeneration &&
    !!currentNote && !!token.name && currentNote.name === token.name;
}

async function loadNotes(openName) {
  var list = $('notes-list');
  list.innerHTML = '';
  var li = document.createElement('li');
  li.className = 'empty';
  li.textContent = 'loading…';
  list.appendChild(li);

  var notes;
  try {
    var data = await getJSON('/api/notes');
    notes = (data && data.notes) || [];
  } catch (e) {
    list.innerHTML = '';
    var err = document.createElement('li');
    err.className = 'empty bad';
    err.textContent = errText(e);
    list.appendChild(err);
    notesLoaded = false;   // try again next time the tab is shown
    return;
  }

  notesLoaded = true;
  renderNotes(notes);
  if (openName) await openNote(openName);
}

function renderNotes(notes) {
  var list = $('notes-list');
  list.innerHTML = '';

  if (!notes.length) {
    var empty = document.createElement('li');
    empty.className = 'empty';
    empty.textContent = 'no notes yet';
    list.appendChild(empty);
    return;
  }

  notes.forEach(function (n) {
    var li = document.createElement('li');
    li.className = 'note-item';

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'note-button';
    btn.dataset.name = n.name;

    var title = document.createElement('span');
    title.className = 'note-item-title';
    title.textContent = n.title || n.name;
    btn.appendChild(title);

    var bits = [];
    if (n.created) bits.push(fmtCreated(n.created));
    if (n.duration_s !== null && n.duration_s !== undefined) bits.push(fmtDuration(n.duration_s));
    if (n.mode) bits.push(n.mode);
    var meta = document.createElement('span');
    meta.className = 'note-item-meta';
    meta.textContent = bits.join(' · ');
    btn.appendChild(meta);

    btn.addEventListener('click', function () { openNote(n.name); });
    li.appendChild(btn);
    list.appendChild(li);
  });

  // A re-render loses the selection; the open note keeps it.
  if (currentNote && currentNote.name) highlightNote(currentNote.name);
}

function highlightNote(name) {
  var buttons = $('notes-list').querySelectorAll('.note-button');
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].classList.toggle('selected', buttons[i].dataset.name === name);
  }
}

/* The editor holds edits the daemon has not seen yet. */
function isDirty() {
  if (!currentNote || viewingVersion !== null) return false;
  return $('note-editor').value !== loadedText;
}

/* Returns false when the user chose to keep unsaved edits. */
function confirmDiscard() {
  if (!isDirty()) return true;
  return window.confirm('This note has unsaved edits. Discard them?');
}

/* Make good on that prompt for callers that do not reload the editor anyway. */
function revertEditor() {
  if (!currentNote || viewingVersion !== null) return;
  $('note-editor').value = loadedText;
  updateSaveState();
}

function updateSaveState() {
  $('note-save').disabled = processing || viewingVersion !== null || !isDirty();
}

function updateProcessState() {
  var have = !!(currentNote && currentNote.name);
  var previewing = viewingVersion !== null;
  var instructions = $('revise-instructions').value.trim();
  var editor = $('note-editor');
  $('regenerate').disabled = processing || previewing || !have;
  $('revise').disabled = processing || previewing || !have || !instructions;
  $('regenerate-mode').disabled = processing;
  $('revise-instructions').disabled = processing;
  $('version-select').disabled = processing;
  $('version-restore').disabled = processing || !previewing;

  // Single owner of the editor's read-only state: an old version on screen or a
  // write in flight both mean "what you type here would be lost".
  editor.readOnly = processing || previewing;
  editor.classList.toggle('viewing-old', previewing);

  // The list stays browsable during a write: the load-generation guard discards a late
  // reply's DOM writes, and the server keeps the new version for when the note is reopened.

  updateSaveState();
}

function setProcessing(on) {
  processing = on;
  updateProcessState();
}

/* Versions -----------------------------------------------------------------
 * The server sends them oldest first; the list shows them newest first and the
 * current note is the last entry. */

function clip(text, max) {
  var s = String(text).replace(/\s+/g, ' ').trim();
  var chars = Array.from(s);   // never cut a surrogate pair in half
  return chars.length > max ? chars.slice(0, max - 1).join('') + '…' : s;
}

function versionDesc(v, isFirst) {
  var op = v.op || 'version';
  if (op === 'clean') {
    if (isFirst) return 'original';
    return v.mode ? 'clean: ' + v.mode : 'clean';
  }
  if (op === 'regenerate') return v.mode ? 'regenerate: ' + v.mode : 'regenerate';
  if (op === 'revise') return v.instructions ? 'revise: "' + clip(v.instructions, 40) + '"' : 'revise';
  if (op === 'restore') {
    return (v.restored_from !== null && v.restored_from !== undefined)
      ? 'restore of v' + v.restored_from : 'restore';
  }
  return op;
}

function versionLabel(v, isFirst) {
  var bits = ['v' + v.n, versionDesc(v, isFirst)];
  var t = fmtTime(v.created);
  if (t) bits.push(t);
  return bits.join(' · ');
}

/* The highest version number, i.e. what #note-editor currently holds. */
function currentVersionN(versions) {
  if (!versions || !versions.length) return null;
  return versions[versions.length - 1].n;
}

function renderVersions(versions) {
  var box = $('note-versions');
  var sel = $('version-select');
  sel.innerHTML = '';
  if (!versions || !versions.length) {
    box.hidden = true;
    viewingVersion = null;
    updateProcessState();
    return;
  }
  var firstN = versions[0].n;
  for (var i = versions.length - 1; i >= 0; i--) {
    var v = versions[i];
    var opt = document.createElement('option');
    opt.value = String(v.n);
    opt.textContent = versionLabel(v, v.n === firstN);
    sel.appendChild(opt);
  }
  sel.value = String(currentVersionN(versions));
  box.hidden = false;
  viewingVersion = null;
  updateProcessState();
}

/* Preview version n. The select is disabled for the duration of the GET, so the
 * reply can only ever be the preview the user is still waiting for. */
async function showVersion(n) {
  if (!currentNote || !currentNote.name || processing) return;
  var status = $('note-save-status');
  var editor = $('note-editor');
  var token = noteToken();

  if (n === currentVersionN(currentNote.versions)) {
    viewingVersion = null;
    editor.value = loadedText;
    status.textContent = '';
    updateProcessState();
    return;
  }

  status.textContent = 'loading v' + n + '…';
  viewingVersion = n;
  setProcessing(true);
  try {
    var data = await getJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/versions/' + encodeURIComponent(n));
    if (!stillCurrent(token) || viewingVersion !== n) return;
    editor.value = data.text || '';
    status.textContent = 'viewing v' + n + ' — restore to edit';
  } catch (e) {
    if (!stillCurrent(token) || viewingVersion !== n) return;
    // Nothing was loaded: hand the editor back instead of freezing it.
    viewingVersion = null;
    status.textContent = 'could not load v' + n + ' — ' + errText(e);
    var back = currentVersionN(currentNote.versions);
    if (back !== null) $('version-select').value = String(back);
  } finally {
    setProcessing(false);
  }
}

async function restoreVersion() {
  if (!currentNote || !currentNote.name || viewingVersion === null || processing) return;
  var status = $('note-save-status');
  var token = noteToken();
  var n = viewingVersion;
  setProcessing(true);
  status.textContent = 'restoring v' + n + '…';
  try {
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/restore', { n: n });
    if (!stillCurrent(token)) return;
    notesLoaded = false;
    viewingVersion = null;          // loadNote() must not think edits are pending
    await loadNote(token.name);
    if (currentNote && currentNote.name === token.name) {
      flash($('note-save-status'), 'restored v' + n + ' as v' + (data.version || '?'));
    }
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

/* Detail -------------------------------------------------------------------- */

/* Click path: asks before throwing unsaved edits away. */
async function openNote(name) {
  if (!name) return;
  if (!confirmDiscard()) return;
  await loadNote(name);
}

async function loadNote(name) {
  if (!name) return;
  var detail = $('note-detail');
  $('process-status').textContent = '';
  $('note-save-status').textContent = '';
  $('note-path-status').textContent = '';
  viewingVersion = null;
  var editor = $('note-editor');
  updateProcessState();            // drops any preview's read-only state
  highlightNote(name);

  // Anything already in flight for the previous note is now stale.
  noteGeneration += 1;
  var gen = noteGeneration;

  var data;
  try {
    data = await getJSON('/api/notes/' + encodeURIComponent(name));
  } catch (e) {
    if (gen !== noteGeneration) return;
    currentNote = null;
    loadedText = '';
    detail.hidden = false;
    $('note-title').textContent = 'could not open ' + name;
    $('note-meta').textContent = errText(e);
    editor.value = '';
    $('note-audio').hidden = true;
    $('note-versions').hidden = true;
    $('note-folder').hidden = true;
    updateProcessState();
    return;
  }
  if (gen !== noteGeneration) return;   // the user opened something else meanwhile

  currentNote = data;
  // The server sends the list-item summary fields top-level; fall back to the raw
  // meta.json keys so an older daemon still renders something sensible.
  var raw = data.meta || {};
  var meta = {
    title: data.title || raw.title,
    created: data.created || raw.created,
    duration_s: (data.duration_s !== undefined && data.duration_s !== null) ? data.duration_s
      : (raw.audio_duration_s !== undefined ? raw.audio_duration_s : raw.recording_duration_s),
    mode: data.mode || raw.cleanup_mode,
    backend: data.backend || raw.cleanup_backend
  };

  $('note-title').textContent = meta.title || data.name || name;

  var bits = [];
  if (meta.created) bits.push(fmtCreated(meta.created));
  if (meta.duration_s !== null && meta.duration_s !== undefined) {
    bits.push(fmtDuration(meta.duration_s));
  }
  if (meta.mode) bits.push(meta.mode);
  if (meta.backend) bits.push(meta.backend);
  $('note-meta').textContent = bits.join(' · ');

  loadedText = data.note || '';
  editor.value = loadedText;
  $('note-transcript').querySelector('pre').textContent = data.transcript || '';

  var audio = $('note-audio');
  if (data.audio_url) {
    audio.src = data.audio_url;
    audio.hidden = false;
  } else {
    audio.removeAttribute('src');
    try { audio.load(); } catch (e) { /* ignore */ }
    audio.hidden = true;
  }

  var path = data.path || '';
  $('note-path').textContent = path;
  $('note-folder').hidden = !path;

  renderVersions(data.versions || []);
  selectIfPresent($('regenerate-mode'), meta.mode);
  updateProcessState();
  detail.hidden = false;
}

/* Manual edit -> a new version. */
async function saveNote() {
  if (!currentNote || !currentNote.name || viewingVersion !== null || processing) return;
  var status = $('note-save-status');
  var token = noteToken();
  var name = token.name;
  var text = $('note-editor').value;
  setProcessing(true);
  status.textContent = 'saving…';
  try {
    var data = await putJSON('/api/notes/' + encodeURIComponent(name) + '/note',
                             { text: text });
    if (!stillCurrent(token)) return;
    notesLoaded = false;
    await loadNote(name);
    if (currentNote && currentNote.name === name) {
      flash($('note-save-status'), 'saved as v' + (data.version || '?'));
    }
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

/* Re-process ---------------------------------------------------------------
 * Regenerate re-runs cleanup from the raw transcript; revise applies an
 * instruction to the current note. Both write a new version. */

/* Apply a {title, note, version} reply to the detail. */
async function applyProcessed(data, status, token) {
  if (!stillCurrent(token)) return;   // the user is looking at another note now

  // The reply is the newest version, whatever was on screen before it landed.
  viewingVersion = null;
  $('note-save-status').textContent = '';
  if (data.title) $('note-title').textContent = data.title;
  loadedText = data.note || '';
  $('note-editor').value = loadedText;
  currentNote.note = loadedText;
  $('revise-instructions').value = '';
  updateProcessState();

  // Refetch: the list item's title, #note-meta and the version list all moved.
  notesLoaded = false;
  await loadNotes(token.name);
  // loadNote() moved the generation on, so match on the name alone here.
  if (currentNote && currentNote.name === token.name) {
    status.textContent = 'done — v' + (data.version || '?');
  }
}

async function regenerateNote() {
  if (!currentNote || !currentNote.name || processing || viewingVersion !== null) return;
  if (!confirmDiscard()) return;
  var status = $('process-status');
  var token = noteToken();
  setProcessing(true);
  status.textContent = 'regenerating…';
  try {
    var body = { mode: $('regenerate-mode').value };
    var backend = $('pick-backend').value;
    if (backend) body.backend = backend;
    var instructions = $('revise-instructions').value.trim();
    if (instructions) body.instructions = instructions;
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/reclean', body);
    await applyProcessed(data, status, token);
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

async function reviseNote() {
  if (!currentNote || !currentNote.name || processing || viewingVersion !== null) return;
  var instructions = $('revise-instructions').value.trim();
  if (!instructions) return;
  if (!confirmDiscard()) return;
  var status = $('process-status');
  var token = noteToken();
  setProcessing(true);
  status.textContent = 'revising…';
  try {
    var body = { instructions: instructions };
    var backend = $('pick-backend').value;
    if (backend) body.backend = backend;
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/revise', body);
    await applyProcessed(data, status, token);
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

/* Folder -------------------------------------------------------------------- */

async function revealNote() {
  if (!currentNote || !currentNote.name) return;
  var btn = $('note-reveal');
  var status = $('note-path-status');
  var token = noteToken();
  btn.disabled = true;
  status.textContent = 'opening…';
  try {
    // No body: the route takes none.
    var data = await api(
      '/api/notes/' + encodeURIComponent(token.name) + '/reveal', { method: 'POST' });
    if (!stillCurrent(token)) return;
    if (data.opened) {
      status.textContent = '';
      flash(status, 'opened');
    } else {
      status.textContent = "couldn't open it here — the path is above";
    }
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    btn.disabled = false;
  }
}

/* ----------------------------------------------------------------- settings */

function makeControl(setting) {
  var control;
  if (setting.kind === 'choice') {
    control = document.createElement('select');
    var choices = setting.choices || [];
    if (setting.value !== null && setting.value !== undefined &&
        choices.indexOf(setting.value) === -1) {
      choices = choices.concat([setting.value]);
    }
    choices.forEach(function (choice) {
      var opt = document.createElement('option');
      opt.value = String(choice);
      opt.textContent = String(choice);
      control.appendChild(opt);
    });
    control.value = setting.value === null || setting.value === undefined
      ? '' : String(setting.value);
  } else if (setting.kind === 'int') {
    control = document.createElement('input');
    control.type = 'number';
    control.value = setting.value === null || setting.value === undefined
      ? '' : String(setting.value);
  } else {
    control = document.createElement('input');
    control.type = 'text';
    control.value = setting.value === null || setting.value === undefined
      ? '' : String(setting.value);
    control.spellcheck = false;
  }
  control.dataset.key = setting.key;
  control.dataset.kind = setting.kind || 'str';
  control.dataset.original = control.value;
  return control;
}

function renderSettings(settings) {
  settingsRows = [];
  var tbody = $('settings-table').querySelector('tbody');
  tbody.innerHTML = '';

  settings.forEach(function (setting) {
    var tr = document.createElement('tr');

    var keyCell = document.createElement('td');
    var code = document.createElement('code');
    code.textContent = setting.key;
    keyCell.appendChild(code);
    tr.appendChild(keyCell);

    var descCell = document.createElement('td');
    descCell.textContent = setting.description || '';
    tr.appendChild(descCell);

    var valueCell = document.createElement('td');
    var control = makeControl(setting);
    valueCell.appendChild(control);

    var hint = null;
    if (setting.editable === false) {
      control.disabled = true;
      hint = 'set ' + (setting.env || setting.key.toUpperCase()) + ' and restart the daemon';
    } else if (setting.source === 'env') {
      control.disabled = true;
      hint = 'overridden by ' + (setting.env || setting.key.toUpperCase());
    }
    if (hint) {
      var hintEl = document.createElement('p');
      hintEl.className = 'row-hint';
      hintEl.textContent = hint;
      valueCell.appendChild(hintEl);
    }
    tr.appendChild(valueCell);

    var sourceCell = document.createElement('td');
    var badge = document.createElement('span');
    badge.className = 'badge badge-' + (setting.source || 'default');
    badge.textContent = setting.source || 'default';
    sourceCell.appendChild(badge);
    tr.appendChild(sourceCell);

    tbody.appendChild(tr);
    settingsRows.push({ setting: setting, control: control });
  });
}

function changedSettings() {
  var payload = {};
  var count = 0;
  settingsRows.forEach(function (row) {
    if (row.control.disabled) return;
    var value = row.control.value;
    if (value === row.control.dataset.original) return;
    if (row.setting.kind === 'int') {
      var n = parseInt(value, 10);
      if (isNaN(n)) throw new Error(row.setting.key + ': "' + value + '" is not a whole number');
      payload[row.setting.key] = n;
    } else {
      payload[row.setting.key] = value;
    }
    count += 1;
  });
  return count ? payload : null;
}

async function saveSettings() {
  var status = $('settings-status');
  var btn = $('settings-save');
  var payload;
  try {
    payload = changedSettings();
  } catch (e) {
    status.textContent = errText(e);
    return;
  }
  if (!payload) {
    flash(status, 'nothing changed');
    return;
  }

  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await putJSON('/api/settings', payload);
    var data = await getJSON('/api/settings');
    var settings = (data && data.settings) || [];
    renderSettings(settings);
    applySettingsToPicks(settings);
    status.textContent = '';
    flash(status, 'saved');
  } catch (e) {
    status.textContent = errText(e);
  } finally {
    btn.disabled = false;
  }
}

/* Vocabulary -------------------------------------------------------------- */

async function loadVocab() {
  var status = $('vocab-status');
  status.textContent = 'loading…';
  try {
    var data = await getJSON('/api/vocab');
    $('vocab').value = data.text || '';
    status.textContent = '';
  } catch (e) {
    vocabLoaded = false;   // let the next visit retry
    status.textContent = errText(e);
  }
}

async function saveVocab() {
  var status = $('vocab-status');
  var btn = $('vocab-save');
  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await putJSON('/api/vocab', { text: $('vocab').value });
    status.textContent = '';
    flash(status, 'saved');
  } catch (e) {
    status.textContent = errText(e);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------- wiring */

function typingInAField(el) {
  if (!el) return false;
  var tag = (el.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'button') return true;
  return !!el.isContentEditable;
}

function wire() {
  TABS.forEach(function (t) {
    var el = $(t.tab);
    if (el) el.addEventListener('click', function () {
      // Leaving the notes view hides the editor — ask before losing edits.
      if (currentTab === 'notes' && t.name !== 'notes') {
        if (!confirmDiscard()) return;
        revertEditor();   // the prompt said "discard", so actually discard them
      }
      showTab(t.name);
    });
  });

  // blur after a click so a later Space toggles pause instead of re-clicking the button
  $('record').addEventListener('click', function (ev) { ev.currentTarget.blur(); startRecording(); });
  $('pause').addEventListener('click', function (ev) { ev.currentTarget.blur(); togglePause(); });
  $('stop').addEventListener('click', function (ev) { ev.currentTarget.blur(); stopRecording(); });
  $('retry').addEventListener('click', function () {
    if (!lastUpload) return;
    if (lastUpload.pcm) return uploadRecording(wavFromPcm(lastUpload.pcm), 'wav');
    uploadRecording(lastUpload.blob, lastUpload.format);
  });

  ['pick-mode', 'pick-backend', 'pick-language', 'live-toggle'].forEach(function (id) {
    $(id).addEventListener('change', savePicks);
  });

  $('copy').addEventListener('click', function () {
    copyText($('result-text').value, $('copy-status'));
  });
  $('live-copy').addEventListener('click', function () {
    copyText(liveText(), $('live-copy-status'));
  });
  $('note-copy').addEventListener('click', function () {
    var token = noteToken();
    copyText($('note-editor').value, $('note-copy-status'),
             function () { return stillCurrent(token); });
  });

  $('result-open').addEventListener('click', function () {
    if (!lastNoteName) return;
    notesLoaded = true;   // keep showTab() from starting a second, nameless load
    showTab('notes');
    loadNotes(lastNoteName);
  });

  $('notes-refresh').addEventListener('click', function () { loadNotes(); });

  $('note-editor').addEventListener('input', updateSaveState);
  $('note-save').addEventListener('click', saveNote);

  $('regenerate').addEventListener('click', regenerateNote);
  $('revise').addEventListener('click', reviseNote);
  $('revise-instructions').addEventListener('input', updateProcessState);
  $('revise-instructions').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !$('revise').disabled) { ev.preventDefault(); reviseNote(); }
  });

  $('version-select').addEventListener('change', function (ev) {
    var sel = ev.currentTarget;
    var n = parseInt(sel.value, 10);
    if (isNaN(n)) return;
    if (!confirmDiscard()) {
      var back = currentVersionN(currentNote && currentNote.versions);
      if (back !== null) sel.value = String(back);
      return;
    }
    showVersion(n);
  });
  $('version-restore').addEventListener('click', restoreVersion);

  $('note-path-copy').addEventListener('click', function () {
    var token = noteToken();
    copyText($('note-path').textContent, $('note-path-status'),
             function () { return stillCurrent(token); });
  });
  $('note-reveal').addEventListener('click', revealNote);

  $('note-continue').disabled = true;   // an affordance only — no behaviour yet

  $('settings-save').addEventListener('click', saveSettings);
  $('vocab-save').addEventListener('click', saveVocab);

  // Closing the tab with unsaved editor text — or during a live take, whose audio
  // only this tab and the daemon's half-finished session hold — asks first.
  window.addEventListener('beforeunload', function (ev) {
    if (!isDirty() && live === null) return;
    ev.preventDefault();
    ev.returnValue = '';
  });

  // Space toggles pause while a recording is active.
  document.addEventListener('keydown', function (ev) {
    if (ev.code !== 'Space' && ev.key !== ' ') return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (typingInAField(document.activeElement)) return;
    if (!capturing() && !capturePaused()) return;
    ev.preventDefault();
    togglePause();
  });
}

function init() {
  wire();
  initLiveToggle();
  setTransport('ready');
  updateProcessState();   // nothing is open yet: save/regenerate/revise stay off
  paintTimer();
  showTab(lsGet(LS_TAB) || 'record');
  boot();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
