# Phase 9 — The real interface: live transcript, editing, history sidebar

> The research read, decisions and phasing for the Body's post-test feedback on the
> 0.5.0 web UI (2026-08-24). Decided the same day (grill): **readback-only** note view
> with a Continue-recording affordance designed in · the raw/processed **layout goes to
> Claude Design** as two explorations · **linear version history** for the note ·
> **full re-transcription on Stop** with the live text staying copyable. The design
> brief is `docs/design/claude-design-brief.md`.
>
> **Status:** Phase 1 (API foundations) built 2026-08-24 — versions, edit, revise,
> instructions (also `vnote --instructions`), reveal, `/stream/ping` + 30-min TTL, and
> the current page wired to them with the brief's ids. Phase 2 (daemon-side incremental
> streaming: `vnote/stream.py`, VAD commits, per-session worker, PCM spill,
> `/stream/finish?note=1`, abandoned audio → `failed/live-*.wav`) built the same day;
> Phase 3 (AudioWorklet capture + live pane in the current page) built. **Design chosen
> 2026-08-24: exploration 1a — stage + drawer**; the Claude Design handoff (index.html,
> style.css, HANDOFF.md) is recorded under `docs/design/handoff/`; Phase 4 = rewire
> `app.js` to it (state via `data-view` / `data-state` / `data-live` / `data-raw` /
> `data-daemon` attributes, as the handoff specifies) — **built and reviewed the same day;
> version 0.6.0**. Phase 5 not started.

## What the Body asked for (after recording a real session in the 0.5.0 page)

1. **Live transcript while recording** (optional), selectable/copyable while still
   talking, also while paused.
2. **On stop**: processing starts; the text stays copyable meanwhile.
3. **Raw vs processed** — two panes; layout open.
4. **Edit the processed note; re-process** with mode changes and free-text AI
   instructions ("make it longer"), iterating inside the app.
5. **Notes/history sidebar** on the far left; click opens the note in the normal view.
6. **All settings** surfaced somewhere sane.

Open questions: continue recording into an existing note? · open-folder button ·
raw/processed layout · what re-processing does to history.

