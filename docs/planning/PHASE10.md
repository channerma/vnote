# Phase 10 — Beta feedback: warm start, styles, takes

> The Body's first feedback on the 0.6.0 web app (2026-08-25) and the decisions
> reached in the same session (grill, four rounds). This page is the plan; the
> reasoning is in the session, the decisions are here. Order: **A → D → C → E → F**,
> each a separate commit with tests and a fresh-context diff review.
>
> **Status:** decided 2026-08-25. **A** (warm start) and **D** (labels) built and
> reviewed the same day; **C** built the same day (review fixes in progress); E and F
> not started.

## The feedback (six items) and the verdicts

| # | asked | verdict |
|---|---|---|
| 1 | Warm Ollama in the background so recording can start at once | **Yes** — and it is Whisper, not Ollama, that blocks the page today (`serve()` warms before `serve_forever`); Ollama starts lazily on the first cleanup. Both go to a background thread → **A** |
| 2 | Edit the live transcript while paused | **No** — Stop re-transcribes the whole recording; live edits would be thrown away |
| 3 | A "process on stop" toggle; edit the raw transcript before the AI runs | **Yes** — off → a raw note; the raw pane becomes editable; Regenerate reads the edit → **C** |
| 4 | Continue a stopped recording | **Yes** — per-take folders, per-take cleanup, re-run, delete → **F** |
| 5 | Does Revise work from the raw or the note? | Revise = the current note; Regenerate = the raw transcript; both exist, the labels hide it → **D** |
| 6 | Where are the AI instructions; make them templates | `vnote/cleanup.py` constants; **styles** replace modes → **E** |

## A. Warm start (cheap)

- `serve()` starts `serve_forever` first; Whisper warms on a background thread.
  `transcribe._load_model()` gets a load lock so a request that arrives mid-warm
  waits for the same model instead of building a second one (today the warm thread
  and `_transcribe_pcm` → `transcribe()` would race on the bare `_model is None`).
- The same thread then makes Ollama ready, best-effort and never fatal:
  `_ensure_ollama_running()` + a preload `POST /api/chat {"model": …, "messages": []}`
  (per the Ollama API doc: an empty `messages` array loads the model; `keep_alive`
  defaults to `5m`). Every cleanup request sends `keep_alive` (setting
  `ollama_keep_alive`, default `30m`) so a finished note keeps the model hot for the
  next one. Only the `ollama` backend is warmed; VRAM headroom for Whisper + a 14B
  model on machines other than the Body's is unknown, hence best-effort.
- `/health` gains `warm: true|false` and `ollama: "ready"|"starting"|"absent"`.
  The page's daemon strip shows "warming large-v3-turbo …" while `warm` is false;
  **Record stays enabled** — live PCM spills to disk and the worker simply waits
  for the model; a `/api/note` upload waits the same way.

## D. Labels (trivial)

One **Instructions** box; the buttons read **Regenerate from raw** and **Revise
note**, and the box's label says it feeds both (today it reads "Revise this note"
but `regenerateNote()` reads it too — `app.js` `regenerateNote`).

## C. Process on stop + transcript editing (cheap–medium)

- A per-browser toggle **Process on stop** (localStorage, next to the live-transcript
  toggle). Off → Stop finishes with `raw=1`: a note folder with audio + transcript and
  no `note.md`; the note view opens on the raw pane with the Regenerate controls.
- The raw pane is editable. `PUT /api/notes/<name>/transcript {"text"}` rewrites
  `transcript.txt`; the first write copies Whisper's output to
  `transcript.original.txt` once and never overwrites it. Regenerate reads
  `transcript.txt` (it already does — `pipeline.resolve_redo`). A note's detail payload
  carries `transcript_edited: bool`.
- Built on the flat layout; F's lazy migration turns the flat files into `takes/1/`,
  so nothing here is built twice.

## E. Styles (medium)

**A style is one Markdown file**: front matter + body; the body is the instruction
the model gets (what `_MODE_INSTRUCTIONS[mode]` holds today).

```markdown
---
description: a Claude Code session prompt   # the dropdown line
output: plain                                 # note | plain (default note)
backend: claude-code                          # optional; blank = the Settings default
model:                                        # optional; blank = the backend's default
---
Turn the spoken brief into a session prompt in the speaker's voice …
```

