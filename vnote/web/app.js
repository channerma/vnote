/* vnote web UI — vanilla JS, no build step, no external requests.
 *
 * Contract between markup (index.html, the Claude Design handoff) and behaviour
 * (this file). Every id below exists exactly once in the document.
 *
 * State is CSS-driven: this file sets data attributes, style.css does the rest.
 *   #app        data-view="note|settings"  data-daemon="down"  data-sidebar="collapsed"
 *   #view-note  data-state="idle|recording|paused|processing|note"
 *               data-live="on|off"  data-starting="true" (during a start, Stop = Cancel)
 *   #note       data-raw="shown|hidden"    tab-note|tab-raw|tab-audio (phone tabs)
 *               data-processed="no"        no note.md and nothing failed: a raw take
 *   #sidebar    data-open="true|false"     (off-canvas below 860px)
 *
 *   Shell
 *     #app                      the flex shell; carries view/daemon/sidebar state
 *     #sidebar #sidebar-toggle  the history pane; the toggle collapses it
 *     #new-note                 clears the stage back to idle
 *     #notes-search             filters the rows by title, live
 *     #stats-notes #stats-minutes   the stats strip ("7 notes this week", "29 min")
 *     #notes-list               .day headers + .note-row anchors, newest first
 *     #notes-empty              swapped with #notes-list via `hidden`
 *     #notes-empty-dir          the daemon's notes_dir, inside that sentence
 *     #nav-settings             toggles data-view
 *     #daemon-info              one line from /health ("warming <model> …" until the
 *                               model lands, "whisper failed: …" when it never
 *                               does), or "daemon unreachable"
 *
 *   Stage — recording
 *     #view-note                the stage; data-state drives every screen below
 *     #rec-status               New note / Recording / Paused / Processing
 *     #timer #big-timer         recorded time (frozen while paused); the big one
 *                               is the stage when the live transcript is off
 *     #record #pause #stop      transport (#pause: Pause/Resume, #stop: Stop/Cancel)
 *     #live-copy #live-copy-status   copy the live text, from the top bar
 *     #process-status           the one status line: the processing sentence, a
 *                               live-transcript warning, a reprocess result
 *     #pick-mode                select: light edit summary dictation raw
 *     #pick-backend             select, filled from /api/settings
 *     #pick-language            select: the configured language, the last pick,
 *                               the common ones, and auto
 *     #process-toggle           checkbox: run cleanup on Stop (off = a raw note)
 *     #live-toggle              checkbox: stream PCM and show the transcript live
 *     #mic-help                 banner: permission denied / insecure context
 *     #retry-wrap #retry-detail #retry   banner: a failed upload, still in this tab
 *     #live                     the live stage (scroller)
 *     #live-committed           the settled text (one <p> per paragraph)
 *     #live-tail                <span>: the still-changing tail
 *
 *   Stage — a note
 *     #note                     the article; data-raw drives the raw drawer
 *     #note-title               contenteditable; rewrites the note's "# " heading
 *     #note-meta                created, duration, mode, backend, whisper, timings
 *     #version-select #version-restore   linear history, newest first
 *     #note-continue            disabled affordance ("Continue recording")
 *     #note-warning             "cleanup failed — this is the raw transcript"
 *     #note-unprocessed         the calm twin: a raw take, nothing went wrong
 *     #note-tab-note #note-tab-raw #note-tab-audio   phone tabs over the panes
 *     #note-editor              the note's Markdown
 *     #note-save #note-save-status    PUT the editor as a new version
 *     #note-copy #note-copy-status
 *     #note-raw #note-raw-save #note-raw-copy #note-raw-status #note-raw-toggle
 *                               the transcript, editable — Save rewrites transcript.txt
 *     #regenerate-mode #regenerate    re-run cleanup from the raw transcript
 *     #revise-instructions      ONE instructions box, read by both Regenerate (appended
 *                               to the cleanup prompt) and Revise
 *     #revise                   apply the instruction to the current note
 *     #note-audio               <audio>, hidden when the folder has none
 *     #note-path #note-path-copy #note-path-status #note-reveal
 *
 *   Settings view
 *     #view-settings            the settings screen (CSS only: data-view shows it)
 *     #settings-table           the <tbody> this file fills
 *     #settings-save #settings-status
 *     #vocab #vocab-save #vocab-status
 */

'use strict';

/* ------------------------------------------------------------------ helpers */

function $(id) { return document.getElementById(id); }

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

