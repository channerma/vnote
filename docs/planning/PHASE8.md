# Phase 8 — The web UI replaces flow mode

> The tray / always-on layer (Phase 5) turned out to be the wrong delivery
> vehicle: too much to set up, and every piece of it — the Windows-side
> client, keystroke injection, `pythonw` shortcuts, "one venv per OS" — was
> OS-specific behaviour the project no longer wants to own. Usage agreed:
> notes mode kept being used daily; flow history went quiet after
> 2026-07-06. So flow mode is **retired** (decided 2026-08-24), and the
> recommended way to use vnote becomes a small web page served by the
> daemon that already exists. The CLI stays the foundation.

## Objective

1. **A web UI served by `vnote --serve`** at `http://127.0.0.1:8760` — one
   static page (`vnote/web/index.html` + `app.js` + `style.css`), vanilla
   JS, no build step, zero new Python dependencies (the stdlib
   `ThreadingHTTPServer` in `server.py` grows a `GET /` and a `/api/*`
   family). `vnote --serve --open` opens the browser.
2. **The browser owns the microphone.** Record / Pause / Resume / Stop via
   `getUserMedia` + `MediaRecorder`; on Stop the blob is POSTed to
   `/api/note` and the daemon runs the full note pipeline in-process. The
   page shows the resulting Markdown in a box with a **Copy** button — that
   is the new "put it somewhere else" path. `parec` / WSLg / `sounddevice`
   stop mattering for this route: anything with a browser and a place to
   run the daemon works.