- `output: note` → `TITLE:` line + Markdown body, `note.md` starts with `# Title`
  (today's behaviour). `output: plain` → body only, the title lives in `meta.json` and
  the sidebar, `note.md` has no heading — what `dictation` does today and what
  anything pasted elsewhere wants (Copy copies the editor verbatim). No `system`
  override in v1: the shared preamble carries the output contract.
- **Sources and precedence.** Built-ins shipped in the package (`vnote/styles/*.md`)
  · **Mine** = `~/.config/vnote/styles/` (next to `vocab.txt`) · optional
  `VNOTE_STYLES_DIRS` (a `os.pathsep` list; each folder is a dropdown group named
  after the folder). A later folder overrides an earlier one by name; every folder
  overrides the built-ins. New and edited styles are written to Mine; "Edit" on a
  built-in creates the override copy there. Dropdowns group: *Mine / <folder> /
  Built-in*, the plain file name inside. An unreadable folder is a warning in
  Settings, not a failed start. (The three-source model with an automatic
  `<notes_dir>/styles/` "Project" group was considered and cut — reachable later by
  adding that folder to the list; decided 2026-08-25.)
- **Ships:** `light`, `edit`, `summary` (today's text verbatim), `dictation` (plain),
  `prompt` (plain, `claude-code`), `email`. The `prompt` and `email` texts are first
  drafts the Body iterates on in the files.
- **In-app editor** in the Settings view: list with source + path, edit text, new
  (to Mine), duplicate, delete (Mine only) — `GET /api/styles`,
  `GET/PUT/DELETE /api/styles/<name>`, the vocabulary-editor pattern (mtime cache).
- **Styles replace modes.** `config.MODES` and the `mode` setting become the style
  registry and a `style` setting; CLI `--style NAME` (+ `--mode` as an alias,
  `--dictation` = `--style dictation`); the page's mode selects become style selects
  with `raw — no LLM` as the pseudo-entry; `meta.cleanup_mode` keeps its name and
  holds the style name, so **no note migration**. A note whose style no longer exists
  shows "(missing)" in the Regenerate select and falls back to `edit`. The
  `dictation_model` setting is retired (a `model:` line in the style file). Revise
  stays style-agnostic. The page's backend select gains "style default"; an explicit
  pick wins over the style's `backend`.

## F. Takes — continue recording, re-run, delete (bigger)

**Layout.** A note is a sequence of takes:

```
<note>/
  takes/1/audio.wav  transcript.txt  transcript.original.txt
  takes/2/…
  transcript.txt        ← derived: the takes' (edited) transcripts joined, rewritten
                          whenever a take is added, edited or deleted — every existing
                          reader (--redo, Regenerate, resolve_redo) keeps working
  note.md  versions/  meta.json (takes: [{n, created, duration_s}])
```

A single-take note keeps today's flat layout for as long as it has one take; the
first Continue moves the root `audio.*` / `transcript.txt` / `transcript.original.txt`
(C's kept copy — `transcript_edited` is derived from that file's existence, not a meta
flag) into `takes/1/` (same filesystem, a rename) and writes the derived root transcript. No bulk migration.
`duration_s` = the sum of the takes. Audio: one file per take, never appended; the
Audio tab lists takes (time, duration, a player each).

**Continue.** `#note-continue` (already in the markup) reuses the recording stage
with the live pane; the existing note sits in the drawer. Stop → `/stream/finish`
bound to the note (`?continue=<name>`) → the new take is transcribed and cleaned per a
per-browser select (remembered like the other picks):

| how | what the model sees | what happens to the note |
|---|---|---|
| **continue** (default) | the current note as read-only context + the new take's transcript; outputs only the continuation, same structure and voice | appended under a bare `---` |
| **append** | the new take's transcript alone | appended under `---` |
| **merge** | the current note + the new transcript in one prompt | the whole note rewritten |

Version entry `{op: "continue" \| "merge", take: n, how}`; the base is the current
version (edits and revisions included). Continuing a raw note appends a raw take and
cleans nothing. The raw pane shows takes as time-labelled sections.

**Re-run take n** — the same select, applied to the *current* note → a new version.
For the pre-take state, restore that version first (the version list shows
"continue: take 2"). `POST /api/notes/<name>/takes/<n>/rerun {how, style?, backend?}`.

**Regenerate from raw** — per-run checkboxes per take in the raw pane (≥ 1 required),
recorded as `takes: [1, 3]` on the version entry; the raw record keeps every take.

**Delete take** — `DELETE /api/notes/<name>/takes/<n>` moves `takes/<n>/` to
`<notes_dir>/trash/<note>/take-<n>/` after a confirm; rebuilds the joined transcript
and the duration; `note.md` is untouched (the Body regenerates or edits); take numbers
keep their gaps (history says `take: 2` and must stay true); refused on the last take.

**Delete note** — `DELETE /api/notes/<name>` → `<notes_dir>/trash/<name>/` after a
confirm naming the title; refused while a live session is bound to the note; the
sidebar drops it and the stage goes idle. Trash never auto-empties in v1: Settings
shows its path with Reveal; the guide says "empty it by hand". Trash is invisible to
the API; restoring is a folder move, documented. This is the app's first destructive
action — the rule from VNOTE-002/003 stands: the daemon never unlinks the only copy
of a recording; trash is a move.

## Contract additions (the boundary for the page)

| route | behaviour |
|---|---|
| `GET /health` | + `warm`, `ollama` |
| `PUT /api/notes/<name>/transcript` | `{"text"}` → rewrites `transcript.txt`; first write keeps `transcript.original.txt` |
| `GET /api/styles` · `GET/PUT/DELETE /api/styles/<name>` | the style registry; PUT/DELETE act on Mine |
| `POST /api/notes/<name>/reclean` | `mode` → a style name; + `takes: [n, …]` |
| `POST /stream/finish?continue=<name>&how=` | the daemon-held audio becomes take n of `<name>`; cleaned per `how` |
| `POST /api/notes/<name>/takes/<n>/rerun` | `{how}` → new version |
| `DELETE /api/notes/<name>/takes/<n>` · `DELETE /api/notes/<name>` | → trash |

`meta.json` additions: `takes`, `transcript_edited`; version entries gain `take`, `how`,
`takes`.

## Out of scope this round

Stats strip · search · empty-trash · opus compaction (I-002) · keyboard shortcuts
(I-003) · an automatic per-project styles folder (see E) · after-the-fact
re-selection of a take's section inside an edited note (re-run works on the current
note instead).

## Unknowns carried forward

- VRAM: Whisper + the default 14B model coexist on the Body's 4090; other machines
  unknown → the Ollama warm is best-effort and logged, never fatal.
- Whether `continue` (context-aware) beats `append` in practice is a hypothesis; the
  select exists so the Body can compare on real notes.
