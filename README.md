# vnote — local voice notes & dictation, on your own GPU

[![CI](https://github.com/greenwoodms06/vnote/actions/workflows/ci.yml/badge.svg)](https://github.com/greenwoodms06/vnote/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Speak, and get clean Markdown back — transcribed locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your **GPU**, tidied by a
**local LLM**, on your machine by default. Two ways to use it:

- **Notes** — `vnote` records (or takes an audio file), transcribes, cleans it up, and
  drops the note on your clipboard. Great for memos and long-form dictation you want to keep.
- **Flow** — a global hotkey pastes what you say into *whatever app has focus*, anywhere.
  Wispr-Flow-style dictation, but fully local.

> A personal tool I use daily, shared as-is — no support promised, but issues and PRs are welcome.
> On macOS and want polished point-and-talk? [yapper](https://github.com/ahmedlhanafy/yapper)
> or [local-whisper](https://github.com/luisalima/local-whisper) fit better.

## Platform support

| Platform | Mic recording | Transcription | Clipboard | Status |
|---|---|---|---|---|
| **WSL2** (Windows) | `parec` via WSLg | CUDA | `clip.exe` | **primary — tested** |
| **Native Linux** | `parec` / `pw-record` / `sounddevice` | CUDA | `wl-copy` / `xclip` / `xsel` | **tested** |
| Windows (native) | `sounddevice` | CUDA | `clip.exe` | untested — should work |
| macOS | `sounddevice` | CPU only | `pbcopy` | untested — CPU-only, slower |
| any | — (file mode) | CUDA / CPU | best-effort | processing audio files works everywhere |

Processing an existing file (`vnote memo.m4a`) needs no audio setup at all.

## Install

```bash
uv tool install git+https://github.com/greenwoodms06/vnote   # puts `vnote` on your PATH
```

You also need:

- **A cleanup backend** — one of:
  - **[Claude Code](https://claude.com/product/claude-code)** — best quality, uses your
    Claude subscription, no API key. Nothing to download.
  - **[Ollama](https://ollama.com)** — local, offline, free: `ollama pull qwen2.5:14b-instruct`
    (~10 GB VRAM; lighter options exist).
- On **WSL**, the recorder: `sudo apt install -y pulseaudio-utils`.

The first transcription downloads the Whisper model (~1.6 GB). Then run `vnote --doctor`
to check your setup — it names anything missing and how to fix it.

<sub>Hacking on it? Clone and `uv sync && uv pip install -e .`, then run commands as
`uv run vnote …`. See the [User Guide](docs/USER_GUIDE.md#install-from-a-clone).</sub>

## Quickstart — Notes

```bash
vnote                  # record from the mic; press Enter to stop
vnote memo.m4a         # …or process an existing audio file
```

You get a cleaned note on your clipboard and saved under `voice-notes/`. That's it.

The default cleanup reorganizes into headings and lists; use `--light` to only fix
grammar and fillers, `--summary` to condense, or `--raw` for the bare transcript.
You can dictate formatting as you talk — *"make that a bulleted list"*, *"scratch that"*,
*"put a heading here"* — and the cleanup follows along.

First run asks which cleanup backend to use (and, for Ollama, which model size) and saves
your choice — re-run it any time with `vnote --setup`. It suggests Claude Code when that
CLI is installed, Ollama otherwise. Override per run with `--backend`:

```bash
vnote --backend claude-code   # your Claude subscription (no API key)
vnote --backend ollama        # local and offline
vnote --backend claude        # Anthropic API, billed per token
```

See the [User Guide](docs/USER_GUIDE.md) for every flag.

## Quickstart — Flow (dictate into any app)

Flow adds a global push-to-talk hotkey that pastes into the focused app. It needs a
warm **daemon** (holds the models in VRAM) plus the **`vnote-flow`** client.

```bash
pip install 'vnote[flow]'    # 1. add the flow extra (pynput, tray icon)
vnote --serve                # 2. start the warm daemon  → 127.0.0.1:8760
vnote-flow                   # 3. hotkey loop: press ctrl+shift+space, speak, press again
```

Common flags — run `vnote-flow --help` or see the
[User Guide](docs/USER_GUIDE.md#vnote-flow--flow-mode-reference) for the full set:

- `--vad` — auto-stop after a short pause, so you don't press the hotkey twice
- `--clean` — light LLM cleanup before pasting (default pastes the raw transcript)
- `--hotkey COMBO` — change the trigger from `ctrl+shift+space`
- `--once --print` — one hotkey-free capture to stdout; the easiest first test

### Always-on with a tray icon

Run the client in the tray instead of a console — green *ready* / red *recording* /
amber *processing*, with toggles for cleanup and VAD:

```bash
vnote-flow --tray
```

To launch it automatically at login (and for the WSL2 setup where the daemon lives in
WSL and `vnote-flow` runs on the **Windows** side), follow
**[User Guide → Always-on setup](docs/USER_GUIDE.md#always-on-setup)** — it has the
one-command Windows installer, the Linux systemd unit, and the WSL Task Scheduler recipe.

> **Which machine?** Run `vnote-flow` on the machine that owns the keyboard and mic.
> On WSL2 that's **Windows** Python talking to the daemon inside WSL over `localhost` —
> install the client with `py -m pip` or the installer script, **never `uv` from the
> cloned repo** (it fights the Linux `.venv` WSL built). Full walkthrough:
> [User Guide → Always-on setup](docs/USER_GUIDE.md#always-on-setup).

## Output

Each note run writes `voice-notes/YYYY-MM-DD-HHMM-<slug>/`:

| file | what |
|---|---|
| `audio.wav` | the recording (or a copy of the file you passed) |
| `transcript.txt` | raw Whisper output |
| `note.md` | the cleaned note — the thing you keep (also copied to your clipboard) |
| `meta.json` | model, durations, language, timestamps |

Flow takes are logged separately under `voice-notes/flow/` and any one can be *promoted*
into a full note folder. See the [User Guide](docs/USER_GUIDE.md#dictation-history).

## Learn more

The **[User Guide](docs/USER_GUIDE.md)** covers everything this page leaves out: the full
CLI and flow flag reference, the warm daemon, custom vocabulary, per-app tone, injection
methods and their caveats, dictation history and promotion, always-on setup for every
platform, the full environment-variable table, and development/testing.

## License

[MIT](LICENSE) © Scott Greenwood
