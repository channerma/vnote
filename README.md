# vnote — local voice notes, on your own GPU

[![CI](https://github.com/greenwoodms06/vnote/actions/workflows/ci.yml/badge.svg)](https://github.com/greenwoodms06/vnote/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Speak, and get clean Markdown back — transcribed locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your **GPU**, tidied by an LLM
(local [Ollama](https://ollama.com), or your Claude subscription), on your machine by default.
Two ways in:

- **Web UI** (recommended) — `vnote --serve --open` opens a page in your browser: record
  (pause and resume as you think), get the cleaned note with a **Copy** button, browse and
  play everything you've recorded, edit a note or regenerate / revise it, change settings.
- **CLI** — `vnote` records from the mic (Space pauses, Enter stops) or takes an audio file,
  and drops the note on your clipboard. Everything the web UI does is a flag here too.

> A personal tool I use daily, shared as-is — no support promised, but issues and PRs are welcome.
> On macOS and want polished point-and-talk? [yapper](https://github.com/ahmedlhanafy/yapper)
> or [local-whisper](https://github.com/luisalima/local-whisper) fit better.

## Platform support

| Route | Needs | Status |
|---|---|---|
| **Web UI** | a browser, and somewhere to run the daemon (Linux, WSL2, macOS, Windows) — the **browser owns the mic**, no audio setup | **primary — tested on WSL2 + a Windows browser** |
| CLI mic recording | WSL2 / Linux: `parec` (`pulseaudio-utils`) · native Windows / macOS: `sounddevice` | WSL2 + Linux tested; others best-effort |
| Audio files (`vnote memo.m4a`) | nothing extra | everywhere |

Transcription uses CUDA where it exists and falls back to CPU (macOS is CPU-only — slower, but it works).

## Install

```bash
uv tool install git+https://github.com/greenwoodms06/vnote   # puts `vnote` on your PATH
```

You also need **a cleanup backend** — one of:

- **[Claude Code](https://claude.com/product/claude-code)** — best quality, uses your Claude
  subscription, no API key. Nothing to download.
- **[Ollama](https://ollama.com)** — local, offline, free: `ollama pull qwen2.5:14b-instruct`
  (~10 GB VRAM; lighter options exist).

The first transcription downloads the Whisper model (~1.6 GB). Then `vnote --doctor` checks your
setup and names anything missing. (On WSL, **CLI** mic recording additionally needs
`sudo apt install -y pulseaudio-utils`; the web UI does not.)

<sub>Hacking on it? Clone and `uv sync && uv pip install -e .`, then run commands as
`uv run vnote …`. See the [User Guide](docs/USER_GUIDE.md#install-from-a-clone).</sub>

## Quickstart — Web UI

```bash
vnote --serve --open        # loads the models once, opens http://127.0.0.1:8760
```

- **Record** → talk → **Pause** / **Resume** (or Space) → **Stop**. Pick the cleanup mode
  (light / edit / summary / dictation / raw) and the backend per recording.
- The note appears with a **Copy** button; it is also saved under `voice-notes/`.
- **Notes**: every note you've made, newest first — play the audio, copy, **edit** the
  Markdown, regenerate in another mode or **revise** it with an instruction ("make it
  shorter"); every change is a version you can restore. **Settings**: every setting with a
  description, saved to `~/.config/vnote/config.json` (the CLI reads the same file), plus
  your custom vocabulary.
- **WSL2:** run the daemon in WSL and open the page in your Windows browser — `localhost` just
  works. Leave the daemon running (a systemd unit for Linux is in the
  [User Guide](docs/USER_GUIDE.md#warm-daemon)).

The page is served by the daemon itself — no build step, nothing else to install, and it
never talks to anything but `127.0.0.1`.

## Quickstart — CLI

```bash
vnote                  # record from the mic; Space pauses, Enter stops
vnote memo.m4a         # …or process an existing audio file
```

You get a cleaned note on your clipboard and saved under `voice-notes/`. The default cleanup
reorganizes into headings and lists; `--light` only fixes grammar and fillers, `--summary`
condenses, `--dictation` gives plain text from a small fast model, `--raw` skips the LLM.
You can dictate formatting as you talk — *"make that a bulleted list"*, *"scratch that"* — and
the cleanup follows along. `--redo DIR --summary` re-cleans a saved note without
re-transcribing.

First run asks which cleanup backend to use and saves the choice (`vnote --setup` re-runs it;
the web UI's Settings page edits the same file). Override per run with `--backend`:

```bash
vnote --backend claude-code   # your Claude subscription (no API key)
vnote --backend ollama        # local and offline
vnote --backend claude        # Anthropic API, billed per token
```

See the [User Guide](docs/USER_GUIDE.md) for every flag.

## Output

Each note is a folder `voice-notes/YYYY-MM-DD-HHMM-<slug>/`:

| file | what |
|---|---|
| `audio.wav` / `audio.webm` | the recording (or a copy of the file you passed) |
| `transcript.txt` | raw Whisper output |
| `note.md` | the cleaned note — the thing you keep |
| `meta.json` | model, durations, language, timestamps |
| `versions/note-<n>.md` | every version of the note (the current one included) — edits, regenerations, revisions — restorable from the web UI |

## Learn more

The **[User Guide](docs/USER_GUIDE.md)** covers everything this page leaves out: the web UI
page by page, the full CLI flag reference, the warm daemon and its HTTP API, custom vocabulary,
every setting and environment variable, and development/testing.

## License

[MIT](LICENSE) © Scott Greenwood