function toDate(iso) {
  if (!iso) return null;
  var d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

/* ISO string -> local "HH:MM" */
function fmtTime(iso) {
  var d = toDate(iso);
  return d ? pad2(d.getHours()) + ':' + pad2(d.getMinutes()) : '';
}

/* Local calendar day of an ISO string, as a comparable key. */
function dayKey(iso) {
  var d = toDate(iso);
  if (!d) return '';
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/* "Today" / "Yesterday" / "Aug 22" — the sidebar's day headers. */
function dayLabel(iso) {
  var d = toDate(iso);
  if (!d) return 'Undated';
  var today = new Date();
  var key = dayKey(iso);
  if (key === dayKey(today.toISOString())) return 'Today';
  var yesterday = new Date(today.getTime());
  yesterday.setDate(yesterday.getDate() - 1);   // not -86400000: DST days are not 24 h
  if (key === dayKey(yesterday.toISOString())) return 'Yesterday';
  try {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  } catch (e) {
    return key;
  }
}

/* seconds -> "m:ss" */
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return '';
  var total = Math.max(0, Math.round(Number(seconds)));
  return Math.floor(total / 60) + ':' + pad2(total % 60);
}

/* Transient status text ("copied", "saved 14:33", …) */
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
 * whose status line belongs to a note drop the flash once that note is gone.
 * `show`, when given, replaces the flash — a status line that carries a standing
 * marker has to repaint it afterwards (see flashRaw). */
async function copyText(text, statusEl, stillWanted, show) {
  var msg;
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('no clipboard api');
    await navigator.clipboard.writeText(text);
    msg = 'copied';
  } catch (e) {
    msg = copyFallback(text) ? 'copied' : 'copy failed';
  }
  if (stillWanted && !stillWanted()) return;
  if (show) show(msg);
  else flash(statusEl, msg);
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

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/* -------------------------------------------------------------- shell state
 *
 * The four attributes below are the whole navigation model: there are no tabs
 * and nothing is shown or hidden from here beyond them and the `hidden` flags.
 */

var vocabLoaded = false;

function setView(name) {
  var view = name === 'settings' ? 'settings' : 'note';
  $('app').dataset.view = view;
  if (view === 'settings' && !vocabLoaded) {
    vocabLoaded = true;
    loadVocab();
  }
}

function currentView() { return $('app').dataset.view === 'settings' ? 'settings' : 'note'; }

var stage = 'idle';   // data-state on #view-note

var STAGE_LABEL = {
  idle: 'New note', recording: 'Recording', paused: 'Paused',
  processing: 'Processing', note: 'Note'
};

function setStage(name) {
  stage = name;
  $('view-note').dataset.state = name;
  $('rec-status').textContent = STAGE_LABEL[name] || name;
}

/* The live pane or the big timer, decided per take (a take that fell back to
 * MediaRecorder has no live text, whatever the toggle says). */
function setLiveView(on) {
  $('view-note').dataset.live = on ? 'on' : 'off';
}

function narrow() {
  try {
    return !!(window.matchMedia && window.matchMedia('(max-width: 860px)').matches);
  } catch (e) {
    return false;
  }
}

/* Below 860px the sidebar is an overlay (data-open); above it, a column the
 * toggle collapses away (data-sidebar). */
function toggleSidebar() {
  if (narrow()) {
    var side = $('sidebar');
    side.dataset.open = side.dataset.open === 'true' ? 'false' : 'true';
  } else {
    var app = $('app');
    if (app.dataset.sidebar === 'collapsed') delete app.dataset.sidebar;
    else app.dataset.sidebar = 'collapsed';
  }
  syncSidebarToggle();
}

/* The one button for both moves, so it has to say which one it is about to make.
 * "Expanded" is "the sidebar is on screen", overlay or column. */
function syncSidebarToggle() {
  var open = narrow() ? $('sidebar').dataset.open === 'true'
                      : $('app').dataset.sidebar !== 'collapsed';
  var btn = $('sidebar-toggle');
  var label = open ? 'Hide sidebar' : 'Show sidebar';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  btn.setAttribute('aria-label', label);
  btn.title = label;
}

function closeSidebarOverlay() {
  if (!narrow()) return;
  $('sidebar').dataset.open = 'false';
  syncSidebarToggle();
}

function daemonIsDown() { return $('app').dataset.daemon === 'down'; }

/* --------------------------------------------------------------- daemon health */

var HEALTH_POLL_MS = 5000;
var WARM_POLL_MS = 2000;   // while the models load, ask more often so the strip clears promptly
var HEALTH_TIMEOUT_MS = 4000;   // a hung socket must not stall the poll: say "unreachable" instead

var warming = false;       // last /health said the daemon is still loading Whisper

/* Record stays enabled while warming: live audio spills to disk and the daemon's
 * worker simply waits for the model. Only a daemon that is *down* disables it. */
function daemonUp(health) {
  delete $('app').dataset.daemon;
  var failed = typeof health.warm_error === 'string' ? health.warm_error : '';
  warming = health.warm === false && !failed;   // a daemon too old to say is treated as warm
  if (failed) {
    $('daemon-info').textContent = 'whisper failed: ' + failed;   // it will not arrive by waiting
  } else if (warming) {
    $('daemon-info').textContent = 'warming ' + (health.whisper_model || '?') + ' …';
  } else {
    var line = 'vnote ' + (health.version || '?') + ' · ' +
      (health.whisper_model || '?') + ' on ' + (health.device || '?');
    if (health.ollama === 'starting') line += ' · ollama starting';
    $('daemon-info').textContent = line;
  }
  syncRecordEnabled();
}

/* Quietly: a daemon that is down is polled every 5 s, and nothing is logged.
 * Never while a take runs: the attribute takes the pointer off #stop, and a daemon
 * restarted mid-recording would leave the take with no way to end. The next poll
 * after it ends sets it if the daemon is still gone. */
function daemonDown() {
  if (takeActive()) return;
  warming = false;
  $('app').dataset.daemon = 'down';
  $('daemon-info').textContent = 'daemon unreachable';
  syncRecordEnabled();
}

/* CSS already stops the pointer on #record / #stop / #new-note while the daemon
 * is down; this keeps the keyboard honest too. */
function syncRecordEnabled() {
  if (!takeActive()) $('record').disabled = daemonIsDown();
}

/* Notes are also created outside this page (the tray, the CLI). Refresh the list
 * whenever nothing here would be disturbed by it. */
function refreshNotesIfIdle() {
  if (daemonIsDown() || takeActive() || isDirty()) return;
  loadNotes();
}

var healthTicks = 0;
var NOTES_REFRESH_POLLS = 6;   // every 6th health poll, i.e. ~30 s

async function checkHealth() {
  var t = timeoutSignal(HEALTH_TIMEOUT_MS);
  try {
    daemonUp(await api('/health', { method: 'GET', signal: t.signal }));
  } catch (e) {
    daemonDown();
  } finally {
    t.cancel();
  }
  healthTicks += 1;
  if (healthTicks % NOTES_REFRESH_POLLS === 0) refreshNotesIfIdle();
}

/* A chained timer rather than one interval: the beat changes with the state. */
var healthTimer = null;

function scheduleHealth() {
  if (healthTimer !== null) window.clearTimeout(healthTimer);
  healthTimer = window.setTimeout(function () {
    healthTimer = null;
    checkHealth().then(scheduleHealth, scheduleHealth);
  }, warming ? WARM_POLL_MS : HEALTH_POLL_MS);
}

/* --------------------------------------------------------------------- boot */

var settingsRows = [];   // [{setting, control}] — control null for read-only rows
var backendChoices = [];
var defaultMode = 'edit';   // the daemon's default_mode; 'edit' is its built-in one

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
    live: $('live-toggle').checked,
    process: $('process-toggle').checked
  }));
}

/* Off: Stop writes a raw note (audio + transcript, no LLM) whatever the mode says,
 * and the mode pick keeps its value for the Regenerate that follows. Default on. */
function initProcessToggle() {
  var picks = readPicks();
  $('process-toggle').checked = (picks && typeof picks.process === 'boolean') ? picks.process : true;
}