Inspiration: Wispr Flow's own window — a left sidebar (history grouped by date with
a stats strip, Dictionary, Snippets, Styles, Settings) beside the dictation itself
([Wispr Flow help: navigating the app](https://docs.wisprflow.ai/articles/5096240724-navigating-the-wispr-flow-app-desktop-ios-and-android),
[Flow Styles](https://docs.wisprflow.ai/articles/2368263928-how-to-setup-flow-styles)).

## The read: what the code supports and what blocks each item

Numbers from the Body's own notes (`voice-notes/*/meta.json`, Aug 19–24): recordings
run **2.5–7 minutes**; the final transcription takes **13–35 s**; cleanup **1–15 s**
(llama3.2:3b). Whatever we build must be comfortable at 10 minutes.

| item | supports it today | blocks / bigger than it looks |
|---|---|---|
| Live transcript | `/stream/start` · `/stream/append` · `/stream/finish` exist, are tested, and were kept in 0.5.0 for exactly this. `_infer_lock` serializes the GPU. | **(a) Scaling.** `append` re-transcribes the *whole* buffer, synchronously inside the request, every ≥0.5 s of new audio (`server.py:_MIN_NEW_PCM`). Built for 5-second flow utterances. At 5 minutes one pass is ~20 s → requests pile up; unusable past ~60 s. Needs an incremental model: committed text + an uncommitted tail; commit the tail at a silence boundary (`faster_whisper.vad` is installed; the old `vnote/vad.py` with `speech_spans()` is one `git show HEAD:vnote/vad.py` away) or at ~30 s; re-transcribe only the tail (0.5–1.5 s on the 4090); run it on a per-session worker so `append` returns immediately. **This is the big lift.** **(b) Capture.** The page records with `MediaRecorder` (webm/opus); the stream endpoints want raw s16le 16 kHz PCM → an `AudioWorklet` capture path (`AudioContext({sampleRate: 16000})`, ~150 lines, no deps). **(c) Pause.** `_STREAM_TTL_S = 120` drops a session two minutes after its last chunk — a longer pause kills the live session. Keepalive or a 30-min TTL. **(d) Selection.** A `textarea.value = …` update resets the user's selection; the live pane needs a committed part that is stable DOM (paragraphs appended, never re-rendered) and only the tail node re-rendered. |
| Copy during processing | The stop path is one `POST /api/note`; nothing forces the page to clear the text. | Nothing. Cheap: keep the live pane, show state, let the result augment it. |
| Raw vs processed panes | Every response carries both `transcript` and `note`; the detail endpoint too. | Pure layout — belongs in the design brief. |
| Edit the processed note | `note.md` is a plain file; `write_session`/`reclean` write it. | No endpoint writes it from the page → `PUT /api/notes/<name>/note` (cheap). |
| Re-process with mode | `POST /api/notes/<name>/reclean {mode, backend}` exists; `vnote --redo` is the same code. | Overwrites `note.md` (marks `recleaned`) — see versioning. |
| Re-process with instructions | `cleanup._build_user_prompt(…, tone)` already appends a free-text suffix ("Write in a {tone} tone.") — the seam generalizes to `instructions` in ~10 lines. | Two different operations hide here: **regenerate** (raw transcript → note, new mode/instructions) vs **revise** (current note → edited note, e.g. "shorter"). Revise needs its own prompt (input = the note, same `TITLE/---` output contract) and endpoint. Cheap–medium. |
| Notes sidebar | `GET /api/notes` (newest-first, `created`, title, duration, mode) — enough to group by day like Wispr's history. | Layout only. |
| Settings | `/api/settings` with descriptions, sources, editability; vocabulary editor. | Placement only. |
| Continue recording into a note | Folder = `audio.*` + `transcript.txt` + `note.md` + `meta.json`; `_audio_file()` takes the first `audio.*` (note: `audio-2.webm` would sort *before* `audio.webm`). | A webm/opus archive can't be appended without ffmpeg. **If the daemon owns the PCM** (see design below) the archive is WAV and appending is raw concatenation + a header rewrite; then "continue" = append PCM, transcribe the new segment, append to the transcript with a timestamp line, offer re-clean. Medium once Phase 2 exists; large before it. |
| Open the folder | One folder per note, path known. | The daemon is in WSL, the browser on Windows: a `POST /api/notes/<name>/reveal` doing `explorer.exe $(wslpath -w …)` / `xdg-open` / `open` best-effort (same pattern as `--open`), plus the path shown with a Copy button as the honest fallback. Cheap. |

### Design consequence worth adopting: the daemon owns the audio

With live transcript on, the browser streams PCM to the daemon anyway. Let the daemon
*keep* it (spilled to a temp WAV as it arrives) and make `/stream/finish` write the
note (`?note=1&mode=…&backend=…`). Then: no second upload on stop; a browser crash
mid-recording loses nothing (the daemon has the audio so far); "continue recording"
becomes cheap; and every folder holds `audio.wav` (1.9 MB/min — a 7-minute note is
~13 MB; optional later: opus via PyAV, already a dependency). `MediaRecorder` +
`POST /api/note` stays as the path when live transcript is off or `AudioWorklet` is
unavailable — both end in `pipeline.make_note`.

## Decisions on the open questions (recommendations, adopted 2026-08-24)

1. **Continue recording?** v1 readback-only (transcript, note, meta, audio, open folder,
   re-process, edit). Design the note view with a "Continue recording" affordance so
   the layout doesn't change when it arrives; implement it after Phase 2, when it is a
   PCM append rather than a container problem. Tradeoff: "continue" is the feature that
   turns a note into a living document, but it forces the multi-take rules (how the
   transcript is joined, whether the old note is re-cleaned whole) — better decided
   after living with edit + revise.
2. **Open folder** — yes, fits storage as-is; ship the best-effort reveal + the path.
3. **Layout** — undecided on purpose: the brief asks Claude Design for both *stage +
   drawer* (live/raw is the stage while recording; the processed note takes over on
   completion with raw as a side pane / tab) and *always side by side*, at desktop and
   phone widths. We pick from pictures.
4. **Re-processing and history** — *linear versions*: `note.md` is current; each
   regenerate / revise / manual save snapshots the new text as `versions/note-<n>.md` and
   appends `{n, created, op, mode, backend, instructions}` to `meta.json`. Version picker +
   "restore" (which is itself a new version).
   Overwrite (today) destroys the Body's edits on re-run; branching is a tree UI
   nobody asked for.
5. **(Ours) Final transcript authority** — after stop, run the full one-pass
   transcription for the note (what the CLI does; +13–35 s for typical notes) while
   the live text stays on screen and copyable, with a "use the live transcript now"
   shortcut. The alternative — accept the committed live text as final — is instant
   but each segment was transcribed without the context across its boundaries.

## Phased plan (dependencies and cost)

**Phase 1 — foundations, independent of the design (cheap; ships with today's page).**
`PUT /api/notes/<name>/note` (save an edit → new version) · versions + `meta.versions`
on reclean/edit · `instructions` on reclean · `POST /api/notes/<name>/revise
{instructions}` with its own prompt · `POST /api/notes/<name>/reveal` + path in the
detail payload · stream TTL → 30 min + `/stream/ping` · the current page keeps the
text visible and copyable during processing. All unit-testable with fakes.

**Phase 2 — incremental streaming in the daemon (the big lift; pure Python).**
Committed/tail model · VAD commit (resurrect `vad.py`) · per-session worker, bounded
partial cost, drop-behind policy · PCM spilled to a temp WAV · `/stream/finish?note=1`
runs `make_note` on it · keepalive · tests with fake transcribe and synthetic PCM.
Nothing else needs it for short recordings, but every real note (2.5–7 min) does.

**Phase 3 — browser live capture + live pane (medium).** `AudioWorklet` PCM at 16 kHz →
`/stream/append` · live pane = committed paragraphs (stable DOM) + a re-rendered tail ·
selection/copy survive updates and pause · stop → finish → note · fallback to
`MediaRecorder` when live is off or the worklet is unavailable · the live toggle is a
per-browser pick remembered in localStorage with the other picks (not a daemon setting —
it depends on the browser's `AudioWorklet` support, so it belongs to the page) · the tab
keeps a safety copy of the PCM so a lost session can still be uploaded as a WAV note.
Depends on Phase 2 for anything beyond ~1 minute.

**Phase 4 — the new layout from Claude Design (medium; parallel with 2–3).** Sidebar
(history by day, search later) · stage + drawer · editor with version picker · revise
box · settings view. The PHASE8 id contract is the seam: `app.js` is rewired to the
new ids; the API contract (PHASE8 + Phase 1 additions) is the boundary the design
must respect.

**Phase 5 — later.** Continue recording (PCM append) · opus compaction via PyAV ·
keyboard shortcuts · search in the sidebar · stats strip.

Cheap: Phase 1, the sidebar and settings placement, the two-pane layout, the reveal
button. Medium: Phase 3, revise. Bigger than it looks: Phase 2 (streaming that
scales), continue-recording (multi-take semantics), selection-preserving live text.

## Contract additions (Phase 1 + Phase 2; the boundary the design must respect)

| route | behaviour |
|---|---|
| `PUT /api/notes/<name>/note` | `{"text": …}` → saves an edit as a new version (`op: "edit"`) |
| `POST /api/notes/<name>/reclean` | now also accepts `instructions` (free text appended to the cleanup prompt); result is a new version (`op: "regenerate"`) |
| `POST /api/notes/<name>/revise` | `{instructions, backend?, model?}` → the *current note* rewritten per the instruction (own prompt, same `TITLE/---` contract) → new version (`op: "revise"`) |
| `GET /api/notes/<name>/versions/<n>` · `POST /api/notes/<name>/restore {"n": …}` | read an old version; restore = a new version (`op: "restore"`) |
| `POST /api/notes/<name>/reveal` | best-effort open of the folder (`explorer.exe` via `wslpath -w` on WSL, `xdg-open`, `open`); the detail payload also carries `path` |
| `POST /stream/ping?sid=` · `_STREAM_TTL_S` → 30 min | pauses survive |
| `POST /stream/append` | returns `{committed, tail}`; work happens on a per-session worker; the request never blocks on the GPU |
| `POST /stream/finish?sid=&note=1&mode=&backend=&language=&raw=` | the daemon-held audio → full one-pass transcription → `make_note` → the same payload as `/api/note` (+ `live_transcript` for comparison) |

Versions on disk: `versions/note-<n>.md` holds **every** version, the current one included
(so a version is never reconstructed from anything else); `meta.json.versions = [{n, created,
op, mode, backend, model, instructions, restored_from}]`; `note.md` is a copy of the current
version's text. Pre-versions folders are migrated (v1 = their note.md) when first opened or
written.

The element-id contract for the new markup is the table in the brief; `app.js` is
rewired to it in Phase 4 (the 0.5.0 ids `tab-*`, `view-record`, `result*`,
`note-detail`, `note-text`, `reclean*` are superseded).
