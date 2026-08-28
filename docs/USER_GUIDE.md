# vnote — User Guide

Complete reference for vnote: the web UI, the CLI, the daemon and its HTTP API, and every
setting. For a quick "get running" path, start with the [README](../README.md); this guide
is everything that page leaves out.

- [Install from a clone](#install-from-a-clone)
- [First run & setup](#first-run--setup)
- [Web UI](#web-ui)
- [`vnote` — command reference](#vnote--command-reference)
- [Styles](#styles)
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

The backend menu offers four options and pre-selects whichever your machine can actually
run — **Claude Code** if that CLI is on your PATH, else **opencode** if *its* CLI is, else
**Ollama** — so a machine is never steered somewhere it can't go:

| Backend | Auth | Notes |
|---|---|---|
| `claude-code` | your Claude **subscription** | Best quality, no API key, nothing to download. Needs the [Claude Code CLI](https://claude.com/product/claude-code); run `claude` once to sign in. |
| `opencode` | whatever opencode already uses | Reuses your [opencode](https://opencode.ai) provider setup — a local MLX/llama.cpp server, or a hosted provider. No separate key for vnote. |
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
ollama pull llama3.2:3b            # lightest / fastest — a good `model:` line for the dictation style
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

1. Pick the **style** (the dropdown groups them by where they come from, plus `raw` for
   no LLM at all — see [Styles](#styles)), the **backend**, and optionally a **language**
   code (blank = auto-detect). The backend list starts with **style default**: leave it
   there and the style's own `backend:` line decides, falling back to the `backend`
   setting. Defaults come from your settings; the page remembers your last picks.
2. **Record** → talk → **Pause** / **Resume** whenever you need to think (Space does the
   same while a recording is active) → **Stop**.
3. The audio goes to the daemon, which transcribes, cleans, and writes a note folder —
   exactly what the CLI does. There is no progress bar (the daemon can't report one); a
   long recording with a big cleanup model can take a minute.
4. The result appears with a **Copy** button (Markdown), a warning if cleanup failed and
   you're looking at the raw transcript, and an **open in Notes** shortcut.

**Process on stop** (on by default): turn it off and Stop writes a *raw* note — audio and
transcript, no LLM — whatever the style says. The note opens on the raw transcript with a
quiet "not processed" line and the Regenerate controls ready, so you can fix the transcript
first and process it when you like. Your style pick is remembered and is what Regenerate
offers. Like the other picks, the toggle is per browser.

**Continue recording** (the button beside the version picker on an open note): records
another **take** into that note instead of making a new one. The stage becomes a recording
exactly as Record does — a strip above it says which note you are continuing — and a **How**
pick decides what the daemon does with the new take on Stop:

| How | What the model sees | What happens to the note |
|---|---|---|
| **continue** (default) | the note as read-only context plus the new transcript; it writes only the continuation | appended under a `---` rule |
| **append** | the new take's transcript alone | appended under a `---` rule |
| **merge** | the note and the new transcript together | the whole note rewritten |

Either way the take is its own folder — `takes/2/audio.wav` and `takes/2/transcript.txt` —
and the result is a new note version, so the note before the take is one **Restore** away.
Continuing a note that was never processed just adds a raw take; nothing is cleaned. The
How pick is remembered per browser like the other picks, and can still be changed while the
take is running. Only one recording can be bound to a note at a time: the button says so
while another tab (or the tray) is already continuing it.

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

Every note folder under `voice-notes/`, newest first — title, date, duration, style. Open
one to:

- **Read and edit** the processed note (it's Markdown in a plain editor) and **Save** —
  every save is a new version.
- **Play** the audio (seeking works) — one player per take — **Copy** the note, unfold the
  raw transcript.
- **Edit the raw transcript** and **Save** — mishearings, names, a sentence you want the
  model to see differently. The raw pane is one section per take (a note you never continued
  has exactly one, called simply "Transcript"; the others are headed `Take 2 · 14:44 · 2:40`),
  each with its own textarea and Save. A section says "edited" once you have saved an edit
  there, and Whisper's own output is kept as `transcript.original.txt` beside it the first
  time you save (written once, never overwritten). The next **Regenerate** reads your edit.
  Saving a transcript is not a note version: `note.md` is untouched.
- **Re-run** a take (the *how* pick beside it, then **Re-run**): the same three choices as
  Continue, applied to the note as it stands now — a new version, so nothing is lost. Use it
  when `append` should have been `merge`. To get back to the note as it was before that
  take, restore the version below it in the list instead.
- **Delete take** — its audio and transcript move to the trash folder and the joined
  transcript and duration are rebuilt. `note.md` is *not* touched (regenerate or edit it
  yourself), take numbers keep their gaps so the version history stays true, and the last
  remaining take cannot be deleted.
- **Regenerate** it from the raw transcript in another style — the same thing as
  `vnote --redo` — or **Revise** the note as it stands. With more than one take, the
  **include** checkboxes decide which takes that run reads (at least one); the takes
  themselves are never touched, only this run's input. Both read the one
  **Instructions** box ("make it shorter", "turn the second half into a checklist"):
  Regenerate appends it to the cleanup prompt, Revise applies it to the current note.
- Pick an older **version** from the dropdown to read it, and **Restore** it (which is
  itself a new version — nothing is ever destroyed). On disk: `versions/note-<n>.md` and
  a `versions` list in `meta.json` recording when, which operation, style, backend and
  instruction produced each one. (The field is still called `cleanup_mode` / `mode` — styles
  replaced modes without touching a single note on disk.) A note made with a style you have
  since deleted shows `(missing: <name>)` in the Regenerate dropdown and falls back to
  `edit` until you pick another.
- **Open folder** (best-effort: Explorer from WSL, `xdg-open` on Linux, `open` on macOS),
  with the path shown and copyable as the fallback.
- **Delete note** — after a confirm naming it, the whole folder *moves* to
  `voice-notes/trash/<note>/`. Nothing is erased and no recording is ever unlinked: restore
  a note by moving its folder back. A note a recording is currently continuing cannot be
  deleted until that take ends.

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

The **Styles** block below it is the style editor: the list on the left is every style
file the daemon found, grouped by source; click one to see its text. **Save** always
writes to *your* folder (`~/.config/vnote/styles/`), so saving a built-in creates your
own copy of it — which then wins, and can be deleted to get the original back. **New**
asks for a file name and seeds the front matter, **Duplicate** copies the open one as
`<name>-copy`, and **Delete** (your own files only) removes the file. Anything the daemon
could not read — a folder that will not open, a file with a bad `output:` — is listed
above the editor and skipped, never fatal.

The **Trash** block at the bottom names `voice-notes/trash/` and how many folders are in
it, with **Open folder**. Deleted notes and takes are moved there and vnote never empties
it — that is your call. Restoring is a folder move; the API cannot see inside it.

### Restyling the page

`vnote/web/index.html` is markup only — no JavaScript. `vnote/web/app.js` finds its
elements by id (the full list is the comment at the top of that file — including the
per-take ids it builds at runtime) and drives the look through a few data attributes
(`data-view`, `data-state`, `data-live`, `data-raw`, `data-daemon`, `data-continue`)
that the CSS reacts to. The shipped page is the Claude Design handoff
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
| `--style NAME` | clean up with that [style](#styles) (`vnote --config` lists the ones it found) |
| `--light` · `--edit` · `--summary` · `--dictation` | shortcuts for the built-in styles of those names (`--edit` is the built-in default) |
| `--raw` | transcript only, no LLM |
| `--backend {ollama,claude-code,opencode,claude}` | cleanup backend — see [First run & setup](#first-run--setup) |
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

The style with no flag is the `default_style` setting (`edit` unless you changed it);
`--mode NAME` is still accepted as the old name for `--style`. `--redo` is handy for trying
a different style without re-transcribing (the slow part) — e.g.
`vnote --redo voice-notes/2026-08-24-1033-… --summary`. Every re-run
(and every edit or revision in the web UI) is kept as a version — see
[Web UI → Notes](#notes).

**No GPU?** `--backend claude-code` (or `--backend opencode`) runs cleanup through an
already-configured CLI and needs
no local model at all; transcription falls back to CPU automatically — slower, but it works.

---

## Styles

A **style** is one Markdown file: a small front matter block, then the instruction the
model gets. That instruction *is* the style — there is nothing else to it.

```markdown
---
# the description is the line the dropdown shows
description: a Claude Code session prompt
output: plain              # note | plain (default note)
backend: claude-code       # optional; blank = the backend setting
model:                     # optional; blank = the backend's default
---
Turn the spoken brief into a session prompt in the speaker's voice …
```

- The **file name** is the style's name: lowercase letters, digits, `-` and `_`.
- **`output: note`** asks the model for a title and Markdown; `note.md` starts with
  `# Title`. **`output: plain`** asks for the text alone — the title still goes in
  `meta.json` and the sidebar, but `note.md` has no heading, which is what anything you
  paste somewhere else wants.
- A line starting with `#` is a comment anywhere in the front matter. After a *value*,
  ` #` starts a comment only on `output`, `backend` and `model` — a `description` is prose,
  so `description: sprint #12 review` keeps its `#`. Keys it does not know are ignored, so a
  file can carry its own notes.
- A file that will not parse is skipped and listed in Settings; the rest still load.

**Where they come from**, each source overriding the one before it *by name*:

1. **Built-in** — shipped inside the package (`vnote/styles/*.md`).
2. **Mine** — `~/.config/vnote/styles/`, next to `vocab.txt`. This is where the web UI
   writes; editing a built-in creates your copy of it here.
3. **`VNOTE_STYLES_DIRS`** — extra folders, separated by the platform's path separator
   (`:` on Linux/macOS). Each becomes its own dropdown group, named after the folder, and
   a later folder wins over an earlier one. A folder that will not open is a warning in
   Settings, not a failed start.

Edits apply to the next note — no daemon restart.

**The shipped six:**

| style | output | what it does |
|---|---|---|
| `light` | note | fixes fillers and grammar, keeps your wording and order |
| `edit` | note | reorganizes into headings, lists, and tidy paragraphs (the built-in default) |
| `summary` | note | condenses to the key points |
| `dictation` | plain | plain text, no title or structure — for pasting into something else; put a small fast model in its `model:` line if you use it a lot |
| `prompt` | plain | turns a spoken brief into the opening prompt for a coding-agent session; runs on `claude-code` unless you pick another backend |
| `email` | plain | an email draft — subject, greeting, body, sign-off |

`prompt` and `email` are first drafts on purpose: open them in Settings and make them
yours. And `--raw` (or the `raw` entry in the page's dropdown) is not a style at all — it
skips the LLM and keeps only the Whisper transcript.

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
- **macOS (LaunchAgent):**
  ```bash
  cp scripts/com.vnote.daemon.plist ~/Library/LaunchAgents/
  # edit the absolute paths in ~/Library/LaunchAgents/com.vnote.daemon.plist first —
  # launchd runs with a minimal PATH, so PATH must include the cleanup CLI (opencode,
  # claude) and ~/.local/bin; set VNOTE_DIR to your notes dir
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vnote.daemon.plist
  ```
  Runs at login, restarts on crash; logs to `~/.local/state/vnote-daemon.log`. Restart
  it after reinstalling vnote: `launchctl kickstart -k gui/$(id -u)/com.vnote.daemon`.
- **Native Windows:** leave a terminal running `vnote --serve`.

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
| `backend` | `VNOTE_BACKEND` | `ollama` | `ollama` (local, offline, free) · `claude-code` (your Claude subscription via the Claude Code CLI, no API key) · `opencode` (whatever provider/model [opencode](https://opencode.ai) is configured with) · `claude` (Anthropic API, billed per token) |
| `default_style` | `VNOTE_STYLE` | `edit` | [style](#styles) used when none is picked — the choices are whatever your style folders hold. The old `default_mode` / `VNOTE_MODE` names are still read (deprecated) |
| `language` | `VNOTE_LANGUAGE` | — | transcription language code (`en`, `de`, …); blank = auto-detect per recording |
| `ollama_model` | `VNOTE_OLLAMA_MODEL` | `qwen2.5:14b-instruct` | Ollama model for note cleanup (`ollama pull` it once); a style can name its own instead |
| `ollama_host` | `OLLAMA_HOST` | `http://127.0.0.1:11434` | where Ollama listens |
| `ollama_keep_alive` | `VNOTE_OLLAMA_KEEP_ALIVE` | `30m` | how long Ollama keeps the model loaded after a request — a duration with a unit (`30m`, `1h30m`), a bare number of seconds (`300`), or `-1` = until Ollama exits (Ollama's own default is `5m`) |
| `claude_model` | `VNOTE_CLAUDE_MODEL` | `claude-sonnet-5` | model for the `claude` (API) backend; `claude-code` uses the CLI's own choice |
| `claude_code_bin` | `VNOTE_CLAUDE_CODE_BIN` | `claude` | name or path of the Claude Code CLI |
| `opencode_bin` | `VNOTE_OPENCODE_BIN` | `opencode` | name or path of the opencode CLI |
| `opencode_model` | `VNOTE_OPENCODE_MODEL` | — | model for the `opencode` backend as `provider/model` (blank = opencode's own default; `opencode models` lists the ids) |
| `double_clean` | `VNOTE_DOUBLE_CLEAN` | `0` | after cleaning, save a second varied cleanup alongside the baseline: `0` = off, `1` = on. Written next to the note as `<folder>_note.md` (temperature-0 baseline) and `<folder>_note_variant_tN.md` (varied pass) |
| `variant_temperature` | `VNOTE_VARIANT_TEMPERATURE` | `0.3` | sampling temperature for the second (`double_clean`) pass; the variant file name encodes it (`t3` = 0.3, `t4` = 0.4, …) |
| `whisper_model` | `VNOTE_WHISPER_MODEL` | `small` (CPU) / `large-v3-turbo` (CUDA) | faster-whisper model loaded at daemon start (~1.6 GB on first use) — **restart to apply** |
| `notes_dir` | `VNOTE_DIR` | `./voice-notes` | where note folders are written — **restart to apply** |
| `daemon_host` | `VNOTE_DAEMON_HOST` | `127.0.0.1` | address the daemon binds (keep it on localhost — no auth) — **restart to apply** |
| `daemon_port` | `VNOTE_DAEMON_PORT` | `8760` | port the daemon listens on — **restart to apply** |
| `styles_dirs` | `VNOTE_STYLES_DIRS` | — | extra [style](#styles) folders, `:`-separated; each overrides the built-ins and the folders before it — **restart to apply** (their *contents* apply without one) |
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
| `GET /api/styles` | `{groups: [{label, source, dir, styles: [{name, description, output, backend, model, body, source, path}]}], problems: [...], mine_dir}` — groups in dropdown order (Mine, each extra folder, Built-in) |
| `GET /api/styles/<name>` | the same style fields plus `text` (the file as it is on disk) and `mine`; 404 if there is no such style |
| `PUT /api/styles/<name>` | `{"text": …}` → 201 (created) or 200 (updated) `{saved, path}` — always written to *your* folder; 400 on a bad name or a file that will not parse |
| `DELETE /api/styles/<name>` | 204 — your own files only; 403 for a built-in or another folder's, 404 if unknown |
| `GET /api/notes` | newest-first `{"notes": [{name, title, created, duration_s, mode, backend, has_audio, has_note}]}` |
| `GET /api/notes/<name>` | the same fields plus `meta`, `note`, `transcript`, `transcript_edited`, `style_missing`, `audio_url`, `path`, `versions`, `takes: [{n, created, duration_s, transcript, transcript_edited, audio_url}]` and `live` (a recording is being continued into it right now). A note with one take reports it from its root files |
| `GET /api/notes/<name>/audio` | the audio file (`Range` supported) — after the note has takes, the earliest take's |
| `GET /api/notes/<name>/takes/<n>/audio` | that take's audio (`Range` supported) |
| `POST /api/note?format=webm&mode=…&backend=…&language=…&raw=0` | body = audio bytes → a finished note: `{name, title, note, transcript, meta, cleanup_error}`. `mode` is a style name; omit `backend` (or send it blank) to let the style decide |
| `POST /api/note?continue=<name>&how=continue\|append\|merge` | the same upload, but as the next **take** of an existing note (see `/stream/finish` below for what `how` does) |
| `POST /api/notes/<name>/reclean` | `{mode, backend?, model?, instructions?, takes?}` → `{title, note, version}` — regenerate from the transcript; `mode` is a style name. `takes: [n, …]` regenerates from those takes' transcripts only (non-empty; the version entry records them) |
| `POST /api/notes/<name>/takes/<n>/rerun` | `{how, mode?, backend?, model?, instructions?}` → `{title, note, version, take}` — apply that take to the note *as it stands now*, as a new version; 400 when the note has never been cleaned (regenerate instead) |
| `PUT /api/notes/<name>/takes/<n>/transcript` | `{"text": …}` → `{transcript, transcript_edited}` — edit one take's transcript; keeps its `transcript.original.txt` once and rebuilds the note's joined `transcript.txt`. On a one-take note this is the note-level route |
| `DELETE /api/notes/<name>/takes/<n>` | → `{name, take, trashed}` — the take **moves** to `<notes_dir>/trash/<name>.takes/take-<n>/` (its own namespace, never inside a trashed note of the same name); the join and the duration are rebuilt, `note.md` is untouched and the remaining take numbers keep their gaps. 409 on the note's last take, or while a recording is being continued into it |
| `DELETE /api/notes/<name>` | → `{name, trashed}` — the whole folder **moves** to `<notes_dir>/trash/<name>/`. 409 while a recording is being continued into it |
| `GET /api/trash` · `POST /api/trash/reveal` | `{path, entries}` — where the trash is and how many folders are in it; reveal opens it in the file manager (`{opened, path, entries}`) |
| `POST /api/notes/<name>/revise` | `{instructions, backend?, model?}` → `{title, note, version}` — apply an instruction to the current note |
| `PUT /api/notes/<name>/note` | `{"text": …}` → `{version, title, note}` — save a manual edit |
| `PUT /api/notes/<name>/transcript` | `{"text": …}` → `{transcript, transcript_edited}` — rewrite `transcript.txt` (what Regenerate reads); the first write keeps Whisper's output as `transcript.original.txt`. Not a version. Empty text is allowed. 409 once the note has takes: its `transcript.txt` is derived from them, so edit a take's instead |
| `GET /api/notes/<name>/versions/<n>` | `{n, text, created, op, mode, backend, model, instructions, restored_from}` |
| `POST /api/notes/<name>/restore` | `{"n": …}` → `{title, note, version}` — restores that version as a new one |
| `POST /api/notes/<name>/reveal` | open the folder in the OS file manager (best-effort) → `{opened, path}` |
| `POST /transcribe` | JSON `{audio_path, language?}` (shared filesystem) or the audio as an `application/octet-stream` body with `?format=` → `{transcript, meta}` |
| `POST /clean` | `{transcript, mode?, backend?, model?, tone?, instructions?}` → `{title, body}` — `mode` is a style name |
| `POST /revise` | `{note, instructions, backend?, model?}` → `{title, body}` |
| `POST /stream/start` | `{language?}` → `{session_id, note}` — opens a live session; the daemon keeps the audio |
| `POST /stream/start?continue=<name>` | the same, bound to that note: the recording becomes its next take, and the note refuses a delete while the session lives. 404 if there is no such note; 409 if one is already being recorded into it (one live take per note) |
| `POST /stream/append?sid=` | body = raw s16le 16 kHz mono PCM → `{partial, committed, tail, seconds}`, at once: a per-session worker transcribes the uncommitted tail and commits it at a silence boundary (or after 30 s), so the request never waits on the GPU |
| `POST /stream/ping?sid=` | `{ok: true}` — keeps a paused session alive (sessions expire 30 min after the last touch; an abandoned one's audio lands in `failed/live-*.wav`) |
| `POST /stream/cancel?sid=&keep=0\|1` | drop a live session → `{cancelled: true}`. Its audio goes with it (the user backed out); with `keep=1` the recording is parked in `failed/live-*.wav` first and the reply carries `audio_kept` — for a page dropping a session whose recording it has not saved yet. A recording under half a second is dropped either way |
| `POST /stream/finish?sid=` | → `{transcript, meta, live_transcript}` — one full pass over the whole recording (authoritative), with the live text alongside for comparison |
| `POST /stream/finish?sid=&note=1&mode=…&backend=…&model=…&language=…&raw=0` | the daemon-held audio → a finished note folder: the `/api/note` payload plus `live_transcript` (no second upload on stop) |
| `POST /stream/finish?sid=&note=1&continue=<name>&how=continue\|append\|merge` | the audio becomes take *n* of that note (`takes/<n>/`), then: **continue** (default) — the note is read-only context and the model writes only what carries it on, appended under a `---`; **append** — the new transcript is cleaned on its own and appended the same way; **merge** — the whole note is rewritten with the new material worked in. The reply is the note's detail payload plus `take` and `live_transcript`; the version entry records `{op, how, take}`. The style is `mode` if given, else the note's own, else the default. A raw note (or `raw=1`) keeps the take and cleans nothing. A cleanup that fails *after* the take is written answers 500 with `take` — the recording is already safe in the take folder |

---

## Config file & paths

```bash
vnote --doctor             # check recorder, GPU, clipboard, daemon and backend — with fixes
vnote --config             # every setting, its value and its source
vnote --setup              # re-run the interactive first-run setup
```

- **Config:** `~/.config/vnote/config.json` (`$XDG_CONFIG_HOME/vnote/config.json`)
- **Vocabulary:** `~/.config/vnote/vocab.txt`
- **Your styles:** `~/.config/vnote/styles/*.md`
- **Notes:** `./voice-notes/` (or `VNOTE_DIR`) — one folder per note; a note that has been
  continued keeps each recording in `takes/<n>/` and its root `transcript.txt` is their join
- **Trash:** `./voice-notes/trash/` — deleted notes (`trash/<name>/`) and takes
  (`trash/<name>.takes/take-<n>/`) are *moved* here, never unlinked. vnote never empties it: that
  is your call. The API cannot see inside it, and restoring is a folder move back
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