3. **Notes view**: newest-first list of session folders; open one to read
   the note, the transcript, play the audio (`Range` support so seeking
   works), Copy, and **re-clean** in another mode (the CLI's `--redo`).
4. **Settings**: per-recording picks (mode / backend / language / raw) next
   to Record, plus a Settings page listing every setting **with a
   description**, backed by one registry in `config.py` (`SETTINGS`) that
   also drives `vnote --config` and a docs-consistency test. Editable
   values persist to `~/.config/vnote/config.json` — shared with the CLI —
   so `vnote --setup` becomes optional. Settings bound at daemon start
   (Whisper model, notes dir, address) are shown read-only with a restart
   hint. The custom-vocabulary file is editable in the page too.
5. **`vnote/pipeline.py`**: the record→transcribe→clean→`write_session`
   body is factored out of `cli.py:main()` (and `--redo`) into pure
   functions (`make_note`, `reclean`) that the CLI and the server both call.
6. **CLI recorder pause**: while `vnote` records, **Space** pauses/resumes
   (paused audio is discarded, resume is gap-free) and Enter stops; one
   capture loop for every backend.
7. **Removal**: `vnote-flow`, `vnote/client/`, `vad.py`, `commands.py`,
   `history.py` (+ `--promote`, `/history`, `/promote`), the `[flow]` extra,
   `scripts/install-windows-client.ps1`, the always-on docs, the per-app
   tone map. `cleanup.py`'s `dictation` mode stays as a fourth pick
   (plain text, fast). The `/stream/*` endpoints stay (daemon-side,
   portable, tested — the natural route to live partials in the browser).
8. **Version 0.5.0** — removing a console script is a breaking change.

## API contract (the stable boundary — a Claude Design export may replace the markup)

| route | behaviour |
|---|---|
| `GET /` | `vnote/web/index.html` |
| `GET /static/<file>` | files in `vnote/web/` only (`.js .css .svg .png .ico`; no `..`) |
| `GET /health` | unchanged |
| `GET /api/settings` | `{"settings":[{key, value, default, description, kind, choices?, source: env\|file\|default, editable}]}` |
| `PUT /api/settings` | `{key: value, …}` → editable keys to config.json; 400 on unknown / non-editable / env-locked key or bad choice |
| `GET` / `PUT /api/vocab` | `{"text": …}` ↔ `vocab.vocab_file()` (mtime cache: applies on the next transcription, no restart) |
| `GET /api/notes` | newest-first `{"notes": [{name, title, created, duration_s, mode, backend, has_audio, has_note}]}` from each `meta.json` |
| `GET /api/notes/<name>` | `{name, meta, note, transcript, audio_url}` |
| `GET /api/notes/<name>/audio` | the `audio.*` file, Content-Type by suffix, single-range `Range` |
| `POST /api/note?format=webm&mode=&backend=&model=&language=&raw=` | body = audio bytes → `pipeline.make_note` under the inference lock → `{name, title, note, transcript, meta}` |
| `POST /api/notes/<name>/reclean` | `{mode, backend?, model?}` → `pipeline.reclean` → `{title, note}` |
| `POST /transcribe`, `/clean`, `/stream/*` | unchanged |

`<name>` must match `^\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9-]+$` **and** resolve
under `NOTES_DIR`; anything else is 404. The page binds behaviour to a short,
documented list of element ids (top of `app.js`), so a redesigned
`index.html` that keeps those ids drops in unchanged.

## Design constraints

- **Zero new dependencies**, `[project.dependencies]` untouched; the page
  makes no external requests (the daemon serves everything).
- **Localhost only, no auth** — as today. `getUserMedia` needs a secure
  context, which `http://localhost` / `127.0.0.1` are; a non-localhost
  daemon host gets a clear message, not a mystery failure.
- **The pipeline is pure.** `pipeline.py` prints nothing, touches no
  clipboard, cleans no temp files; the CLI and the server each own their
  I/O. A web run never touches the daemon's clipboard — Copy is explicit.
- **One writer for settings.** The page writes only through
  `config.save_config`; env vars keep overriding and are reported as such.
- **Design/behaviour split.** `index.html` carries no JavaScript; `app.js`
  reaches elements by id only.
- **No capture-time organisation** (PHASE7's rule stands): a recording is a
  recording; mode/backend are cleanup choices, and re-clean is post-hoc.

## Scope

**In:** `pipeline.py`; the `SETTINGS` registry + resolvers for
`claude_model` / `claude_code_bin` / `ollama_host` / `default_mode` /
`language`; `server.py` static + `/api/*` + `--open`; the page; Space-pause
in `record.py`; the removal above; README + User Guide rewrites;
ROADMAP retirement note; `.env.example`; 0.5.0.

**Out:** audio extraction / clipping (explicitly "not yet" — and no segment
timestamps are captured for it yet), live partials in the browser,
Markdown rendering beyond a trivial one, auth or LAN binding, a daemon
auto-start recipe for Windows, PyPI publishing.

## Acceptance criteria

- [ ] `uv run vnote --serve --open` opens the page; recording **with a
      pause** produces `voice-notes/<stamp>-<slug>/` with `audio.webm`,
      `transcript.txt`, `note.md`, `meta.json` (`source: "web"`); Copy puts
      the Markdown on the clipboard.
- [ ] The Notes page lists existing sessions newest-first; an existing
      `audio.wav` plays and seeks; re-clean in `summary` rewrites `note.md`
      and marks `meta.json` `recleaned`.
- [ ] Changing `backend` in Settings updates `~/.config/vnote/config.json`
      and `vnote --config` shows it; an env-overridden key is shown as `env`
      and refuses edits with a clear message.
- [ ] Adding `jason -> JSON` to the vocabulary in the page corrects the next
      transcript with **no daemon restart** (closes the open Phase 4 check).
- [ ] `vnote`: Space pauses (timer freezes, "paused" shown), Space resumes,
      Enter stops; the reported duration excludes the pause; piped stdin
      still works Enter-only; the terminal is restored on every exit path.
- [ ] `pip install` of the package exposes only `vnote`; the wheel contains
      `vnote/web/*`; `vnote --doctor` no longer mentions flow.
- [ ] Every `SETTINGS` env var appears in the User Guide table and
      `.env.example` (enforced by a test); CI (ruff + pytest, no heavy deps)
      passes; version 0.5.0 with `uv.lock` in the same commit.
