# vnote — User Guide

Complete reference for vnote and its flow-mode client. For a quick "get running" path,
start with the [README](../README.md); this guide is everything that page leaves out.

- [Install from a clone](#install-from-a-clone)
- [First run & setup](#first-run--setup)
- [`vnote` — command reference](#vnote--command-reference)
- [Cleanup modes](#cleanup-modes)
- [Warm daemon](#warm-daemon)
- [`vnote-flow` — flow-mode reference](#vnote-flow--flow-mode-reference)
- [Spoken commands](#spoken-commands)
- [Custom vocabulary](#custom-vocabulary)
- [Tone & per-app tone](#tone--per-app-tone)
- [Injection methods & caveats](#injection-methods--caveats)
- [Dictation history](#dictation-history)
- [Promote a take to a note](#promote-a-take-to-a-note)
- [Always-on setup](#always-on-setup)
- [Environment variables](#environment-variables)
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
uv pip install -e '.[flow]'   # optional: flow-mode client (pynput, tray)
```

The `vnote` command then lives inside the project's `.venv`, so invoke it as
**`uv run vnote …`** (uv handles the venv), or `source .venv/bin/activate` first and
call `vnote` directly. Every example below uses the bare `vnote` form.

> **On WSL2, this `.venv` is Linux-only.** The daemon runs here (`uv run vnote --serve`),
> but the Windows flow client installs into Windows Python separately — don't run `uv`
> against this clone from Windows. See [Always-on setup](#always-on-setup).

**Audio on WSL:** WSL has no native ALSA device, so recording goes through WSLg's
PulseAudio bridge via `parec` — `sudo apt install -y pulseaudio-utils`. `pw-record`,
`ffmpeg`, or the `sounddevice` library are used as fallbacks if present.

---

## First run & setup

The first time you run `vnote` interactively it asks which cleanup backend to use and,
for Ollama, which model size (pre-selected from your detected GPU memory). Your choice is
saved to `~/.config/vnote/config.json`.

The backend menu offers three options, and pre-selects **Claude Code** when that CLI is on
your PATH (Ollama otherwise, so a machine without it is never steered somewhere it can't
go):

| Backend | Auth | Notes |
|---|---|---|
| `claude-code` | your Claude **subscription** | Best quality, no API key, nothing to download. Needs the [Claude Code CLI](https://claude.com/product/claude-code); run `claude` once to sign in. |
| `ollama` | none | Private, offline, free. One-time model download. |
| `claude` | `ANTHROPIC_API_KEY` | Same models, **billed per token**. Needs the `[claude]` extra. |

- Delete that file to run setup again, or `vnote --setup` to re-run it explicitly.
- Override any choice with a flag or a `VNOTE_*` environment variable.
- The prompt is skipped when input isn't a terminal, so scripts and pipelines never block.

Local cleanup models (pull whichever you chose):

```bash
ollama pull qwen2.5:14b-instruct   # default; ~10 GB VRAM
ollama pull qwen2.5:7b-instruct    # lighter
ollama pull llama3.2:3b            # lightest / fastest
```

---

## `vnote` — command reference

```bash
vnote                      # record from the mic; press Enter to stop
vnote memo.m4a             # process an existing audio file
```

| flag | effect |
|---|---|
| `--light` | faithful cleanup — de-fill + grammar only |
| `--edit` | editorial cleanup — reorganize into headings/lists (**default**) |
| `--summary` | condensed rewrite |
| `--raw` | transcript only, no LLM |
| `--backend {ollama,claude-code,claude}` | cleanup backend — see [First run & setup](#first-run--setup) |
| `--model NAME` | override the cleanup model name |
| `--language CODE` | force transcription language (e.g. `en`); default: auto-detect |
| `--no-clipboard` | don't touch the clipboard |
| `--stdout` | also print the note to stdout (for piping) |
| `-o`, `--open` | open the new note in `$EDITOR` afterward |
| `--redo PATH` | re-run cleanup on a saved note, skipping transcription |
| `--keep-temp-audio` | keep the temporary upload after a daemon run (debugging) |
| `--serve` | run the warm daemon (see [Warm daemon](#warm-daemon)) |
| `--no-daemon` | ignore a running daemon; load models in-process for this run |
| `--promote [TAKE]` | promote a flow take to a note (see [Promote](#promote-a-take-to-a-note)) |
| `--doctor` | check recorder, GPU, clipboard, and backend — with fixes — then exit |
| `--config` | print resolved settings and the config-file path, then exit |
| `--setup` | re-run the interactive first-run setup, then exit |
| `--version` | print the version |

`--redo` is handy for trying a different cleanup intensity without re-transcribing (the
slow part) — e.g. `vnote --redo voice-notes/2026-07-06-1432-… --summary`.

**No GPU?** `--backend claude-code` runs cleanup through your Claude subscription and needs
no local model at all; transcription falls back to CPU automatically — slower, but it works.

---

## Cleanup modes

| mode | flag | what it does |
|---|---|---|
| light | `--light` | fixes fillers and grammar, keeps your wording and order |
| edit | `--edit` (default) | reorganizes into headings, lists, and tidy paragraphs |
| summary | `--summary` | condenses to the key points |
| raw | `--raw` | no LLM at all — just the Whisper transcript |

You can also dictate formatting instructions as you speak and the cleanup step follows
them: *"make that a bulleted list"*, *"put a heading here"*, *"scratch that"*.

---

## Warm daemon

Normally every run loads the Whisper model into VRAM first — several seconds before
transcription even starts. Leave a daemon running and repeat runs skip that entirely:

```bash
vnote --serve              # terminal A: loads the model once, serves on 127.0.0.1:8760
vnote memo.m4a             # terminal B: detects the daemon, starts transcribing at once
```

- The CLI probes for a daemon on every run and silently falls back to in-process models
  when none is up — same output, same files.
- `--no-daemon` forces in-process for a single run.
- `vnote --doctor` shows whether a daemon is up.
- `VNOTE_DAEMON_HOST` / `VNOTE_DAEMON_PORT` move the address.

It binds to localhost only, with no auth — a single-user convenience, not a network
service. The daemon is also required for [flow mode](#vnote-flow--flow-mode-reference).

---

## `vnote-flow` — flow-mode reference

`vnote-flow` is a thin push-to-talk client for the daemon: press the hotkey anywhere,
speak, press it again — the transcript is pasted into whatever app has focus (clipboard
paste, with your previous clipboard restored). Needs the `[flow]` extra and a running
[daemon](#warm-daemon).

```bash
vnote-flow                           # hotkey loop (default: ctrl+shift+space)
```

| flag | effect |
|---|---|
| `--hotkey COMBO` | change the trigger, e.g. `--hotkey ctrl+alt+d` |
| `--vad` | auto-stop after a pause — no second key press |
| `--vad-silence S` | seconds of silence that end an utterance (default `1.0`) |
| `--clean [MODE]` | LLM cleanup before pasting; bare = `dictation` (light). Also `light`/`edit`/`summary` |
| `--backend {ollama,claude-code,claude}` | cleanup backend for `--clean` (runs daemon-side) |
| `--model NAME` | override the cleanup model for `--clean` |
| `--tone TEXT` | free-text tone hint for `--clean` (see [Tone](#tone--per-app-tone)) |
| `--language CODE` | force transcription language |
| `--stream` | show live partial text on the console while you speak |
| `--inject {auto,paste,type}` | how to deliver text (see [Injection](#injection-methods--caveats)) |
| `--once` | run a single capture cycle and exit (no hotkey loop) |
| `--print` | write the result to stdout instead of injecting — pairs well with `--once` |
| `--tray` | system-tray icon instead of a console (see [Always-on](#always-on-setup)) |
| `--no-history` | don't save this session's takes to `voice-notes/flow/` |
| `--version` | print the version |

`vnote-flow --once --print` runs one hotkey-free capture to stdout and works anywhere —
the simplest way to smoke-test the daemon connection.

Point `VNOTE_DICTATION_MODEL` at a small model (e.g. `llama3.2:3b`) to keep `--clean`
fast.

**Backend choice matters more in flow mode than for notes.** You are waiting on the paste,
so latency is felt directly: a small local model returns in about a second, while
`--backend claude-code` spends several seconds per utterance (it launches the CLI each
time). Claude Code is the better pick for notes; a small Ollama model is usually the
better pick for dictation. `--backend` is per-run, so you can mix — a saved default of
`claude-code` with `vnote-flow --clean --backend ollama` for typing.

---

## Spoken commands

The commands **"new line"**, **"new paragraph"**, and **"scratch that"** are applied to
the transcript by rule, even without `--clean`. With `--clean`, a light dictation prompt
also fixes punctuation and fillers and handles the fuzzier commands (*"period"*,
*"quote … unquote"*).

---

## Custom vocabulary

Bias transcription toward your spellings and fix known mistakes, in
`~/.config/vnote/vocab.txt` (path shown by `vnote --config`; override with `VNOTE_VOCAB`).
Applies to **all** transcription paths and needs **no restart**:

```
TRANSFORM              # bare line: bias transcription toward this spelling
Dymola
jason -> JSON          # correction: fix the transcript afterwards (whole-word)
v note -> vnote
```

- A **bare line** is a hotword — it nudges Whisper toward that spelling.
- An **`a -> b`** line is a post-transcription whole-word correction.

---

## Tone & per-app tone

With `--clean`, set a tone hint:

```bash
vnote-flow --clean --tone casual          # any free text
vnote-flow --clean --tone "formal, concise"
```

Or map tones to apps by the focused window's title, in `~/.config/vnote/config.json`:

```json
{ "app_tones": { "slack": "casual", "outlook": "formal, concise" } }
```

The window title is matched case-insensitively against each key; the first match wins.

---

## Injection methods & caveats

`--inject` (or `VNOTE_INJECT`) picks how text reaches the focused app:

| value | behavior |
|---|---|
| `auto` | clipboard-paste, the robust default (**default**) |
| `paste` | force clipboard-paste (saves → sets → Ctrl+V → restores your clipboard) |
| `type` | per-character typing via pynput — fallback for apps that swallow paste |

Caveats:

- **Windows UIPI:** a non-elevated client **cannot** type into elevated/admin windows,
  and it fails *silently*. Run the client elevated if you must inject into admin apps.
- **Wayland (Linux):** injection needs `wtype` or `ydotool` installed.

---

## Dictation history

Every flow take is saved by default — audio, raw transcript, and cleaned text — as an
append-only daily log next to your batch notes:

```
voice-notes/flow/
  2026-07-06.md               # one ## entry per take: raw, clean, audio link
  audio/20260706-143207.wav
```

Switches (all on by default):

| switch | effect |
|---|---|
| `--no-history` (or `VNOTE_HISTORY=0`) | don't save takes this session |
| `VNOTE_HISTORY_AUDIO=0` | keep the text, drop the WAVs |
| `VNOTE_HISTORY_RAW=0` | omit raw transcripts |
| `VNOTE_HISTORY_CLEAN=0` | omit cleaned text |

`--no-history` is also a tray toggle. The daemon owns the files (they land in its
`voice-notes/`); the client sends each take to `POST /history` best-effort — dictation
never blocks on history.

---

## Promote a take to a note

A take that turns out to be a real note can be **promoted** — rebuilt as its own
dated-titled session folder, same layout as batch notes, with the WAV moved in and a
pointer left under the take's timestamp in the daily log:

```bash
vnote --promote                    # the last take
vnote --promote 14:32:07           # that take, in the newest day file
vnote --promote "2026-07-05 09:15:00"   # an explicit day + time
```

The tray menu's **"Save last take as note"** does the same with one click. Promoting a
take twice is a clear error — the daily log keeps the complete timeline either way.

---

## Always-on setup

Run flow mode hands-free with a tray icon, and start it automatically. First, the tray:

```bash
vnote-flow --tray
```

A tray icon replaces the console — **green** ready / **red** recording / **amber**
processing — with toggles for cleanup and VAD. Pair it with `pythonw` (Windows) for a
fully windowless client.

Then pick your topology to make it start on its own:

### Windows client (the WSL2 case)

The daemon runs inside WSL (CUDA); `vnote-flow` runs as **Windows** Python and reaches
the WSL daemon over `localhost` with no setup. Install and register a windowless startup
shortcut in one command (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows-client.ps1 -Startup
```

This installs the `[flow]` extra and drops a hidden startup shortcut. **Re-run it after
every update** — the Windows client is a copy, so code changes don't reach it until you
reinstall. If `vnote-flow` isn't on PATH afterward, run it as `py -m vnote.client.app`.

> **Never run `uv` from the cloned repo on the Windows side.** `uv run` / `uv sync` try to
> rebuild the project's `.venv` — but WSL created that as a *Linux* venv, and Windows can't
> remove its `lib64` symlink, so you get
> `failed to remove file … .venv\lib64: Access is denied (os error 5)`. One `.venv` can't
> serve both OSes. Install into Windows Python instead (below).

**Two ways to install the Windows client** — either avoids the `.venv` collision:

| Approach | Command (PowerShell) | Trade-off |
|---|---|---|
| **Local clone** (what the installer script does) | `py -m pip install --upgrade "D:\Projects\vnote[flow]"` | Reflects your local code, but as a *copy* — re-run after every change. |
| **From GitHub** (`uv tool`, isolated env) | `uv tool install "vnote[flow] @ git+https://github.com/greenwoodms06/vnote"` | Isolated global tool, `vnote`/`vnote-flow` on PATH — but installs the **pushed** version, not un-pushed local edits. |

Then launch with `py -m vnote.client.app --tray` (or `vnote-flow --tray` if it's on PATH).

> Running the daemon in WSL from a clone with `uv run vnote --serve` is fine — that uses
> the Linux `.venv`. The rule is just **one venv per OS**: WSL owns `.venv`; Windows
> installs separately.

### Daemon at Windows logon (WSL2)

Start the WSL daemon automatically via Task Scheduler → new task:

- **Action:** `wsl.exe -d <YourDistro> -- ~/.local/bin/vnote --serve`
- **Trigger:** *At log on*
- Tick **Hidden**.

(Or just leave a terminal running `vnote --serve`.)

### Daemon on native Linux (systemd)

```bash
cp scripts/vnote-daemon.service ~/.config/systemd/user/
systemctl --user enable --now vnote-daemon
```

### All-native (Linux / Windows / macOS)

Daemon and client on the same machine — `pip install 'vnote[flow]'`, run `vnote --serve`
and `vnote-flow --tray`. No WSL seam to bridge.

---

## Environment variables

Environment variables override the saved first-run choice. A `.env` in the current
directory is auto-loaded (see `.env.example`).

| var | default |
|---|---|
| `VNOTE_DIR` | `./voice-notes` |
| `VNOTE_WHISPER_MODEL` | `large-v3-turbo` |
| `VNOTE_BACKEND` | `ollama` (`ollama` \| `claude-code` \| `claude`) |
| `VNOTE_OLLAMA_MODEL` | `qwen2.5:14b-instruct` |
| `VNOTE_CLAUDE_CODE_BIN` | `claude` (path to the Claude Code CLI; `--backend claude-code`) |
| `VNOTE_CLAUDE_MODEL` | `claude-sonnet-5` (`--backend claude` only) |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` |
| `VNOTE_DAEMON_HOST` | `127.0.0.1` |
| `VNOTE_DAEMON_PORT` | `8760` |
| `VNOTE_HOTKEY` | `ctrl+shift+space` (vnote-flow) |
| `VNOTE_INJECT` | `auto` (vnote-flow: `paste` / `type`) |
| `VNOTE_VAD` | off (vnote-flow: `1` = auto-stop on silence) |
| `VNOTE_VAD_SILENCE` | `1.0` (seconds of pause that end an utterance) |
| `VNOTE_STREAM` | off (vnote-flow: `1` = transcribe while speaking) |
| `VNOTE_TRAY` | off (vnote-flow: `1` = system-tray icon) |
| `VNOTE_HISTORY` | on (vnote-flow: `0` = don't save takes) |
| `VNOTE_HISTORY_AUDIO` | on (vnote-flow: `0` = keep text, drop the WAVs) |
| `VNOTE_HISTORY_RAW` | on (vnote-flow: `0` = omit raw transcripts) |
| `VNOTE_HISTORY_CLEAN` | on (vnote-flow: `0` = omit cleaned text) |
| `VNOTE_DICTATION_MODEL` | the `ollama_model` (small/fast model for `--clean`) |
| `VNOTE_VOCAB` | `~/.config/vnote/vocab.txt` (hotwords + corrections) |
| `ANTHROPIC_API_KEY` | — (required for `--backend claude`; **not** for `claude-code`) |

---

## Config file & paths

```bash
vnote --doctor             # check recorder, GPU, clipboard, and backend — with fixes
vnote --config             # show resolved settings and the config-file path
vnote --setup              # re-run the interactive first-run setup
```

- **Config:** `~/.config/vnote/config.json` (backend, model, `app_tones`, …)
- **Vocabulary:** `~/.config/vnote/vocab.txt`
- **Notes & history:** `./voice-notes/` (or `VNOTE_DIR`)
- **Whisper model cache:** `~/.cache/huggingface`

---

## Development & testing

```bash
uv pip install -e '.[dev]'   # pytest + ruff
uv run python -m pytest -q   # unit tests (pure logic; no GPU/mic/network)
uv run ruff check vnote tests
```

The unit tests cover the testable core — transcript parsing, slugging, config
resolution, first-run gating, history append/promote. The hardware paths (mic capture,
GPU transcription, Ollama/Claude calls) can't run in CI; smoke-test them manually with
the bundled public-domain clip:

```bash
uv run vnote .testdata/jfk.flac
```
