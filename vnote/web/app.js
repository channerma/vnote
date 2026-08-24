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
 *     #record #pause #stop      transport buttons (#pause label: Pause/Resume)
 *     #timer                    "0:00" of recorded time (frozen while paused)
 *     #rec-status               ready / recording / paused / processing / error
 #retry                                  re-send the last recording after a failed upload (hidden otherwise)
 *     #pick-mode                select: light edit summary dictation raw
 *     #pick-backend             select, filled from the `backend` setting
 *     #pick-language            text input, placeholder "auto"
 *     #result                   container, hidden until a note exists
 *     #result-title #result-text (readonly textarea) #result-warning
 *     #copy #copy-status #result-open
 *
 *   Notes view
 *     #notes-refresh #notes-list
 *     #note-detail              hidden until a note is opened
 *     #note-title #note-meta #note-audio (<audio>) #note-text (readonly)
 *     #note-copy #note-copy-status
 *     #note-transcript          <details> containing a <pre>
 *     #reclean-mode #reclean #reclean-status
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
    if (data && data.error) throw new Error(data.error);
    throw new Error('HTTP ' + res.status + ' ' + (res.statusText || ''));
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

async function copyText(text, statusEl) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      flash(statusEl, 'copied');
      return;
    }
    throw new Error('no clipboard api');
  } catch (e) {
    flash(statusEl, copyFallback(text) ? 'copied' : 'copy failed');
  }
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
    language: $('pick-language').value
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
var recStarting = false;
var lastUpload = null;   // {blob, format} of a recording whose upload failed — for Retry
var recMime = '';
var recElapsedMs = 0;      // accumulated recorded time (excludes pauses)
var recSegmentStart = 0;   // performance.now() of the current running segment
var recTicker = null;
var lastNoteName = null;

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

function recordedMs() {
  var ms = recElapsedMs;
  if (recorder && recorder.state === 'recording') ms += performance.now() - recSegmentStart;
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

function setTransport(state) {
  var recording = state === 'recording';
  var paused = state === 'paused';
  var busy = state === 'processing';

  $('record').disabled = recording || paused || busy || $('record').dataset.blocked === '1';
  $('pause').disabled = !(recording || paused);
  $('stop').disabled = !(recording || paused);
  $('pause').textContent = paused ? '▶ Resume' : '⏸ Pause';

  ['pick-mode', 'pick-backend', 'pick-language'].forEach(function (id) {
    $(id).disabled = recording || paused || busy;
  });

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

async function startRecording() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $('rec-status').textContent =
      'Microphone access needs a secure context — open this page at ' +
      'http://localhost:8760 or http://127.0.0.1:8760';
    return;
  }
  if (typeof MediaRecorder === 'undefined') {
    $('rec-status').textContent = 'this browser has no MediaRecorder — recording is not possible';
    return;
  }

  if (recorder || recStream || recStarting) return;  // a second click must not start a second recorder
  recStarting = true;
  $('record').disabled = true;
  $('rec-status').textContent = 'requesting the microphone…';
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    recStarting = false;
    $('rec-status').textContent = 'microphone unavailable — ' + errText(e);
    setTransport('ready');
    return;
  }
  recStarting = false;

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
  $('rec-status').textContent = 'recording';
}

function togglePause() {
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
    $('rec-status').textContent = 'recording';
  }
}

function stopRecording() {
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

  $('retry').hidden = true;
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
    $('retry').hidden = false;
    $('rec-status').textContent = errText(e) + ' — the recording is still here: Retry upload, or Record to start over';
  } finally {
    setTransport('ready');
    if ($('record').dataset.blocked !== '1') $('record').disabled = false;
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

/* -------------------------------------------------------------------- notes */

var currentNote = null;

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
  if (openName) openNote(openName);
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
}

function highlightNote(name) {
  var buttons = $('notes-list').querySelectorAll('.note-button');
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].classList.toggle('selected', buttons[i].dataset.name === name);
  }
}

async function openNote(name) {
  if (!name) return;
  var detail = $('note-detail');
  $('reclean-status').textContent = '';
  highlightNote(name);

  var data;
  try {
    data = await getJSON('/api/notes/' + encodeURIComponent(name));
  } catch (e) {
    detail.hidden = false;
    $('note-title').textContent = 'could not open ' + name;
    $('note-meta').textContent = errText(e);
    $('note-text').value = '';
    $('note-audio').hidden = true;
    return;
  }

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

  $('note-text').value = data.note || '';
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

  selectIfPresent($('reclean-mode'), meta.mode);
  detail.hidden = false;
}

async function recleanNote() {
  if (!currentNote || !currentNote.name) return;
  var btn = $('reclean');
  var status = $('reclean-status');
  btn.disabled = true;
  status.textContent = 're-cleaning…';
  try {
    var body = { mode: $('reclean-mode').value };
    var backend = $('pick-backend').value;
    if (backend) body.backend = backend;
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(currentNote.name) + '/reclean', body);
    if (data.title) $('note-title').textContent = data.title;
    $('note-text').value = data.note || '';
    currentNote.note = data.note || '';
    status.textContent = '';
    flash(status, 're-cleaned');
    notesLoaded = false;
  } catch (e) {
    status.textContent = errText(e);
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
    if (el) el.addEventListener('click', function () { showTab(t.name); });
  });

  // blur after a click so a later Space toggles pause instead of re-clicking the button
  $('record').addEventListener('click', function (ev) { ev.currentTarget.blur(); startRecording(); });
  $('pause').addEventListener('click', function (ev) { ev.currentTarget.blur(); togglePause(); });
  $('stop').addEventListener('click', function (ev) { ev.currentTarget.blur(); stopRecording(); });
  $('retry').addEventListener('click', function () {
    if (lastUpload) uploadRecording(lastUpload.blob, lastUpload.format);
  });

  ['pick-mode', 'pick-backend', 'pick-language'].forEach(function (id) {
    $(id).addEventListener('change', savePicks);
  });

  $('copy').addEventListener('click', function () {
    copyText($('result-text').value, $('copy-status'));
  });
  $('note-copy').addEventListener('click', function () {
    copyText($('note-text').value, $('note-copy-status'));
  });

  $('result-open').addEventListener('click', function () {
    if (!lastNoteName) return;
    showTab('notes');
    loadNotes(lastNoteName);
  });

  $('notes-refresh').addEventListener('click', function () { loadNotes(); });
  $('reclean').addEventListener('click', recleanNote);

  $('settings-save').addEventListener('click', saveSettings);
  $('vocab-save').addEventListener('click', saveVocab);

  // Space toggles pause while a recording is active.
  document.addEventListener('keydown', function (ev) {
    if (ev.code !== 'Space' && ev.key !== ' ') return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (typingInAField(document.activeElement)) return;
    if (!recorder) return;
    if (recorder.state !== 'recording' && recorder.state !== 'paused') return;
    ev.preventDefault();
    togglePause();
  });
}

function init() {
  wire();
  setTransport('ready');
  paintTimer();
  showTab(lsGet(LS_TAB) || 'record');
  boot();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
