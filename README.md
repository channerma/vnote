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
| macOS (Apple Silicon) | `sounddevice` | CPU only | `pbcopy` | **tested** — CPU-only, slower |
| any | — (file mode) | CUDA / CPU | best-effort | processing audio files works everywhere |

Processing an existing file (`vnote memo.m4a`) needs no audio setup at all.

<details>
<summary><b>macOS notes</b> — model choice, permissions, alternatives</summary>

CTranslate2 (faster-whisper's backend) has no Metal/MPS build, so transcription runs on
**CPU**. That is the one real ceiling on macOS — tools built on whisper.cpp do use Metal
and are markedly faster here. Apple Silicon is quick enough anyway, but the default
`large-v3-turbo` is the slowest option. Measured on an M-series Mac with the 11-second `.testdata/jfk.flac`, models
already downloaded:

| `VNOTE_WHISPER_MODEL` | time | notes |
|---|---|---|
| `base` | 1.3 s | fastest; fine for flow dictation |
| `small` (default) | 2.8 s | good balance — the default |
| `large-v3-turbo` | 10.1 s | best accuracy; the right pick on CUDA |

`small` is the default for that reason — it keeps flow dictation responsive on CPU. On a
CUDA box, where `large-v3-turbo` runs about realtime, prefer accuracy:

```bash
export VNOTE_WHISPER_MODEL=large-v3-turbo
```

Mic recording and the flow hotkey both need macOS permissions, granted to *the terminal or
app that launches vnote* (System Settings → Privacy & Security):

- **Microphone** — for `vnote` with no file argument.
- **Accessibility** — for `vnote-flow`: reading the global hotkey, injecting the
  paste, and reading the frontmost app's window title as AppleScript all go through
  it. Without it the hotkey silently never fires.

Beyond those two, nothing else on macOS needs installing:

- **No audio tools.** Mic capture uses `sounddevice` with its bundled PortAudio.
  `parec`/`pw-record` are WSL/Linux-only — don't install `pulseaudio-utils` here.
  A Homebrew `ffmpeg` on PATH is harmless: vnote ignores it for mic capture (its
  `-f pulse` input is Linux-only). Audio *files* (`vnote memo.m4a`) decode through
  faster-whisper's bundled ffmpeg, so no separate binary is needed either.
- **No CUDA noise.** CTranslate2 ships no macOS build, so vnote doesn't even probe
  CUDA on this platform — you never see "GPU init failed … not compiled with CUDA";
  `vnote --doctor`/`--config` just report CPU. (Off macOS the probe and its error
  still run, so a broken Linux CUDA stack stays loud.)
- **Pasting is ⌘V.** `vnote-flow` sends Cmd+V on macOS (Ctrl+V elsewhere) — that's
  one more reason the Accessibility grant must be to the launching app.
- **Per-app tone reads the window title via AppleScript** (System Events), only if
  you add an `app_tones` map to `~/.config/vnote/config.json`. The first read
  prompts once for Automation control of System Events; decline and tone just
  falls back to `--tone`.
- **Always-on is manual so far.** No launchd unit ships yet — run
  `vnote --serve` and `vnote-flow --tray` yourself, or add your own LaunchAgent,
  for at-login dictation (see the User Guide's always-on section).

</details>

## Install

```bash
uv tool install git+https://github.com/greenwoodms06/vnote   # puts `vnote` on your PATH
```

You also need:

- **A cleanup backend** — one of:
  - **[Claude Code](https://claude.com/product/claude-code)** — best quality, uses your
    Claude subscription, no API key. Nothing to download.
  - **[opencode](https://opencode.ai)** — reuses whatever provider you already set it up
    with (a local MLX/llama.cpp server, or a hosted one). No extra key for vnote.
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
your choice — re-run it any time with `vnote --setup`. It suggests whichever CLI you
actually have: Claude Code, then opencode, then Ollama. Override per run with `--backend`:

```bash
vnote --backend claude-code   # your Claude subscription (no API key)
vnote --backend opencode      # opencode's configured provider/model
vnote --backend ollama        # local and offline
vnote --backend claude        # Anthropic API, billed per token
```

Both CLI backends run with **every tool disabled** — cleanup is a pure text transform, so
the model gets no access to your files (opencode additionally runs in a scratch directory
rather than your current project). Details in the
[User Guide](docs/USER_GUIDE.md#the-opencode-backend).

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
