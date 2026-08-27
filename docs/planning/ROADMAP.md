# vnote → Flow: roadmap to a Wispr-Flow-class local dictation tool

> A plan to evolve `vnote` from a batch "voice-notes" CLI into a resident,
> dictate-anywhere tool with Wispr-Flow-style UX — **while staying local-first**
> (faster-whisper + Ollama on your RTX 4090) with opt-in cloud as an upgrade.
>
> Sourced from a fact-checked research pass (25 verified claims). Citations are
> inline as `[n]`; the list is at the bottom.
>
> **Status (2026-08-24): flow mode was retired.** Phases 1–7 shipped and were
> used, but the OS-specific delivery layer (hotkey client, injection, tray,
> always-on installers) cost more than it returned. Phase 8 replaces it with a
> web UI served by the daemon — see `PHASE8.md`. The daemon (§2), ASR, the
> cleanup profiles and vocabulary all carry forward; §3's injection layer, §9's
> client items, and the flow-mode topologies below are historical.

---

## 0. The one-sentence reframe

You already have the *hard* part — GPU Whisper + a good local cleanup LLM,
arguably **better** quality than Wispr Flow, and it never leaves your machine.
What you're missing is not model quality; it's **delivery**:

| | vnote today | Wispr Flow | what closes the gap |
|---|---|---|---|
| Models | cold-loaded **every invocation** | always warm in the cloud | a **resident daemon** |
| Trigger | you type `vnote` in a terminal | a **global hotkey**, anywhere | hotkey listener |
| Output | clipboard + a `note.md` folder | **typed into the focused app** | text injection |
| Latency | seconds (mostly model load) | ~700 ms after you stop `[1]` | warm models + streaming |
| Formatting | one heavy "editor" pass | fast, per-app, personalized `[2]` | a light "dictation" profile |

Everything below is about delivery. The pipeline core (`transcribe.py`,
`cleanup.py`) is reused almost verbatim.

### Design principles

- **Cross-platform**: runs on **native Linux, native Windows, and WSL2**. No
  single "headline" OS — portability is a requirement.
- **CLI *and* client are both first-class.** The existing simple CLI stays the
  zero-setup baseline; the global-hotkey client is an additive layer. Both are
  just clients of the same warm daemon.
- **Local-first, opt-in cloud.** Ollama + faster-whisper stay the default;
  Claude/cloud ASR are opt-in toggles only.
- **The only OS-specific layer is text injection** (§3). Daemon, ASR, VAD, and
  cleanup are portable; injection gets a small per-OS backend, and WSL is a
  special seam (Linux process → Windows apps via a thin helper).

---

## 1. How Wispr Flow actually works (verified)

- **It's cloud-first, not on-device.** Audio is streamed to remote servers for
  **both** ASR and the LLM cleanup pass; there is **no offline mode**. `[1]`
  Subprocessors reported: OpenAI (ASR) + a **fine-tuned Llama** cleanup model
  served on Baseten/AWS. `[1]`
- **Latency target ~700 ms** after you stop speaking, budgeted as
  **<200 ms ASR + <200 ms LLM + 200 ms network**. `[1]` That network budget only
  exists *because* they went to the cloud for global reach — it's a cost they
  accept, not a technical necessity.
- **The "magic" is three things**, none of which are the model itself:
  1. always-warm models (no cold start),
  2. a global push-to-talk hotkey daemon,
  3. injection into whatever app has focus, with **per-app tone/formatting**.
- **Personalized formatting layer.** Token-level style control (dashes vs
  commas, capitalization rules), tone matched to the active app (casual in
  Slack, formal in email), delivered via presets (Formal/Casual/…) **plus**
  learned edits. Personalization *data* stays on device; *inference* is
  cloud. `[2]`

**Strategic takeaway:** their cloud dependency is their **weakness**, not their
moat. On a 4090 you can run all of it locally, keep audio on-device, and work
offline — a real differentiator, not a knock-off.

---

## 2. Target architecture

Split the app across the WSL⇄Windows seam into a warm server + thin clients:

