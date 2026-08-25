# vnote — User Guide

Complete reference for vnote: the web UI, the CLI, the daemon and its HTTP API, and every
setting. For a quick "get running" path, start with the [README](../README.md); this guide
is everything that page leaves out.

- [Install from a clone](#install-from-a-clone)
- [First run & setup](#first-run--setup)
- [Web UI](#web-ui)
- [`vnote` — command reference](#vnote--command-reference)
- [Cleanup modes](#cleanup-modes)
- [Warm daemon](#warm-daemon)
- [Custom vocabulary](#custom-vocabulary)
- [Settings reference](#settings-reference)
- [HTTP API](#http-api)
- [Config file & paths](#config-file--paths)
- [Development & testing](#development--testing)

---

## Install from a clone

The `uv tool install` route in the README puts `vnote` on your PATH globally. If you'd
rather hack on the code, install from a clone instead:

```bash
uv sync                       # creates .venv with deps
uv pip install -e .           # installs the `vnote` command into the venv
uv pip install -e '.[claude]' # optional: Anthropic SDK for `--backend claude` (the
                              # metered API; `--backend claude-code` needs nothing)
```

The `vnote` command then lives inside the project's `.venv`, so invoke it as
**`uv run vnote …`** (uv handles the venv), or `source .venv/bin/activate` first and
call `vnote` directly. Every example below uses the bare `vnote` form.

**Audio for the CLI recorder on WSL:** WSL has no native ALSA device, so `vnote` records
through WSLg's PulseAudio bridge via `parec` — `sudo apt install -y pulseaudio-utils`.
`pw-record`, `ffmpeg`, or the `sounddevice` library are used as fallbacks if present.
The **web UI needs none of this** — the browser owns the microphone.

---

## First run & setup

The first time you run `vnote` interactively it asks which cleanup backend to use and,
for Ollama, which model size (pre-selected from your detected GPU memory). Your choice is
saved to `~/.config/vnote/config.json` — the same file the web UI's Settings page edits.

The backend menu offers three options, and pre-selects **Claude Code** when that CLI is on
your PATH (Ollama otherwise, so a machine without it is never steered somewhere it can't
go):

| Backend | Auth | Notes |
|---|---|---|
| `claude-code` | your Claude **subscription** | Best quality, no API key, nothing to download. Needs the [Claude Code CLI](https://claude.com/product/claude-code); run `claude` once to sign in. |
| `ollama` | none | Private, offline, free. One-time model download. |
| `claude` | `ANTHROPIC_API_KEY` | Same models, **billed per token**. Needs the `[claude]` extra. |

- `vnote --setup` re-runs the chooser; the web UI's Settings page does the same job with
  descriptions next to every setting.
- Override any choice with a flag or a `VNOTE_*` environment variable
  ([Settings reference](#settings-reference)).
- The prompt is skipped when input isn't a terminal, so scripts and pipelines never block.

Local cleanup models (pull whichever you chose):

```bash
ollama pull qwen2.5:14b-instruct   # default; ~10 GB VRAM
ollama pull qwen2.5:7b-instruct    # lighter
ollama pull llama3.2:3b            # lightest / fastest — also a good `dictation_model`
```

---

## Web UI

The daemon serves a single page at **http://127.0.0.1:8760**. Nothing else to install,
no build step, and the page never talks to anything but the daemon.

The page is a **sidebar and a stage**: the sidebar lists your notes by day (with a search
box and **New note**), and the stage is either the current recording — the live transcript
fills it — or the note you opened, with the processed Markdown beside its raw transcript.
**Settings** lives at the bottom of the sidebar. Dark mode is the primary look; light
follows your system preference.

```bash
vnote --serve --open        # load the models once, open the page in your browser
vnote --serve               # same, without launching a browser — open the URL yourself
```

- **WSL2:** run the daemon inside WSL and open the page in your **Windows** browser —
  `localhost` / `127.0.0.1` reach the WSL daemon with no setup. `--open` uses the Windows
  default browser from WSL.
- The microphone needs a *secure context*, which `http://localhost` and `http://127.0.0.1`
  are. If you point `VNOTE_DAEMON_HOST` somewhere else, the page tells you recording won't
  work there.
- Keep it running: see [Warm daemon → start it automatically](#start-it-automatically).

### Record

1. Pick the **mode** (light / edit / summary / dictation / raw — see
   [Cleanup modes](#cleanup-modes)), the **backend**, and optionally a **language** code
   (blank = auto-detect). Defaults come from your settings; the page remembers your last
   picks.
2. **Record** → talk → **Pause** / **Resume** whenever you need to think (Space does the
   same while a recording is active) → **Stop**.
3. The audio goes to the daemon, which transcribes, cleans, and writes a note folder —
   exactly what the CLI does. There is no progress bar (the daemon can't report one); a
   long recording with a big cleanup model can take a minute.
4. The result appears with a **Copy** button (Markdown), a warning if cleanup failed and
   you're looking at the raw transcript, and an **open in Notes** shortcut.

**Process on stop** (on by default): turn it off and Stop writes a *raw* note — audio and
transcript, no LLM — whatever the mode says. The note opens on the raw transcript with a
quiet "not processed" line and the Regenerate controls ready, so you can fix the transcript
first and process it when you like. Your mode pick is remembered and is what Regenerate
offers. Like the other picks, the toggle is per browser.

**Live transcript** (the "Live transcript" toggle, on by default where the browser supports
it): the words appear as you speak. Settled text stays put — you can select and copy it
mid-sentence, while paused, and while the daemon is processing after Stop — and the last
few words render dimmer while they may still change. In live mode the browser streams raw
audio to the daemon, which keeps it; on Stop the daemon transcribes the whole recording in
one pass (the live text is a preview — the final transcript reads better across pauses)
and writes the note, so there is nothing to upload and a browser crash mid-recording loses
nothing: an abandoned live session is saved as `voice-notes/failed/live-<stamp>.wav`
after 30 minutes. Live recordings are saved as `audio.wav`.

With live transcript off, the recording is saved as `audio.webm` (Chrome / Edge / Firefox)
or `audio.mp4` (Safari) and uploaded on Stop. Either way the note folder holds the audio,
`transcript.txt`, `note.md` and `meta.json` (plus `transcript.original.txt` once you edit
the transcript — see [Notes](#notes)).

### Notes

Every note folder under `voice-notes/`, newest first — title, date, duration, mode. Open
one to:

- **Read and edit** the processed note (it's Markdown in a plain editor) and **Save** —
  every save is a new version.
- **Play** the audio (seeking works), **Copy** the note, unfold the raw transcript.
- **Edit the raw transcript** and **Save transcript** — mishearings, names, a sentence you
  want the model to see differently. The pane says "edited" once you have saved an edit,
  and Whisper's own output is kept as `transcript.original.txt` the first
  time you save (written once, never overwritten). The next **Regenerate** reads your
  edit. Saving a transcript is not a note version: `note.md` is untouched.
- **Regenerate** it from the raw transcript in another mode — the same thing as
  `vnote --redo` — or **Revise** the note as it stands. Both read the one
  **Instructions** box ("make it shorter", "turn the second half into a checklist"):
  Regenerate appends it to the cleanup prompt, Revise applies it to the current note.
- Pick an older **version** from the dropdown to read it, and **Restore** it (which is
  itself a new version — nothing is ever destroyed). On disk: `versions/note-<n>.md` and
  a `versions` list in `meta.json` recording when, which operation, mode, backend and
  instruction produced each one.
- **Open folder** (best-effort: Explorer from WSL, `xdg-open` on Linux, `open` on macOS),
  with the path shown and copyable as the fallback.

### Settings

Every setting with its description, current value, and where that value comes from
(`env` / `file` / `default`). Editable rows save to `~/.config/vnote/config.json`, which the
CLI reads too. Two kinds of rows can't be edited here:

- **overridden by `VNOTE_…`** — an environment variable wins over the file; unset it to
  edit here.
- **set `VNOTE_…` and restart the daemon** — bound when the daemon starts (Whisper model,
  notes folder, address, vocabulary path).

The **Vocabulary** box edits your [custom vocabulary](#custom-vocabulary) file; changes
apply to the next recording, no restart.

### Restyling the page

`vnote/web/index.html` is markup only — no JavaScript. `vnote/web/app.js` finds its
elements by id (the full list is the comment at the top of that file) and drives the
look through a few data attributes (`data-view`, `data-state`, `data-live`, `data-raw`,
`data-daemon`) that the CSS reacts to. The shipped page is the Claude Design handoff
recorded under `docs/design/handoff/` (layout "stage + drawer"; `HANDOFF.md` there is
the design spec), minus its web-font import — the page makes no external requests. Any
page that keeps the ids and attributes can replace it as is.

---

## `vnote` — command reference

```bash
vnote                      # record from the mic: Space pauses/resumes, Enter stops
vnote memo.m4a             # process an existing audio file
```

While recording, the timer counts only recorded time — paused audio is discarded, and
resume is gap-free. Enter (or `q`) stops. When stdin isn't a terminal (a pipe or a
script) it's Enter-only.

| flag | effect |
|---|---|
| `--light` | faithful cleanup — de-fill + grammar only |
| `--edit` | editorial cleanup — reorganize into headings/lists (the built-in default) |
| `--summary` | condensed rewrite |
| `--dictation` | plain text from a small fast model — no title, no structure |
| `--raw` | transcript only, no LLM |
| `--backend {ollama,claude-code,claude}` | cleanup backend — see [First run & setup](#first-run--setup) |
| `--model NAME` | override the cleanup model name |
| `--instructions TEXT` | extra instructions for the cleanup ("bullet points only"); also with `--redo` |
| `--language CODE` | force transcription language (e.g. `en`); default: the saved `language` setting, else auto-detect |
| `--no-clipboard` | don't touch the clipboard |
| `--stdout` | also print the note to stdout (for piping) |
| `-o`, `--open` | open the new note in `$EDITOR` afterward — with `--serve`: open the web UI in your browser |
| `--redo PATH` | re-run cleanup on a saved note, skipping transcription |
| `--keep-temp-audio` | keep the temporary recording if writing the note fails (debugging) |
| `--serve` | run the warm daemon + web UI (see [Warm daemon](#warm-daemon)) |
| `--no-daemon` | ignore a running daemon; load models in-process for this run |
| `--doctor` | check recorder, GPU, clipboard, daemon and backend — with fixes — then exit |
| `--config` | print every setting, its value and its source, then exit |
| `--setup` | re-run the interactive first-run setup, then exit |
| `--version` | print the version |

The mode with no flag is the `default_mode` setting (`edit` unless you changed it).
`--redo` is handy for trying a different cleanup intensity without re-transcribing (the
slow part) — e.g. `vnote --redo voice-notes/2026-08-24-1033-… --summary`. Every re-run
(and every edit or revision in the web UI) is kept as a version — see
[Web UI → Notes](#notes).

**No GPU?** `--backend claude-code` runs cleanup through your Claude subscription and needs
no local model at all; transcription falls back to CPU automatically — slower, but it works.

---

## Cleanup modes

| mode | flag | what it does |
|---|---|---|
| light | `--light` | fixes fillers and grammar, keeps your wording and order |
| edit | `--edit` | reorganizes into headings, lists, and tidy paragraphs (built-in default) |
| summary | `--summary` | condenses to the key points |
| dictation | `--dictation` | plain text, no title or structure, on the small `dictation_model` — fast, for pasting into something else |
| raw | `--raw` | no LLM at all — just the Whisper transcript |

You can also dictate formatting instructions as you speak and the cleanup step follows
them: *"make that a bulleted list"*, *"put a heading here"*, *"scratch that"*.

---

## Warm daemon

Normally every CLI run loads the Whisper model into VRAM first — several seconds before
transcription even starts. Leave a daemon running and repeat runs skip that entirely; the
daemon is also what serves the [web UI](#web-ui):

```bash
vnote --serve              # terminal A: loads the model once, serves on 127.0.0.1:8760
vnote memo.m4a             # terminal B: detects the daemon, starts transcribing at once
```

- The page is up as soon as the daemon binds — Whisper (and, on the `ollama` backend, the
  LLM) load on a background thread behind it. While that runs the sidebar reads
  *"warming large-v3-turbo …"* and **Record still works**: the recording is held and
  transcribed as soon as the model lands. `GET /health` reports the same state
  (`warm`, `warm_error`, `ollama`); if the model never loads the sidebar says
  *"whisper failed: …"* instead of warming forever.
- The CLI probes for a daemon on every run and silently falls back to in-process models
  when none is up — same output, same files.
- `--no-daemon` forces in-process for a single run.
- `vnote --doctor` shows whether a daemon is up.
- `VNOTE_DAEMON_HOST` / `VNOTE_DAEMON_PORT` move the address.

It binds to localhost only, with no auth — a single-user convenience, not a network
service. Cleanup runs daemon-side, so the backend's needs (Ollama, the `claude` CLI, or the
`anthropic` package + key) live wherever the daemon runs; restart it after changing them.

### Start it automatically

- **Native Linux (systemd):**
  ```bash
  cp scripts/vnote-daemon.service ~/.config/systemd/user/
  systemctl --user enable --now vnote-daemon
  ```
- **WSL2, at Windows logon:** Task Scheduler → new task → *Action:*
  `wsl.exe -d <YourDistro> -- ~/.local/bin/vnote --serve`, *Trigger:* at log on, tick
  **Hidden**. (Or just leave a terminal running `vnote --serve`.)
- **macOS / native Windows:** leave a terminal running `vnote --serve`.

---

## Custom vocabulary

Bias transcription toward your spellings and fix known mistakes, in
`~/.config/vnote/vocab.txt` (path shown by `vnote --config`; override with `VNOTE_VOCAB`).
Edit it in the web UI's Settings page or any editor. Applies to **all** transcription paths
and needs **no restart**:

```
TRANSFORM              # bare line: bias transcription toward this spelling
Dymola
jason -> JSON          # correction: fix the transcript afterwards (whole-word)
v note -> vnote
```

- A **bare line** is a hotword — it nudges Whisper toward that spelling.
- An **`a -> b`** line is a post-transcription whole-word correction.

---

## Settings reference

One list drives the web UI's Settings page, `vnote --config`, and this table. Precedence
for the runtime settings: CLI flag > environment variable > `config.json` > built-in
default. A `.env` in the current directory is auto-loaded (see `.env.example`).

| setting | env var | default | what it does |
|---|---|---|---|
| `backend` | `VNOTE_BACKEND` | `ollama` | `ollama` (local, offline, free) · `claude-code` (your Claude subscription via the Claude Code CLI, no API key) · `claude` (Anthropic API, billed per token) |
| `default_mode` | `VNOTE_MODE` | `edit` | cleanup mode when none is picked: `light` · `edit` · `summary` · `dictation` |
| `language` | `VNOTE_LANGUAGE` | — | transcription language code (`en`, `de`, …); blank = auto-detect per recording |
| `ollama_model` | `VNOTE_OLLAMA_MODEL` | `qwen2.5:14b-instruct` | Ollama model for note cleanup (`ollama pull` it once) |
| `dictation_model` | `VNOTE_DICTATION_MODEL` | — | Ollama model for `dictation` mode — small and fast, e.g. `llama3.2:3b`; blank = same as `ollama_model` |
| `ollama_host` | `OLLAMA_HOST` | `http://127.0.0.1:11434` | where Ollama listens |
| `ollama_keep_alive` | `VNOTE_OLLAMA_KEEP_ALIVE` | `30m` | how long Ollama keeps the model loaded after a request — a duration with a unit (`30m`, `1h30m`), a bare number of seconds (`300`), or `-1` = until Ollama exits (Ollama's own default is `5m`) |
| `claude_model` | `VNOTE_CLAUDE_MODEL` | `claude-sonnet-5` | model for the `claude` (API) backend; `claude-code` uses the CLI's own choice |
| `claude_code_bin` | `VNOTE_CLAUDE_CODE_BIN` | `claude` | name or path of the Claude Code CLI |
| `whisper_model` | `VNOTE_WHISPER_MODEL` | `large-v3-turbo` | faster-whisper model loaded at daemon start (~1.6 GB on first use) — **restart to apply** |
| `notes_dir` | `VNOTE_DIR` | `./voice-notes` | where note folders are written — **restart to apply** |
| `daemon_host` | `VNOTE_DAEMON_HOST` | `127.0.0.1` | address the daemon binds (keep it on localhost — no auth) — **restart to apply** |
| `daemon_port` | `VNOTE_DAEMON_PORT` | `8760` | port the daemon listens on — **restart to apply** |
| `vocab` | `VNOTE_VOCAB` | `~/.config/vnote/vocab.txt` | the custom-vocabulary file (its *contents* apply without a restart) |

Plus `ANTHROPIC_API_KEY` — required for `--backend claude` only (**not** for `claude-code`).

---

## HTTP API

What the page talks to; handy for scripts too. JSON unless noted; errors are non-2xx with
`{"error": "..."}`.

| route | behaviour |
|---|---|
| `GET /` · `GET /static/<file>` | the page (`vnote/web/`) |
| `GET /health` | `{status, version, device, whisper_model, uptime_s, warm, warm_error, ollama}` — `warm` is false until Whisper is loaded, `warm_error` is null unless the load failed; `ollama` is `unknown` · `skipped` (backend is not Ollama) · `starting` · `ready` · `absent` |
| `GET /api/settings` · `PUT /api/settings` | the settings list ↔ `{key: value, …}` (editable keys only; 400 with a reason otherwise) |
| `GET /api/vocab` · `PUT /api/vocab` | `{"text": …}` ↔ the vocabulary file |
| `GET /api/notes` | newest-first `{"notes": [{name, title, created, duration_s, mode, backend, has_audio, has_note}]}` |
| `GET /api/notes/<name>` | the same fields plus `meta`, `note`, `transcript`, `transcript_edited`, `audio_url`, `path`, `versions` |
| `GET /api/notes/<name>/audio` | the audio file (`Range` supported) |
| `POST /api/note?format=webm&mode=…&backend=…&language=…&raw=0` | body = audio bytes → a finished note: `{name, title, note, transcript, meta, cleanup_error}` |
| `POST /api/notes/<name>/reclean` | `{mode, backend?, model?, instructions?}` → `{title, note, version}` — regenerate from the transcript |
| `POST /api/notes/<name>/revise` | `{instructions, backend?, model?}` → `{title, note, version}` — apply an instruction to the current note |
| `PUT /api/notes/<name>/note` | `{"text": …}` → `{version, title, note}` — save a manual edit |
| `PUT /api/notes/<name>/transcript` | `{"text": …}` → `{transcript, transcript_edited}` — rewrite `transcript.txt` (what Regenerate reads); the first write keeps Whisper's output as `transcript.original.txt`. Not a version. Empty text is allowed |
| `GET /api/notes/<name>/versions/<n>` | `{n, text, created, op, mode, backend, model, instructions, restored_from}` |
| `POST /api/notes/<name>/restore` | `{"n": …}` → `{title, note, version}` — restores that version as a new one |
| `POST /api/notes/<name>/reveal` | open the folder in the OS file manager (best-effort) → `{opened, path}` |
| `POST /transcribe` | JSON `{audio_path, language?}` (shared filesystem) or the audio as an `application/octet-stream` body with `?format=` → `{transcript, meta}` |
| `POST /clean` | `{transcript, mode?, backend?, model?, tone?, instructions?}` → `{title, body}` |
| `POST /revise` | `{note, instructions, backend?, model?}` → `{title, body}` |
| `POST /stream/start` | `{language?}` → `{session_id}` — opens a live session; the daemon keeps the audio |
| `POST /stream/append?sid=` | body = raw s16le 16 kHz mono PCM → `{partial, committed, tail, seconds}`, at once: a per-session worker transcribes the uncommitted tail and commits it at a silence boundary (or after 30 s), so the request never waits on the GPU |
| `POST /stream/ping?sid=` | `{ok: true}` — keeps a paused session alive (sessions expire 30 min after the last touch; an abandoned one's audio lands in `failed/live-*.wav`) |
| `POST /stream/cancel?sid=` | drop a live session and its audio (the user backed out) → `{cancelled: true}` |
| `POST /stream/finish?sid=` | → `{transcript, meta, live_transcript}` — one full pass over the whole recording (authoritative), with the live text alongside for comparison |
| `POST /stream/finish?sid=&note=1&mode=…&backend=…&model=…&language=…&raw=0` | the daemon-held audio → a finished note folder: the `/api/note` payload plus `live_transcript` (no second upload on stop) |

---

## Config file & paths

```bash
vnote --doctor             # check recorder, GPU, clipboard, daemon and backend — with fixes
vnote --config             # every setting, its value and its source
vnote --setup              # re-run the interactive first-run setup
```

- **Config:** `~/.config/vnote/config.json` (`$XDG_CONFIG_HOME/vnote/config.json`)
- **Vocabulary:** `~/.config/vnote/vocab.txt`
- **Notes:** `./voice-notes/` (or `VNOTE_DIR`)
- **Whisper model cache:** `~/.cache/huggingface`

---

## Development & testing

```bash
uv pip install -e '.[dev]'   # pytest + ruff
uv run python -m pytest -q   # unit tests (pure logic; no GPU/mic/network)
uv run ruff check vnote tests
node --check vnote/web/app.js  # if you touched the page
```

The unit tests cover the testable core — the pipeline (fake transcribe/clean functions),
the HTTP handlers (a real server on an ephemeral port, models faked), the settings
registry, the CLI recorder's capture loop, transcript parsing, slugging. The hardware paths
(mic capture, GPU transcription, Ollama/Claude calls) can't run in CI; smoke-test them
manually with the bundled public-domain clip:

```bash
uv run vnote .testdata/jfk.flac
```

Design notes live in `docs/planning/` — `PHASE8.md` is the web UI's spec and API contract.