/* True when Stop must not run cleanup — the mode pick says raw, or the toggle is off. */
function skipCleanup() {
  return $('pick-mode').value === 'raw' || !$('process-toggle').checked;
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

function settingValue(settings, key) {
  var s = findSetting(settings, key);
  return s && s.value !== null && s.value !== undefined ? String(s.value) : '';
}

/* "ollama · qwen2.5:14b" — the design's backend label; claude-code brings its own
 * model choice, so it stays bare. */
function backendLabel(settings, name) {
  var model = '';
  if (name === 'ollama') model = settingValue(settings, 'ollama_model');
  else if (name === 'claude') model = settingValue(settings, 'claude_model');
  return model ? name + ' · ' + model : name;
}

function addOption(sel, value, label) {
  var opt = document.createElement('option');
  opt.value = value;
  opt.textContent = label;
  sel.appendChild(opt);
  return opt;
}

/* The languages the picker offers beyond the configured one: whisper takes many
 * more, but a per-recording pick is a short list, not a catalogue. */
var COMMON_LANGUAGES = ['en', 'de', 'fr', 'es', 'it', 'pt', 'nl', 'pl', 'sv', 'ru', 'ja', 'zh', 'ko'];

function applySettingsToPicks(settings) {
  // localStorage wins over the daemon's defaults, and its language is in the list.
  var picks = readPicks();

  var backend = findSetting(settings, 'backend');
  var backendSel = $('pick-backend');
  backendSel.innerHTML = '';
  backendChoices = (backend && backend.choices) ? backend.choices.slice() : [];
  if (!backendChoices.length && backend && backend.value) backendChoices = [backend.value];
  backendChoices.forEach(function (choice) {
    addOption(backendSel, choice, backendLabel(settings, choice));
  });
  if (backend) selectIfPresent(backendSel, backend.value);

  var mode = findSetting(settings, 'default_mode');
  if (mode && mode.value) defaultMode = mode.value;
  if (mode) selectIfPresent($('pick-mode'), mode.value);

  // "Everything stays in <notes_dir> on this machine" — the markup's fallback text
  // stands when the daemon does not say where that is.
  var notesDir = settingValue(settings, 'notes_dir').trim();
  if (notesDir) $('notes-empty-dir').textContent = notesDir;

  // The configured language leads (it is what Settings would use), then whatever
  // was picked last, then the common ones, then auto.
  var langSel = $('pick-language');
  var lang = settingValue(settings, 'language').trim();
  langSel.innerHTML = '';
  var listed = [];   // an array, not a lookup object: a code is not a property name
  function addLanguage(code, label) {
    code = String(code || '').trim();
    if (!code || listed.indexOf(code) !== -1) return;
    listed.push(code);
    addOption(langSel, code, label || code);
  }
  if (lang) addLanguage(lang, lang + ' (settings)');
  if (picks) addLanguage(picks.language);
  COMMON_LANGUAGES.forEach(function (code) { addLanguage(code); });
  addOption(langSel, '', 'auto');
  langSel.value = lang;          // no configured language: auto

  if (picks) {
    selectIfPresent($('pick-mode'), picks.mode);
    selectIfPresent($('pick-backend'), picks.backend);
    if (picks.language === '') langSel.value = '';
    else selectIfPresent(langSel, picks.language);
  }
}

async function boot() {
  var healthPromise = getJSON('/health');
  var settingsPromise = getJSON('/api/settings');

  try {
    daemonUp(await healthPromise);
  } catch (e) {
    daemonDown();
  }

  try {
    var data = await settingsPromise;
    var settings = (data && data.settings) || [];
    applySettingsToPicks(settings);
    renderSettings(settings);
  } catch (e) {
    // 'edit' is the daemon's built-in default; without this the first option
    // ('light') would silently become the one this recording uses.
    selectIfPresent($('pick-mode'), 'edit');
    $('settings-status').textContent = errText(e);
    var tbody = $('settings-table');
    tbody.innerHTML = '';
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = 'could not load settings: ' + errText(e);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  await loadNotes();
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
 * null means there is nothing to retry and the banner stays hidden. */
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
  var elx = $('live-toggle');
  liveAvailable = liveSupported();
  if (!liveAvailable) {
    elx.checked = false;
    elx.disabled = true;
    elx.title = 'not supported in this browser';
    setLiveView(false);
    return;
  }
  var picks = readPicks();
  elx.checked = (picks && typeof picks.live === 'boolean') ? picks.live : true;
  setLiveView(elx.checked);
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

/* The stage belongs to a take: New note and the history rows keep their hands off. */
function takeActive() {
  return recStarting || !!live || !!recorder ||
    stage === 'recording' || stage === 'paused' || stage === 'processing';
}

function recordedMs() {
  var ms = recElapsedMs;
  if (capturing()) ms += performance.now() - recSegmentStart;
  return ms;
}

function paintTimer() {
  var text = fmtDuration(recordedMs() / 1000);
  $('timer').textContent = text;
  $('big-timer').textContent = text;
}

function startTicker() {
  if (recTicker) return;
  recTicker = window.setInterval(paintTimer, 250);
}

function stopTicker() {
  if (recTicker) window.clearInterval(recTicker);
  recTicker = null;
}

/* The one status line under the top bar. CSS shows it in any state as long as it
 * has something to say. */
function say(text) { $('process-status').textContent = text || ''; }

function showMicHelp(on) { $('mic-help').hidden = !on; }

/* The failed-upload banner. `lastUpload` decides whether it can be shown at all;
 * setTransport() owns `hidden`, so the honest detail is all that is set here. */
function retryDetail(text) { $('retry-detail').textContent = text || ''; }

/* Only the microphone banner: #retry-wrap belongs to `lastUpload`, and that is the
 * tab's only copy of a recording the daemon never got. setTransport() owns whether
 * it is on screen; a successful upload — or New note, which asks first — clears it. */
function clearBanners() {
  showMicHelp(false);
}

/* States: ready, starting (microphone/session being opened — Stop cancels),
 * recording, paused, processing. Ready leaves an open note on the stage. */
function setTransport(state) {
  var recording = state === 'recording';
  var paused = state === 'paused';
  var busy = state === 'processing';
  var starting = state === 'starting';
  var active = recording || paused || busy || starting;
  var view = $('view-note');

  $('record').disabled = active || daemonIsDown();
  $('pause').disabled = !(recording || paused);
  $('stop').disabled = !(recording || paused || starting);
  $('pause').textContent = paused ? 'Resume' : 'Pause';
  $('stop').textContent = starting ? 'Cancel' : 'Stop';

  // Retrying mid-take would upload one recording while another runs.
  var offerRetry = !!lastUpload && !active;
  $('retry-wrap').hidden = !offerRetry;
  $('retry').disabled = !offerRetry;
  // …and the live text the user was watching stays on the stage until it is gone
  // (CSS: the idle stage hides #live and the Copy button otherwise).
  if (offerRetry) view.dataset.retry = 'true';
  else delete view.dataset.retry;

  ['pick-mode', 'pick-backend', 'pick-language'].forEach(function (id) {
    $(id).disabled = active;
  });
  $('live-toggle').disabled = active || !liveAvailable;
  $('process-toggle').disabled = active;

  if (starting) {
    setStage('idle');
    view.dataset.starting = 'true';
  } else {
    delete view.dataset.starting;
    if (recording) setStage('recording');
    else if (paused) setStage('paused');
    else if (busy) setStage('processing');
    else if (stage !== 'note') setStage('idle');
  }
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
  say('this browser has no MediaRecorder — recording is not possible');
  setTransport('ready');
}

/* Stop pressed during the start: give the microphone back and stay ready. */
function startCancelled() {
  if (!recCancelled) return false;
  recCancelled = false;
  recStarting = false;
  releaseStream();
  say('');
  setTransport('ready');
  return true;
}

/* Record: the idle stage becomes a take. */
async function startRecording() {
  if (daemonIsDown()) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showMicHelp(true);
    say('the browser exposes no microphone API here — a secure context is required');
    return;
  }
  var wantLive = liveAvailable && $('live-toggle').checked;
  if (!wantLive && typeof MediaRecorder === 'undefined') {
    say('this browser has no MediaRecorder — recording is not possible');
    return;
  }

  // a second click must not start a second recorder — nor a second live session
  if (recorder || recStream || recStarting || live) return;
  if (!confirmDiscard()) return;   // the stage is about to lose the open note
  recStarting = true;
  recCancelled = false;
  liveFallbackReason = '';
  clearBanners();
  closeNote();
  setTransport('starting');   // Stop is live from here on: it cancels the start
  say('requesting the microphone…');
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    recStarting = false;
    showMicHelp(true);
    say('microphone unavailable — ' + errText(e));
    setTransport('ready');
    return;
  }
  if (startCancelled()) return;

  resetLivePane();
  setLiveView(wantLive);
  if (wantLive) {
    var started = await startLive();
    recStarting = false;
    if (started) {
      if (recCancelled) { recCancelled = false; cancelLive(); }
      return;
    }
    setLiveView(false);        // this take has no live text: the big timer is the stage
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
    say('could not start the recorder — ' + errText(e));
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
    say('recording error — ' + errText(err));
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
    say('could not start the recorder — ' + errText(e));
    setTransport('ready');
    return;
  }

  paintTimer();
  startTicker();
  setTransport('recording');
  say(recordingNote());
}

/* Don't paper over a live-transcript failure: while a fallback take runs, the
 * status line keeps saying why the live pane is missing. */
function recordingNote() {
  return liveFallbackReason
    ? 'live transcript unavailable (' + liveFallbackReason + ') — recording without it'
    : '';
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
  } else if (recorder.state === 'paused') {
    recSegmentStart = performance.now();
    try { recorder.resume(); } catch (e) { /* unsupported */ }
    startTicker();
    setTransport('recording');
  }
}

