# Design brief for Claude Design — vnote's web interface

## What vnote is

vnote is a personal, local voice-notes tool. You talk; it transcribes on your own GPU
(faster-whisper) and tidies the transcript into clean Markdown with an LLM (a local
Ollama model, or the user's Claude subscription). Nothing leaves the machine unless the
user chose the Claude backend. It lives in a browser tab served by a small daemon at
`http://127.0.0.1:8760` — one page, no framework, no build step, no external assets.

The person using it records **thinking-out-loud notes of 2–7 minutes, several times a
day**, then copies the result into other tools (docs, chats, task lists). Sometimes a
note is a 10-second reminder. They are technical, use dark mode, and run the daemon
inside WSL2 while the page is open in a Windows browser. It's a tool they use, not a
product they sell — calm, fast, text-first, no onboarding, no accounts.

## The inspiration: Wispr Flow — what to borrow, what not

Wispr Flow is cloud dictation whose desktop app has two faces: a tiny always-there
capture surface (a bar that shows your words appearing as you speak), and its own
window with a **left sidebar** — history grouped by day (Today, Yesterday, dates) with
a small stats strip on top, a personal Dictionary, Snippets, Styles, Settings.
([Navigating the app](https://docs.wisprflow.ai/articles/5096240724-navigating-the-wispr-flow-app-desktop-ios-and-android),
[Styles](https://docs.wisprflow.ai/articles/2368263928-how-to-setup-flow-styles).)

Borrow: the feeling that **text appears while you talk**; the history-by-day sidebar as
the app's spine; a dictionary that is a first-class page (ours is "Vocabulary": hotwords
and corrections); restraint. Do not borrow: the cloud/account/plan chrome, snippets,
per-app styles, or a floating overlay — vnote is a page, and it records *notes*, not
keystrokes into other apps.

## What exists today (v0.5.0) — functional, plain, to be replaced

A single page with three tabs. **Record**: mode/backend/language pickers, a big Record
button, Pause/Resume, Stop, a timer, then a result box with the Markdown and a Copy
button. **Notes**: a list, and a detail with the note text, the transcript, an audio
player, Copy, and Re-clean in another mode. **Settings**: a table of every setting with
a description and where its value comes from (env / file / default), plus a Vocabulary
editor. It works; it looks like a form. Behaviour lives in a JavaScript file that finds
elements **by id**, so your markup can replace the page entirely as long as the ids in
the contract below exist.

## Hard constraints

- Output is **standalone HTML + CSS** (no framework, no JS needed from you, no external
  fonts/CDNs — system font stack; a Google Fonts link is acceptable if you must).
- Every element in the **id contract** (below) exists exactly once. Extra elements are
  fine; missing ids break the app.
- Works from ~360 px (phone) to wide desktop (1400+ px). Light **and** dark
  (`prefers-color-scheme`), dark is the primary use.
- Long text everywhere: a 7-minute note is 1,000+ words of Markdown; the raw
  transcript is one long paragraph. Text areas must scroll inside themselves; the page
  never scrolls horizontally.
- The processed note is edited **as Markdown** in a plain text editor (a textarea or
  equivalent) — no rich-text editing.
- No login, no cloud, no multi-user, no notifications, no onboarding.

## The screens and states to design

Design these as separate pages (or clearly labelled states). Every screen shares the
sidebar and top chrome.

### A. The frame

- **Left sidebar** (collapsible): a **New note** action at the top; the notes list
  grouped by day (Today / Yesterday / Aug 22 …), each row = title (first words), time,
  duration, and a small mode tag (light / edit / summary / dictation / raw); the active
  note highlighted; an empty state for "no notes yet". Room for a search box (may be
  hidden in v1) and a small stats strip if it earns its place (notes this week, minutes
  recorded). At the bottom: **Settings**, and a one-line daemon status ("vnote 0.5.0 ·
  large-v3-turbo on cuda", or "daemon unreachable").
- **Main stage**: whatever is open — a new recording, or an existing note.

### B. New note — idle

The stage invites recording: the Record button dominant; per-recording picks nearby
but quiet (mode, backend, language) with the live-transcript toggle; a hint about
Space = pause. Nothing else competes.

### C. Recording — live transcript on

The words appear as you speak. The **live transcript is the stage**: committed text
settles in place (it must stay selectable — the user copies mid-sentence), the last
few words render slightly differently to show they may still change. A compact
**recording bar** (timer of *recorded* time, red state, Pause, Stop) stays visible
while the transcript grows and scrolls. A **Copy** for the live text is always
reachable. **Paused** variant: amber state, timer frozen, text still selectable,
Resume replaces Pause. Also design the variant with **live transcript off** (just the
bar and the timer — the user chose not to see text).

### D. Stop pressed — processing

The live text stays exactly where it was and stays copyable. A calm progress state
says what's happening ("transcribing … then cleaning — usually 20–40 s; no progress
bar is possible") without a spinner that screams. When done, the screen becomes E.

### E. A finished note (also what opening a note from the sidebar shows)

This is the heart of the app. Show:

- Title (editable inline is a bonus), created time, duration, mode, backend.
- The **processed note** as the primary text, **editable** (Markdown), with **Save**
  and **Copy**.
- The **raw transcript** as a secondary pane — see "two layout explorations".
- **Re-process controls**: pick another mode and **Regenerate** (from the raw
  transcript), or type an instruction — "make it shorter", "turn the second half into
  a checklist" — and **Revise** (applied to the current note). A short status line
  while it runs.
- **Versions**: a small picker — *v3 · revise: "shorter" · 14:32* / *v2 · regenerate:
  summary* / *v1 · original* — reading an old one and **Restore** (which becomes a new
  version). Not a tree; a straight line.
- An **audio player** (seekable), the folder **path** with a copy affordance and an
  **Open folder** button.
- A **Continue recording** affordance, present but disabled ("coming") — it will append
  more audio to this note later; give it a home in the layout now.
- A warning strip for the case "cleanup failed — this is the raw transcript".

### F. Settings

Every setting with its description, its current value, and a small source badge
(`env` / `file` / `default`); rows that can't be edited here say why ("set VNOTE_X and
restart the daemon" / "overridden by VNOTE_X"). A Save with a status line. Below it,
**Vocabulary**: a plain text box (one entry per line: a bare word biases
transcription; `wrong -> right` corrects it afterwards) with Save and a two-line
explanation. This page can be plain; it must be scannable.

### G. Edge states (small, but design them)

Microphone denied or page not on localhost ("needs http://localhost:8760"); upload
failed with a **Retry** that reuses the recording; daemon unreachable (everything
read-only, sidebar still works); cleanup failed (raw shown, warning); empty history.

## Two layout explorations (we will pick from your pictures)

1. **Stage + drawer** — while recording, the live transcript is the whole stage; once
   the note exists, the processed note takes the stage and the raw transcript becomes
   a secondary pane: side by side on wide screens, a tab or collapsible panel below
   ~1100 px.
2. **Always side by side** — two columns from the first second: raw on one side
   filling as you speak, processed on the other filling on completion; the editor
   lives in the processed column.

Show both for screens C and E at desktop width, and how each collapses on a phone.

## Interaction details to honour

- Space toggles pause while recording (unless typing in a field); Enter/Escape are not
  bound. Everything reachable by keyboard.
- Copy is everywhere text is: live transcript, processed note, raw transcript, path.
  A copy confirms with a short inline "copied", not a toast pile.
- Recording state colours: red = recording, amber = paused, neutral/grey = processing;
  the timer counts recorded time only.
- The sidebar is the only navigation; the app has no other pages.
- Mode names and one-line meanings, for labels and tooltips: **light** — fix fillers
  and grammar only · **edit** — reorganize into headings and lists (default) ·
  **summary** — condense · **dictation** — plain text, fast, no title · **raw** — the
  transcript, no LLM.

## Visual direction

Calm and text-first. Generous line length for the note (~70–80 characters), a readable
proportional face for the note, monospace for the raw transcript and the vocabulary.
One accent for the recording state; otherwise near-monochrome with clear hierarchy.
Dark mode as the primary composition, light as its mirror. No illustrations, no
marketing. It should feel like a good writing tool that happens to listen.

## Deliverables

- Standalone HTML/CSS for the frame and each screen/state above (A–G), in both layout
  explorations for C and E; dark and light.
- Use the ids below verbatim. Put no behaviour in the markup (no inline scripts or
  `on*=` attributes); one `<script src="/static/app.js" defer></script>` and one
  `<link rel="stylesheet" href="/static/style.css">` are all the wiring.
- Notes on anything you changed in the content model or think we got wrong.

## The element-id contract

| id | role |
|---|---|
| `sidebar` · `sidebar-toggle` | the notes sidebar and its collapse control |
| `new-note` | start a new recording |
| `notes-list` · `notes-empty` · `notes-search` | the grouped notes list, its empty state, an optional search box |
| `nav-settings` · `daemon-info` | open Settings; the one-line daemon status |
| `view-note` · `view-settings` | the two stages; one visible at a time |
| `record` · `pause` · `stop` · `timer` · `rec-status` | transport; `pause` toggles its label Pause/Resume |
| `live-toggle` · `pick-mode` · `pick-backend` · `pick-language` | per-recording picks |
| `retry` · `mic-help` | retry a failed upload; the microphone/secure-context help |
| `live` · `live-committed` · `live-tail` · `live-copy` · `live-copy-status` | the live transcript: container, settled text, the still-changing tail, copy |
| `process-status` | the processing / re-processing status line |
| `note-title` · `note-meta` · `note-warning` | title, meta line, "cleanup failed" strip |
| `note-editor` · `note-save` · `note-save-status` · `note-copy` · `note-copy-status` | the processed note (editable Markdown) and its actions |
| `note-raw` · `note-raw-toggle` · `note-raw-copy` | the raw transcript pane, its show/hide, copy |
| `regenerate-mode` · `regenerate` · `revise-instructions` · `revise` | re-process controls |
| `version-select` · `version-restore` | the version picker and Restore |
| `note-audio` · `note-path` · `note-path-copy` · `note-reveal` | player, folder path + copy, Open folder |
| `note-continue` | the (disabled for now) Continue recording affordance |
| `settings-table` · `settings-save` · `settings-status` | the settings rows (a `<tbody>` we fill), Save, status |
| `vocab` · `vocab-save` · `vocab-status` | the vocabulary editor |

## Data you can show (reference)

Note list rows: name, title, created (ISO), duration_s, mode, backend, has_audio,
has_note. Note detail: those plus the Markdown note, the raw transcript, an audio URL,
the folder path, meta (model names, transcribe/cleanup seconds, language), and the
versions list ({n, created, op: clean | regenerate | revise | edit | restore, mode,
backend, instructions}). Live recording: the committed transcript and the current
tail. Settings rows: key, env var, value, default, description, kind (choice / text /
path / int), choices, source (env / file / default), editable. Daemon health: version,
device, whisper model.

## Out of scope

Continue-recording behaviour (only its affordance), search, stats beyond a small strip,
mobile capture, auth, sharing, themes beyond light/dark.