```
┌─────────────────────────────  Windows  ──────────────────────────────┐
│  vnote-flow (thin client, system tray)                                │
│    • global push-to-talk hotkey  (pynput GlobalHotKeys)         [4]   │
│    • capture mic  → stream audio                                      │
│    • inject result into focused app (clipboard-paste / SendInput)[3]  │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │  localhost:PORT  (WSL2 NAT, or mirrored) [5]
┌───────────────────────────────┴──────────  WSL2 (RTX 4090)  ──────────┐
│  vnoted  (resident daemon — the keystone)                             │
│    • faster-whisper large-v3-turbo, loaded ONCE, kept warm            │
│      (optional NVIDIA Parakeet backend for ~40× throughput)     [6]   │
│    • Silero/WebRTC VAD for endpointing + streaming              [7]   │
│    • persistent Ollama connection (cleanup profiles)                  │
│    • HTTP + WebSocket API:  audio → {transcript, cleaned}             │
└───────────────────────────────────────────────────────────────────────┘
        ▲
        │ same localhost API
┌───────┴───────────────┐
│  vnote CLI (existing)  │  ← becomes a client of the daemon;
│  note mode / files     │    `vnote memo.m4a` now instant (warm models)
└───────────────────────┘
```

**Why the daemon is Phase 0 and everything else hangs off it:** today every
`vnote` run re-loads the Whisper model into VRAM and re-pokes Ollama — that cold
start *is* your latency. A resident daemon removes it once and enables the
hotkey, streaming, and Windows client as thin layers on top.

**The seam is easy.** Under WSL2's default NAT networking a Windows client
reaches a service inside WSL over `localhost` with no setup; on Win11 22H2+ you
can set `networkingMode=mirrored` in `.wslconfig` for bidirectional `127.0.0.1`
+ IPv6. `[5]` Bind the daemon to `127.0.0.1:PORT`. *Caveat:* localhost
forwarding can intermittently break after sleep/wake — add a health-check +
reconnect. `[5]`

### Deployment topologies (same daemon, three shapes)

The diagram above is the **WSL2** case (the interesting one). The daemon and its
API are identical across all three; only *where the client runs* and *how it
injects* changes:

| Topology | Daemon | Hotkey client | Injection |
|---|---|---|---|
| **Native Linux** | Linux (CUDA) | same machine | `wtype`/`ydotool` (Wayland) or `xdotool` (X11) |
| **Native Windows** | Windows Python (CUDA) | same machine | `SendInput` / clipboard-paste |
| **WSL2** | WSL (CUDA) | **Windows** side, over `localhost` | Windows helper: `SendInput` / clipboard-paste |

In every topology the **CLI** is also a client of the daemon (and falls back to
in-process if no daemon is running), so `vnote memo.m4a` works everywhere with
zero setup — just faster when the daemon is warm.

---

## 3. Component decisions (per layer)