function stopRecording() {
  if (recStarting) {          // nothing to stop yet: startRecording() unwinds instead
    recCancelled = true;
    say('cancelling…');
    return;
  }
  if (live) return stopLive();
  if (!recorder) return;
  if (recorder.state === 'recording') recElapsedMs += performance.now() - recSegmentStart;
  stopTicker();
  setTransport('processing');
  say(processingCopy());
  try {
    recorder.stop();   // the rest happens in recorder.onstop
  } catch (e) {
    releaseStream();
    recorder = null;
    say('could not stop the recorder — ' + errText(e));
    setTransport('ready');
  }
}

/* The design's processing sentence: no honest percentage exists, so it says what
 * is happening and how long it usually takes. */
function processingCopy() {
  var what = skipCleanup() ? 'no cleanup' : 'then cleaning with ' + $('pick-mode').value;
  return 'Transcribing ' + fmtDuration(recordedMs() / 1000) + ' of audio, ' + what +
    ' — usually 20–40 s.';
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
  if (skipCleanup()) {
    add('raw', '1');           // raw => no LLM, no mode
  } else {
    add('mode', mode);
    add('backend', backend);
  }
  add('language', language || 'auto');  // blank pick = auto-detect, even if a language is saved in settings
  return params.length ? '?' + params.join('&') : '';
}

async function uploadRecording(blob, format) {
  if (!blob || blob.size === 0) {
    say('nothing was recorded');
    setTransport('ready');
    return;
  }

  setTransport('processing');
  say(processingCopy());

  try {
    var data = await api('/api/note' + noteQuery(format), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob
    });
    lastUpload = null;
    retryDetail('');
    await showResult(data);
  } catch (e) {
    lastUpload = { blob: blob, format: format };  // the browser holds the only copy until the daemon has it
    retryDetail(errText(e) + '.');
    say('');
  } finally {
    setTransport('ready');   // and with it the retry banner, from lastUpload
  }
}

/* The note reply: open it on the stage exactly as the sidebar would. */
async function showResult(data) {
  lastNoteName = data.name || null;
  if (!lastNoteName) {
    say('the daemon returned a note without a name — it is on disk, but cannot be opened here');
    return;
  }
  await loadNotes();
  await loadNote(lastNoteName);   // not openNote(): this take owns the stage already
  if (data.cleanup_error && currentNote && currentNote.name === lastNoteName) {
    say('cleanup failed — this is the raw transcript: ' + data.cleanup_error);
  }
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

/* Clear the pane. Whether it is on screen is data-state's business, not ours. */
function resetLivePane() {
  liveParas = [];
  liveTail = '';
  $('live-committed').textContent = '';
  $('live-tail').textContent = '';
}

/* The scroller around the live text. */
function livePane() { return $('live'); }

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
      // a span, not a <p>: #live-committed is inline inside .live-text, and the
      // tail has to keep flowing after the last paragraph (.live-para in the CSS)
      var p = document.createElement('span');
      p.className = 'live-para';
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

/* Blank language pick = auto-detect, as in noteQuery(). */
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
  if (skipCleanup()) {
    add('raw', '1');           // raw => no LLM, no mode
  } else {
    add('mode', mode);
    add('backend', backend);
  }
  add('language', language || 'auto');
  return '?' + params.join('&');
}

/* Returns true once PCM is flowing; false means "fall back to MediaRecorder",
 * with the reason in liveFallbackReason (and in the status line meanwhile). */
function liveUnavailable(reason) {
  liveFallbackReason = reason;
  say('live transcript unavailable — recording without it (' + reason + ')');
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
  say('');
  return true;
}

/* Nothing from the worklet after a few seconds: the take is running, but say so
 * — a muted or misrouted input is otherwise invisible until Stop. */
function liveNoAudio(session) {
  session.watchdog = null;
  if (live !== session || session.gotPcm || session.stopping) return;
  session.silentWarned = true;
  say('no audio is arriving from the microphone');
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
  resetLivePane();
  say('');
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
        if (live === session && !session.stopping) say('');
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
  retryDetail('The daemon lost this live session (' + errText(e) + ').');
  say('');
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
        say('');
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
          say('still recording — the live transcript stopped updating (' + errText(e) + ')');
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
  } else if (session.state === 'paused') {
    recSegmentStart = performance.now();
    session.state = 'recording';
    audioCall(session.ctx, 'resume');    // the worklet picks up on its own
    stopLivePing(session);
    startTicker();
    setTransport('recording');
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
  say(processingCopy());

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
    retryDetail('The daemon did not receive the last ' + missing + ' s (' + errText(unsent) + ').');
    say('');
    setTransport('ready');
    return;
  }

  try {
    var data = await api('/stream/finish' + finishQuery(session.sid), { method: 'POST' });
    lastUpload = null;
    retryDetail('');
    await showResult(data);
  } catch (e) {
    var kept = (e && e.data) ? e.data.audio_kept : null;
    if (kept) {
      say(errText(e) + ' — the daemon kept the audio: ' + kept);
    } else {
      lastUpload = { pcm: session.all };   // this tab holds the only copy left
      retryDetail(errText(e) + '.');
      say('');
    }
  } finally {
    setTransport('ready');
  }
}

/* -------------------------------------------------------------------- notes */

var allNotes = [];          // the last /api/notes payload, newest first
var currentNote = null;     // the last /api/notes/<name> payload
var loadedText = '';        // #note-editor as it was loaded — the dirty baseline
var loadedTranscript = '';  // what #note-raw held when the note was loaded
var titleEditable = false;  // false for a note with no "# " heading (dictation)
var haveVersions = false;
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

function sideNote(text, bad) {
  var list = $('notes-list');
  list.innerHTML = '';
  list.appendChild(el('div', bad ? 'side-note bad' : 'side-note', text));
  $('notes-list').hidden = false;
  $('notes-empty').hidden = true;
}

async function loadNotes() {
  if (!allNotes.length) sideNote('loading…');

  var notes;
  try {
    var data = await getJSON('/api/notes');
    notes = (data && data.notes) || [];
  } catch (e) {
    allNotes = [];
    sideNote(errText(e), true);
    renderStats([]);
    return;
  }

  allNotes = notes;
  renderStats(notes);
  renderNotes(filteredNotes());
}

function filteredNotes() {
  var q = $('notes-search').value.trim().toLowerCase();
  if (!q) return allNotes;
  return allNotes.filter(function (n) {
    return String(n.title || n.name || '').toLowerCase().indexOf(q) !== -1;
  });
}

/* "7 notes this week · 29 min" — from the whole list, not the filtered view. */
function renderStats(notes) {
  var since = Date.now() - 7 * 86400000;
  var count = 0;
  var seconds = 0;
  notes.forEach(function (n) {
    var d = toDate(n.created);
    if (!d || d.getTime() < since) return;
    count += 1;
    if (n.duration_s) seconds += Number(n.duration_s) || 0;
  });
  $('stats-notes').textContent = count + (count === 1 ? ' note this week' : ' notes this week');
  $('stats-minutes').textContent = Math.round(seconds / 60) + ' min';
}

function noteRow(n) {
  var row = el('a', 'note-row');
  row.setAttribute('href', '#');
  row.dataset.name = n.name;
  row.appendChild(el('span', 'note-row-title', n.title || n.name));

  var meta = el('span', 'note-row-meta');
  meta.appendChild(el('span', null, fmtTime(n.created)));
  var duration = fmtDuration(n.duration_s);
  if (duration) meta.appendChild(el('span', null, duration));
  meta.appendChild(el('span', 'tag', n.mode || 'raw'));
  row.appendChild(meta);

  row.addEventListener('click', function (ev) {
    ev.preventDefault();
    closeSidebarOverlay();
    openNote(n.name);
  });
  return row;
}

function renderNotes(notes) {
  var list = $('notes-list');
  list.innerHTML = '';

  var empty = !allNotes.length;
  $('notes-empty').hidden = !empty;
  list.hidden = empty;
  if (empty) return;

  if (!notes.length) {
    list.appendChild(el('div', 'side-note', 'no note matches that.'));
    return;
  }

  var day = null;
  notes.forEach(function (n) {
    var key = dayKey(n.created);
    if (key !== day) {
      day = key;
      list.appendChild(el('div', 'day', dayLabel(n.created)));
    }
    list.appendChild(noteRow(n));
  });

  // A re-render loses the selection; the open note keeps it.
  if (currentNote && currentNote.name) highlightNote(currentNote.name);
}

function highlightNote(name) {
  var rows = $('notes-list').children;
  for (var i = 0; i < rows.length; i++) {
    if (!rows[i].dataset || rows[i].dataset.name === undefined) continue;
    var active = rows[i].dataset.name === name;
    rows[i].classList.toggle('is-active', active);
    if (active) rows[i].setAttribute('aria-current', 'true');
    else rows[i].removeAttribute('aria-current');
  }
}

/* The editor holds edits the daemon has not seen yet. */
function isDirty() {
  if (!currentNote || viewingVersion !== null) return false;
  return $('note-editor').value !== loadedText;
}

/* The raw pane is an editor too: its text is what Regenerate will read. */
function rawDirty() {
  if (!currentNote) return false;
  return $('note-raw').value !== loadedTranscript;
}

/* Returns false when the user chose to keep unsaved edits in the note editor.
 * A version preview repaints that editor and nothing else, so it asks with this one:
 * asking about the raw pane there would prompt for edits that are in no danger. */
function confirmDiscardNote() {
  if (!isDirty()) return true;
  return window.confirm('This note has unsaved edits. Discard them?');
}

/* The same question for the paths that can cost the raw pane's edits as well. */
function confirmDiscard() {
  if (!isDirty() && !rawDirty()) return true;
  return window.confirm('This note has unsaved edits. Discard them?');
}

/* Make good on that prompt for callers that do not reload the editor anyway. */
function revertEditor() {
  if (!currentNote || viewingVersion !== null) return;
  $('note-editor').value = loadedText;
  $('note-raw').value = loadedTranscript;
  updateSaveState();
  updateRawSaveState();
  updateRawStatus();
}

function updateSaveState() {
  $('note-save').disabled = processing || viewingVersion !== null || !isDirty();
}

/* loadNote() repaints #note-raw from the server, so a save/restore of the *note*
 * would silently drop an unsaved transcript edit. Stash it before, put it back after. */
function stashRawEdit() {
  return rawDirty() ? $('note-raw').value : null;
}

function restoreRawEdit(text) {
  if (text === null || !currentNote) return;
  $('note-raw').value = text;
  updateRawSaveState();
  updateRawStatus();
}

function updateRawSaveState() {
  $('note-raw-save').disabled = processing || viewingVersion !== null || !rawDirty();
}

/* The standing marker on the raw pane's status line: whether the transcript on
 * disk is still Whisper's own words. A flash (copied/saved) puts it back after. */
function updateRawStatus() {
  var edited = !rawDirty() && !!(currentNote && currentNote.transcript_edited);
  $('note-raw-status').textContent = edited ? 'edited' : '';
}