| Layer | Decision | Why | Notes / alternatives |
|---|---|---|---|
| **ASR (default)** | Keep **faster-whisper `large-v3-turbo`** | Already integrated; strong WER; CTranslate2 is the well-supported backend `[6]` | No change to model quality |
| **ASR (speed opt)** | Add **NVIDIA Parakeet TDT 0.6B v3** backend | ~40× throughput of Whisper large-v3 at near-equal WER (6.68 vs 6.43) → frees GPU headroom to run ASR **and** the cleanup LLM concurrently `[6]` | Parakeet CTC 1.1B is English-only; RTFx is *batched throughput*, not streaming latency `[6]` |
| **Endpointing / streaming** | **Silero VAD** to auto-stop on silence; adopt **RealtimeSTT** (wraps faster-whisper, Silero+WebRTC VAD, partial-text callbacks) for streaming | Removes "press Enter to stop"; enables live partials that hide latency `[7]` | `whisper_streaming` (LocalAgreement-2, ~3.3 s latency in a 2023 large-v2/A40 benchmark) is the alternative streaming approach `[7]` |
| **Global hotkey** | **pynput `GlobalHotKeys`**; hold-to-talk via `HotKey.on_deactivate`, plus a toggle mode | Documented primitive; WhisperWriter is a working reference (default `ctrl+shift+space`, modes: continuous / VAD / press-to-toggle / hold-to-record) `[4][8]` | Hotkey callbacks run on the OS input thread — do no blocking work there `[4]` |
| **Text injection** | **Clipboard-paste by default** (save clipboard → set text → send Ctrl+V via SendInput → restore); fallback to `pynput` `keyboard.type()` | Paste is robust for Unicode/Markdown and fast; per-char typing is the fallback. Handy uses clipboard-paste; WhisperWriter uses pynput typing `[3][8][9]` | **UIPI pitfall:** a non-elevated client **cannot** inject into elevated/admin windows and it fails *silently*. Untypable chars raise `InvalidCharacterException`. `[3]` |
| **Injection (per-OS)** | One `inject` backend interface, one impl per platform: **Windows** SendInput/paste · **Linux X11** `xdotool` · **Linux Wayland** `wtype`/`ydotool` · **WSL** → call the Windows helper over localhost | Injection is the **only** inherently OS-specific layer; keeping it behind one seam keeps the daemon/ASR/cleanup fully portable | Handy uses `xdotool`/`wtype` on Linux `[9]` |
| **LLM cleanup** | Two **profiles**: `dictation` (fast/light) and `note` (today's heavy editor) | Wispr's own cleanup is light + per-app `[2]`; your current `edit` prompt is great for notes but too heavy/slow for dictate-anywhere | `cleanup.py` is already pluggable — add a profile + per-app context |
| **Cleanup model (fast)** | Small warm model for `dictation`: benchmark **qwen2.5:3b-instruct**, **llama3.2:3b**, **gemma3:4b** | Comparable local tools use small models: Voicebox→Qwen3, local-whisper→gemma3:4b, Ollama-Transcriber→mistral/llama3.1:8b @ temp 0.3 `[10][11][12]` | Keep 14B for `note` mode |
| **Cloud opt-in** | Keep the existing Claude backend; add an optional cloud ASR toggle | Already scaffolded (`--backend claude`); mirror it for ASR | Opt-in only; local stays default |

---

## 4. Latency budget (local, realistic)

Wispr's 700 ms is a *cloud* budget dominated by network `[1]`. Locally we delete
the 200 ms network hop but pay for a shared GPU. Target **~1–1.5 s** end-to-end
after speech-stop for a short utterance, and hide it with streaming partials:

- VAD silence window (endpoint): ~200–400 ms
- ASR on a warm `large-v3-turbo`, short clip, 4090: few hundred ms
  (Parakeet backend if we need the GPU for concurrent LLM)
- Light cleanup on a warm 3B: few hundred ms — **or skip cleanup entirely** in a
  "raw dictation" fast-path for near-instant insertion
- **The single biggest win is warmth**: removing today's per-invocation model
  load turns seconds into that budget.

> ⚠️ *Research gap:* actual local end-to-end latency vs the 700 ms cloud figure
> was **not** independently verified, and the best small cleanup model is
> under-evidenced `[13]`. **Measure in Phase 0**, benchmark 3B candidates in
> Phase 2 — don't take the numbers above as promises.

---

## 5. Phased roadmap (each phase independently shippable)

- **Phase 0 — Warm daemon (the keystone).**
  Extract `transcribe` + `clean` behind a localhost service (`vnote serve`),
  models loaded once. The CLI becomes a client. *Outcome:* 3–10× perceived
  speedup with **zero UX change**. Lowest risk, highest leverage.

- **Phase 1 — Global hotkey + injection (Flow-mode MVP).**
  Windows client: hotkey → record → POST to daemon → **clipboard-paste into the
  focused app**. This *is* the Wispr-Flow experience, batch-style. Structurally
  clone WhisperWriter. `[8]`

- **Phase 2 — VAD + fast dictation cleanup.**
  Silero VAD auto-stop `[7]`; a `dictation` cleanup profile on a small warm
  model; a deterministic pre-pass for spoken commands ("new line", "period",
  "scratch that") the way Simon Willison's `llm` cleanup prompt does. `[14]`

- **Phase 3 — Streaming partials.**
  RealtimeSTT-style live text as you speak, finalized on stop `[7]`. Biggest
  perceived-latency win; makes it *feel* instant.

- **Phase 4 — Personalization (Wispr's differentiator).**
  Custom vocabulary via faster-whisper `hotwords`/`initial_prompt` + a
  post-cleanup replacement dictionary; **per-app tone** by reading the active
  window title → a tone preset; style presets. Optional learned corrections.

- **Phase 5 — Polish & packaging.**
  System tray, config UI, opt-in cloud toggles, first-run flow, Windows
  installer / `uv tool` distribution.

---

## 6. Concrete changes mapped onto the current code

**New modules**
- `vnote/server.py` — the daemon: warm model registry + HTTP/WebSocket API.
- `vnote/client/` — Windows hotkey + injection client (`hotkey.py`, `inject.py`, `capture.py`, `tray.py`).
- `vnote/vad.py` — Silero/WebRTC VAD endpointing + chunking.
- `vnote/vocab.py` — user dictionary → whisper hotwords + replacement map.
- `vnote/commands.py` — spoken-command pre-pass ("new line", "scratch that", …).

**Refactors (small, the interfaces already fit)**
- `transcribe.py` — move the warm model into the daemon; add a `hotwords` param
  to `_run`; add a Parakeet backend behind the existing `_build/_run` seam.
- `cleanup.py` — add `profile: "dictation" | "note"` and an optional
  `app_context` to `clean()`; it's already backend-pluggable, so this is
  additive.
- `cli.py` — add a `serve` subcommand; make the normal flow POST to the daemon
  if one is up (fall back to in-process for offline/no-daemon).
- `record.py` — reuse for daemon-side file handling; add a streaming chunk mode
  for the client. Keep the `parec`/WSLg path for CLI use.
- `config.py` — add daemon host/port, hotkey binding, injection method,
  dictionary path, per-app profiles.

**Keep as-is**
- `output.py` session folders + clipboard — excellent for `note` mode and as a
  searchable history; Flow mode simply won't write a folder for every micro-utterance.
- `firstrun.py` / `doctor.py` — extend `doctor` to check the daemon + hotkey +
  injection path.

---

## 7. Risks & open questions (carried from the research)

- **UIPI / elevated windows.** A non-elevated client silently can't type into
  admin windows. `[3]` *Mitigations:* prefer clipboard-paste; document it;
  optionally ship an elevated (UIAccess-manifested) injection helper.
- **WSL localhost flakiness.** Forwarding can break after sleep/wake; mirrored
  mode doesn't support the `::1` IPv6 loopback. `[5]` *Mitigation:* health-check
  + auto-reconnect; fallback plan is to run the daemon under Windows-native
  Python if the seam proves annoying.
- **Where to capture audio.** Client-side (Windows) capture avoids WSLg mic
  quirks and is lower-latency for Flow mode; the existing `parec` path stays for
  CLI/WSL. Decide per mode: the process that owns the hotkey owns the mic.
- **Unverified locally:** real end-to-end latency and the best small cleanup
  model — both are **measure-early** items, not assumptions. `[13]`

---

## 8. What to borrow (don't reinvent)

| Project | Take | License |
|---|---|---|
| **WhisperWriter** `[8]` | hotkey modes + pynput injection structure (Python, closest template) | — |
| **Handy** `[9]` | clipboard-paste injection; Parakeet integration; fully-offline posture | MIT |
| **Voicebox** `[10]` | dictation + local LLM (Qwen3) cleanup UX; most similar product (~22k★) | — |
| **RealtimeSTT / whisper_streaming** `[7]` | streaming + VAD engine | — |
| **local-whisper / Ollama-Transcriber** `[11][12]` | Ollama cleanup prompt patterns, small-model defaults | — |
| **Simon Willison's `llm` cleanup prompt** `[14]` | spoken-command handling in the cleanup prompt | — |
| **VoiceInk** `[15]` | reference only — macOS/Apple-Neural-Engine, **not** portable | — |

---

## 9. Post-0.2.0 backlog

Cleanups the 2026-07-06 pre-push review (34 commits, two-axis) flagged as
worthwhile but non-urgent:

- One shared per-platform dispatch map for `client/inject.py` /
  `client/window.py` — the wsl/wayland/x11/macos if-cascade currently repeats
  four times — made public so `doctor.py` stops importing private helpers.
- Per-mode profiles in `cleanup.py`: the `mode == "dictation"` special-casing
  appears in three places; `_MODE_INSTRUCTIONS` could carry a profile instead.
- A small `Vocab` type in `vocab.py` replacing the positional cache tuple.

Larger candidates (open, not committed to): PyPI publish · Parakeet backend
(§3) · small-dictation-model benchmark (§7 measure-early) · asciinema README
demo · a lightweight flow-mode history (§6 keeps "no folder per
micro-utterance", but an append-only transcript log stays open).

---

## Sources

1. Wispr Flow engineering blog — https://wisprflow.ai/post/technical-challenges
2. (same blog, personalization/formatting) + Personalized Styles feature
3. Win32 `SendInput` (UIPI limits) — https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
4. pynput keyboard (GlobalHotKeys, type) — https://pynput.readthedocs.io/en/latest/keyboard.html
5. WSL networking (NAT / mirrored) — https://learn.microsoft.com/en-us/windows/wsl/networking
6. Open ASR Leaderboard (Whisper vs Parakeet) — https://huggingface.co/blog/open-asr-leaderboard
7. RealtimeSTT — https://github.com/KoljaB/RealtimeSTT · whisper_streaming — https://github.com/ufal/whisper_streaming
8. WhisperWriter — https://github.com/savbell/whisper-writer
9. Handy — https://github.com/cjpais/Handy
10. Voicebox — https://github.com/jamiepine/voicebox
11. local-whisper — https://github.com/luisalima/local-whisper
12. Ollama-Transcriber — https://github.com/chumphrey-cmd/Ollama-Transcriber
13. Verified-research caveat: local end-to-end latency and best small cleanup model are under-evidenced — measure early
14. Simon Willison `llm` CLI cleanup pattern — https://news.ycombinator.com/item?id=40174921
15. VoiceInk (macOS-only) — https://github.com/beingpax/VoiceInk