function flashRaw(text) {
  flash($('note-raw-status'), text);
  window.setTimeout(updateRawStatus, 2100);   // flash() blanks the line at 2 s
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
  $('version-select').disabled = processing || !haveVersions;
  $('version-restore').disabled = processing || !previewing;

  // Single owner of the editor's read-only state: an old version on screen or a
  // write in flight both mean "what you type here would be lost".
  editor.readOnly = processing || previewing;
  $('note-raw').readOnly = processing || previewing;
  editor.classList.toggle('viewing-old', previewing);
  setTitleEditable(titleEditable && have && !processing && !previewing);

  // The list stays browsable during a write: the load-generation guard discards a late
  // reply's DOM writes, and the server keeps the new version for when the note is reopened.

  updateSaveState();
  updateRawSaveState();
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
  var sel = $('version-select');
  sel.innerHTML = '';
  viewingVersion = null;
  haveVersions = !!(versions && versions.length);
  if (!haveVersions) {
    addOption(sel, '', 'no history');
    updateProcessState();
    return;
  }
  var firstN = versions[0].n;
  for (var i = versions.length - 1; i >= 0; i--) {
    var v = versions[i];
    addOption(sel, String(v.n), versionLabel(v, v.n === firstN));
  }
  sel.value = String(currentVersionN(versions));
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
  var keepRaw = stashRawEdit();   // ... and so does the one after a restore
  setProcessing(true);
  status.textContent = 'restoring v' + n + '…';
  try {
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/restore', { n: n });
    if (!stillCurrent(token)) return;
    viewingVersion = null;          // loadNote() must not think edits are pending
    await loadNote(token.name);
    await loadNotes();
    if (currentNote && currentNote.name === token.name) {
      restoreRawEdit(keepRaw);
      flash($('note-save-status'), 'restored v' + n + ' as v' + (data.version || '?'));
    }
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

/* The note on the stage ---------------------------------------------------- */

/* "# Title" -> "Title", for a note that has a heading line at all. */
function headingOf(text) {
  var first = String(text || '').replace(/^\s+/, '').split('\n', 1)[0];
  var m = /^#[ \t]+(.+)$/.exec(first);
  return m ? m[1].trim() : null;
}

/* Rewrite that heading line in place; null when the note has none. */
function withHeading(text, title) {
  var s = String(text || '');
  var nl = s.indexOf('\n');
  var first = nl === -1 ? s : s.slice(0, nl);
  if (!/^[ \t]*#[ \t]+/.test(first)) return null;
  return '# ' + title + (nl === -1 ? '' : s.slice(nl));
}

function setTitleEditable(on) {
  var elx = $('note-title');
  elx.contentEditable = on ? 'true' : 'false';
  elx.setAttribute('contenteditable', on ? 'true' : 'false');
  if (on || !currentNote) {
    elx.title = '';
  } else if (!titleEditable) {
    elx.title = 'this note has no heading line — dictation notes are plain text, ' +
      'so the title comes from the folder';
  }
}

/* The title is the note's first "# " line: editing one edits the other. */
function onTitleInput() {
  if (!currentNote || !titleEditable || processing || viewingVersion !== null) return;
  var title = $('note-title').textContent.replace(/\s+/g, ' ').trim();
  var rewritten = withHeading($('note-editor').value, title);
  if (rewritten === null) return;
  $('note-editor').value = rewritten;
  updateSaveState();
}

/* The calm twin of #note-warning: no note.md and nothing failed, so the recording
 * was simply never handed to the LLM. CSS shows the hint; Regenerate is ready. */
function setProcessed(on) {
  if (on) delete $('note').dataset.processed;
  else $('note').dataset.processed = 'no';
}

function setRaw(shown) {
  $('note').dataset.raw = shown ? 'shown' : 'hidden';
  $('note-raw-toggle').textContent = shown ? 'Hide' : 'Show';
}

/* The stack-into-tabs breakpoint from style.css. Unknown (no matchMedia) = not a phone. */
function isPhoneLayout() {
  try {
    return !!(window.matchMedia && window.matchMedia('(max-width: 860px)').matches);
  } catch (e) {
    return false;
  }
}

/* Phone only (the strip is hidden above 860px): which pane the stack shows. */
function applyNoteTab(name) {
  var note = $('note');
  ['note', 'raw', 'audio'].forEach(function (t) {
    var on = t === name;
    note.classList.toggle('tab-' + t, on);
    var tab = $('note-tab-' + t);
    tab.classList.toggle('is-active', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

function pickNoteTab(name) {
  applyNoteTab(name);
  if (name === 'note') setRaw(false);
  else if (name === 'raw') setRaw(true);
}

/* Everything the stage shows about a note but its text. */
function renderNoteMeta(data) {
  var raw = data.meta || {};
  var box = $('note-meta');
  box.innerHTML = '';

  var created = data.created || raw.created;
  if (created) box.appendChild(el('span', null, dayLabel(created) + ' ' + fmtTime(created)));

  var duration = (data.duration_s !== undefined && data.duration_s !== null) ? data.duration_s
    : (raw.audio_duration_s !== undefined ? raw.audio_duration_s : raw.recording_duration_s);
  var d = fmtDuration(duration);
  if (d) box.appendChild(el('span', null, d));

  var mode = data.mode || raw.cleanup_mode;
  box.appendChild(el('span', null, mode || 'raw'));

  var backend = data.backend || raw.cleanup_backend;
  if (backend) {
    box.appendChild(el('span', null,
      raw.cleanup_model ? backend + ' · ' + raw.cleanup_model : backend));
  }
  if (raw.whisper_model) {
    box.appendChild(el('span', null,
      raw.language ? raw.whisper_model + ' · ' + raw.language : raw.whisper_model));
  }
  var timings = [];
  if (raw.transcribe_seconds !== undefined && raw.transcribe_seconds !== null) {
    timings.push('transcribe ' + raw.transcribe_seconds + 's');
  }
  if (raw.cleanup_seconds !== undefined && raw.cleanup_seconds !== null) {
    timings.push('clean ' + raw.cleanup_seconds + 's');
  }
  if (timings.length) box.appendChild(el('span', null, timings.join(' · ')));
}

/* Click path: asks before throwing unsaved edits away. */
async function openNote(name) {
  if (!name) return;
  if (takeActive()) return;    // the stage belongs to the running take
  if (!confirmDiscard()) return;
  await loadNote(name);
}

/* Leave the note stage without opening another one. */
function closeNote() {
  currentNote = null;
  loadedText = '';
  loadedTranscript = '';
  titleEditable = false;
  viewingVersion = null;
  haveVersions = false;
  $('note-editor').value = '';
  $('note-title').textContent = '';
  $('note-meta').innerHTML = '';
  $('note-raw').value = '';
  $('note-warning').hidden = true;
  setProcessed(true);
  $('note-save-status').textContent = '';
  $('note-raw-status').textContent = '';
  $('note-copy-status').textContent = '';
  $('note-path-status').textContent = '';
  renderVersions([]);
  highlightNote(null);
  updateProcessState();
}

async function loadNote(name) {
  if (!name) return;
  say('');
  $('note-save-status').textContent = '';
  $('note-path-status').textContent = '';
  viewingVersion = null;
  var editor = $('note-editor');
  updateProcessState();            // drops any preview's read-only state
  highlightNote(name);
  setView('note');
  applyNoteTab('note');

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
    loadedTranscript = '';
    titleEditable = false;
    $('note-title').textContent = 'could not open ' + name;
    $('note-meta').innerHTML = '';
    $('note-meta').appendChild(el('span', null, errText(e)));
    editor.value = '';
    $('note-raw').value = '';
    $('note-audio').hidden = true;
    $('note-path').textContent = '';
    $('note-path').hidden = true;      // an empty bordered chip is not a path
    $('note-warning').hidden = true;
    setProcessed(true);
    renderVersions([]);
    setStage('note');
    updateProcessState();
    return;
  }
  if (gen !== noteGeneration) return;   // the user opened something else meanwhile

  currentNote = data;
  var raw = data.meta || {};

  // No note.md: the editor holds the transcript. Deliberate for a raw recording,
  // a failed cleanup when a mode was actually asked for (and never timed). The two
  // read very differently — one is a failure, the other is a note waiting to be made.
  var transcript = data.transcript || '';
  loadedText = data.note || transcript;
  var cleanupFailed = !data.note && !!transcript && !!raw.cleanup_mode &&
    (raw.cleanup_seconds === null || raw.cleanup_seconds === undefined);
  $('note-warning').hidden = !cleanupFailed;
  setProcessed(!!data.note || cleanupFailed);

  editor.value = loadedText;
  loadedTranscript = transcript;
  $('note-raw').value = transcript;
  updateRawStatus();

  // A note that was never processed *is* its transcript: open on that pane (PHASE10 C).
  if (!data.note && !cleanupFailed) {
    if (isPhoneLayout()) pickNoteTab('raw');
    else setRaw(true);
  }

  var heading = headingOf(loadedText);
  titleEditable = heading !== null;
  $('note-title').textContent = heading !== null ? heading : (data.title || data.name || name);

  renderNoteMeta(data);

  var audio = $('note-audio');
  if (data.audio_url) {
    audio.src = data.audio_url;
    audio.hidden = false;
  } else {
    audio.removeAttribute('src');
    try { audio.load(); } catch (e) { /* ignore */ }
    audio.hidden = true;
  }

  $('note-path').textContent = data.path || '';
  $('note-path').hidden = !data.path;

  renderVersions(data.versions || []);
  // A note that was never processed has no mode of its own: offer what the record
  // panel is set to, and the daemon's default when that pick is "raw" (not a mode).
  var pick = $('pick-mode').value;
  selectIfPresent($('regenerate-mode'), data.mode || raw.cleanup_mode ||
                  (pick && pick !== 'raw' ? pick : defaultMode));
  updateProcessState();
  setStage('note');
}

/* Manual edit -> a new version. */
async function saveNote() {
  if (!currentNote || !currentNote.name || viewingVersion !== null || processing) return;
  var status = $('note-save-status');
  var token = noteToken();
  var name = token.name;
  var text = $('note-editor').value;
  var keepRaw = stashRawEdit();   // the reload below repaints the raw pane from the server
  setProcessing(true);
  status.textContent = 'saving…';
  try {
    var data = await putJSON('/api/notes/' + encodeURIComponent(name) + '/note',
                             { text: text });
    if (!stillCurrent(token)) return;
    loadedText = text;              // the daemon has it: nothing is pending any more
    await loadNote(name);
    await loadNotes();
    if (currentNote && currentNote.name === name) {
      restoreRawEdit(keepRaw);
      flash($('note-save-status'),
            'saved ' + fmtTime(new Date().toISOString()) + ' · v' + (data.version || '?'));
    }
  } catch (e) {
    if (stillCurrent(token)) status.textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

/* The raw transcript the user edited -> transcript.txt. Not a version: note.md is
 * untouched, and the next Regenerate reads the edit. */
async function saveTranscript() {
  if (!currentNote || !currentNote.name || viewingVersion !== null || processing) return;
  var token = noteToken();
  var text = $('note-raw').value;
  setProcessing(true);
  $('note-raw-status').textContent = 'saving…';
  try {
    await putJSON('/api/notes/' + encodeURIComponent(token.name) + '/transcript', { text: text });
    if (!stillCurrent(token)) return;
    loadedTranscript = text;              // the daemon has it: nothing is pending any more
    currentNote.transcript = text;
    currentNote.transcript_edited = true;
    flashRaw('saved');
  } catch (e) {
    if (stillCurrent(token)) $('note-raw-status').textContent = errText(e);
  } finally {
    setProcessing(false);
  }
}

/* Re-process ---------------------------------------------------------------
 * Regenerate re-runs cleanup from the raw transcript; revise applies an
 * instruction to the current note. Both write a new version. */

/* Apply a {title, note, version} reply to the stage. */
async function applyProcessed(data, token) {
  if (!stillCurrent(token)) return;   // the user is looking at another note now

  // The reply is the newest version, whatever was on screen before it landed.
  viewingVersion = null;
  loadedText = data.note || '';
  $('note-editor').value = loadedText;
  $('revise-instructions').value = '';

  // Refetch: the row's title, #note-meta and the version list all moved.
  await loadNote(token.name);
  await loadNotes();
  // loadNote() moved the generation on, so match on the name alone here.
  if (currentNote && currentNote.name === token.name) {
    say('done — v' + (data.version || '?'));
  }
}

async function regenerateNote() {
  if (!currentNote || !currentNote.name || processing || viewingVersion !== null) return;
  // Regenerate reads transcript.txt, not the pane: an unsaved edit would be ignored,
  // so offer to save it rather than quietly re-running the old words.
  if (rawDirty()) {
    if (!window.confirm('The transcript edit is unsaved. Save it and regenerate from it?')) return;
    await saveTranscript();
    if (!currentNote || !currentNote.name || processing || rawDirty()) return;  // the save failed; it said why
  }
  if (!confirmDiscard()) return;
  var token = noteToken();
  setProcessing(true);
  say('regenerating…');
  try {
    var body = { mode: $('regenerate-mode').value };
    var backend = $('pick-backend').value;
    if (backend) body.backend = backend;
    var instructions = $('revise-instructions').value.trim();
    if (instructions) body.instructions = instructions;
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/reclean', body);
    await applyProcessed(data, token);
  } catch (e) {
    if (stillCurrent(token)) say(errText(e));
  } finally {
    setProcessing(false);
  }
}

async function reviseNote() {
  if (!currentNote || !currentNote.name || processing || viewingVersion !== null) return;
  var instructions = $('revise-instructions').value.trim();
  if (!instructions) return;
  if (!confirmDiscard()) return;
  var token = noteToken();
  setProcessing(true);
  say('revising…');
  try {
    var body = { instructions: instructions };
    var backend = $('pick-backend').value;
    if (backend) body.backend = backend;
    var data = await postJSON(
      '/api/notes/' + encodeURIComponent(token.name) + '/revise', body);
    await applyProcessed(data, token);
  } catch (e) {
    if (stillCurrent(token)) say(errText(e));
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

/* "default_mode" -> "Default mode": the design's row name, from the key itself. */
function settingName(key) {
  var words = String(key).replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

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
      addOption(control, String(choice), String(choice));
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
    control.className = 'mono';
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
  var tbody = $('settings-table');
  tbody.innerHTML = '';

  settings.forEach(function (setting) {
    var tr = document.createElement('tr');

    var nameCell = document.createElement('td');
    nameCell.appendChild(el('strong', null, settingName(setting.key)));
    nameCell.appendChild(el('code', null, setting.env || setting.key.toUpperCase()));
    tr.appendChild(nameCell);

    tr.appendChild(el('td', null, setting.description || ''));

    var valueCell = document.createElement('td');
    var why = null;
    if (setting.editable === false) {
      why = 'set ' + (setting.env || setting.key.toUpperCase()) + ' and restart the daemon';
    } else if (setting.source === 'env') {
      why = 'overridden by ' + (setting.env || setting.key.toUpperCase());
    }

    var control = null;
    if (why) {
      var value = setting.value === null || setting.value === undefined ? '' : String(setting.value);
      valueCell.appendChild(el('code', null, value));
      valueCell.appendChild(el('span', 'why', why));
    } else {
      control = makeControl(setting);
      valueCell.appendChild(control);
    }
    tr.appendChild(valueCell);

    var sourceCell = document.createElement('td');
    var source = setting.source || 'default';
    sourceCell.appendChild(el('span', source === 'env' ? 'badge badge-env' : 'badge', source));
    tr.appendChild(sourceCell);

    tbody.appendChild(tr);
    settingsRows.push({ setting: setting, control: control });
  });
}

function changedSettings() {
  var payload = {};
  var count = 0;
  settingsRows.forEach(function (row) {
    if (!row.control || row.control.disabled) return;
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
    flash(status, 'saved · restart the daemon for model changes');
  } catch (e) {
    status.textContent = errText(e);
  } finally {
    btn.disabled = false;
  }
}

/* Vocabulary -------------------------------------------------------------- */

function vocabCount() {
  return $('vocab').value.split('\n').filter(function (line) { return line.trim(); }).length;
}

async function loadVocab() {
  var status = $('vocab-status');
  status.textContent = 'loading…';
  try {
    var data = await getJSON('/api/vocab');
    $('vocab').value = data.text || '';
    status.textContent = vocabCount() + ' entries';
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
    flash(status, 'saved ' + fmtTime(new Date().toISOString()) + ' · ' + vocabCount() + ' entries');
  } catch (e) {
    status.textContent = errText(e);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------- wiring */

function typingInAField(elx) {
  if (!elx) return false;
  var tag = (elx.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'button') return true;
  return !!elx.isContentEditable;
}

/* New note: the idle stage, and nothing of the last note left on it. */
function newNote() {
  if (takeActive()) return;
  if (!confirmDiscard()) return;
  // The audio behind #retry lives in this tab and nowhere else.
  if (lastUpload && !window.confirm('Discard the recording that failed to upload?')) return;
  lastUpload = null;
  retryDetail('');
  closeSidebarOverlay();
  revertEditor();
  closeNote();
  clearBanners();
  say('');
  setView('note');
  setStage('idle');
  resetLivePane();
  recElapsedMs = 0;
  paintTimer();
  setLiveView($('live-toggle').checked);
  setTransport('ready');
}

function wire() {
  $('sidebar-toggle').addEventListener('click', toggleSidebar);
  $('new-note').addEventListener('click', newNote);
  $('notes-search').addEventListener('input', function () { renderNotes(filteredNotes()); });
  $('nav-settings').addEventListener('click', function () {
    setView(currentView() === 'settings' ? 'note' : 'settings');
    closeSidebarOverlay();
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

  ['pick-mode', 'pick-backend', 'pick-language'].forEach(function (id) {
    $(id).addEventListener('change', savePicks);
  });
  $('live-toggle').addEventListener('change', function () {
    savePicks();
    if (!takeActive()) setLiveView($('live-toggle').checked);
  });
  $('process-toggle').addEventListener('change', savePicks);

  $('live-copy').addEventListener('click', function () {
    copyText(liveText(), $('live-copy-status'));
  });

  $('note-title').addEventListener('input', onTitleInput);
  // An <h1> the browser may fill with <div>s and <br>s is not a heading line:
  // Enter commits the edit, and a paste arrives as text.
  $('note-title').addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    ev.currentTarget.blur();
  });
  $('note-title').addEventListener('paste', function (ev) {
    if (!ev.clipboardData) return;
    ev.preventDefault();
    var text = String(ev.clipboardData.getData('text/plain') || '').replace(/\s+/g, ' ').trim();
    if (!text) return;
    var inserted = false;
    try { inserted = document.execCommand('insertText', false, text); } catch (e) { inserted = false; }
    if (!inserted) $('note-title').textContent = ($('note-title').textContent + text).trim();
    onTitleInput();
  });
  $('note-editor').addEventListener('input', updateSaveState);
  $('note-save').addEventListener('click', saveNote);
  $('note-copy').addEventListener('click', function () {
    var token = noteToken();
    copyText($('note-editor').value, $('note-copy-status'),
             function () { return stillCurrent(token); });
  });
  $('note-raw').addEventListener('input', function () {
    updateRawSaveState();
    updateRawStatus();
  });
  $('note-raw-save').addEventListener('click', saveTranscript);
  $('note-raw-copy').addEventListener('click', function () {
    var token = noteToken();
    copyText($('note-raw').value, $('note-raw-status'),
             function () { return stillCurrent(token); }, flashRaw);
  });
  $('note-raw-toggle').addEventListener('click', function () {
    setRaw($('note').dataset.raw !== 'shown');
  });
  ['note', 'raw', 'audio'].forEach(function (name) {
    $('note-tab-' + name).addEventListener('click', function () { pickNoteTab(name); });
  });

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
    if (!confirmDiscardNote()) {
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

  // Notes made elsewhere while this tab was in the background.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refreshNotesIfIdle();
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
  syncSidebarToggle();
  initProcessToggle();
  initLiveToggle();
  setStage('idle');
  setTransport('ready');
  setRaw(true);           // the drawer is open by default, and owns its own label
  renderVersions([]);
  updateProcessState();   // nothing is open yet: save/regenerate/revise stay off
  paintTimer();
  boot();
  scheduleHealth();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
