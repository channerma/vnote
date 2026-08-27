# Handoff: vnote web interface (layout 1a — stage + drawer)

## Overview

vnote is a local voice-notes tool served by a small daemon at `http://127.0.0.1:8760`. You
talk; it transcribes on your own GPU (faster-whisper) and tidies the transcript into Markdown
with a local Ollama model or a Claude subscription. This handoff replaces the v0.5.0 three-tab
page with a single-stage app: a **history sidebar** as the only navigation, and a **main stage**
that is either a recording (live transcript fills the stage) or a note (processed Markdown on
the stage, raw transcript in a drawer beside it).

The chosen exploration is **1a — stage + drawer**. The rejected alternative (1b, two columns
from the first second) is not part of this bundle.

## About the design files

`index.html` and `style.css` in this folder are **design references written as real HTML/CSS** —
they show the intended look, states and responsive behaviour. In this project's case they are
unusually close to shippable: the target "codebase" is a single static page served from
`/static/`, with no framework and no build step, so the intended path is to **drop these two
files into the daemon and wire the existing `app.js` to them** rather than to re-implement.
If you instead port this into a framework, treat the CSS as the spec and keep the id contract.

`Board.dc.html` is the full design board that this page was extracted from (every screen and
state side by side, including the rejected 1b exploration and design notes). It is a design
document, not code to ship.

## Fidelity

**High fidelity.** Final colours, type, spacing, states and copy. Recreate exactly.

## Files

| File | What it is |
|---|---|
| `index.html` | The page. Serve as the daemon's HTML. Contains every contract id exactly once, one `<link href="/static/style.css">`, one `<script src="/static/app.js" defer>`, and no inline behaviour. |
| `style.css` | The whole design. Deploy to `/static/style.css`. |
| `preview.html` | Throwaway copy with relative paths so you can open the design in a browser without the daemon. Do not ship. |
| `Board.dc.html` | The design board (all screens, both explorations, notes). Reference only. |
| `screenshots/` | Rendered states, 1280px desktop and 390px phone. |

## Screens and states

State is **CSS-driven from data attributes**; `app.js` sets attributes, CSS does the rest. No
element show/hide logic in JS beyond these attributes and the `hidden` flags.

```
<div class="app" data-view="note|settings" [data-daemon="down"]>
  <section id="view-note" data-state="idle|recording|paused|processing|note" data-live="on|off">
    <article class="note" data-raw="shown|hidden">
```

Plus `hidden` on `#mic-help`, `#retry-wrap`, `#note-warning`, `#notes-empty` / `#notes-list`.

### A. Frame (every screen)

`.app` is `display:flex; height:100%`, `body { overflow:hidden }` — the page never scrolls;
panes scroll inside themselves.

- **`#sidebar`** — 264px fixed (`flex:0 0 264px`), `background var(--panel)`, `border-right:2px solid var(--rule)`.
  - `.side-head` (padding 14/16/12, `border-bottom:2px solid var(--rule)`): brand `vnote` 15px/800, `#sidebar-toggle` 28×28 icon button (Lucide `panel-left`).
  - `.side-actions`: `#new-note` full-width accent button (Lucide `plus`, label flush left, 10px 12px, 14px/800); `#notes-search` full-width input; `.stats` strip — 11px uppercase, `var(--faint)`, "7 notes this week / 29 min". **Delete that one div to remove the stats strip.**
  - `#notes-list` — `flex:1; overflow-y:auto`. `.day` group headers (10px/800, .14em tracking, uppercase, `var(--faint)`): Today / Yesterday / Aug 22. `.note-row` = 8/16/9 padding, 2px transparent left border, title 13.5px/600 truncated with ellipsis, meta row 11px `var(--dim)` (time, duration) and a `.tag` (9.5px uppercase, 1px border) pushed right. `.note-row.is-active` = `background var(--surface)` + `border-left-color var(--accent)`. Hover = 6% text tint.
  - `#notes-empty` — swap with `#notes-list` via `hidden`. "No notes yet" + one line of body copy.
  - `.side-foot` (`border-top:2px solid var(--rule)`): `#nav-settings` link button with gear icon; `#daemon-info` mono 10.5px `var(--faint)` — `vnote 0.5.0 · large-v3-turbo on cuda`, or `daemon unreachable`.
- **`.stage`** — `flex:1; min-width:0; display:flex; flex-direction:column`. Holds `#view-note` and `#view-settings`; `data-view` on `.app` picks one (both are flex columns with `min-height:0`).

Screenshot: `screenshots/15-empty-history.png` (empty sidebar + first-run stage).

### B. New note — idle · `data-state="idle"`

Screenshot: `screenshots/01-new-note-idle.png`

Top bar shows only `#rec-status` ("New note"). `.idle` is the stage: h1 38px/800 "Ready when you
are.", `.lede` 15px/1.6 `var(--dim)` max 58ch with a `<kbd>Space</kbd>`, then `#record` — accent
button, 18px/800 label, flush left, 14px white dot before the label. Below a 2px `.rule`, the
`.picks` row (`display:flex; gap:26px; align-items:flex-end`, max-width 860px): `#pick-mode`,
`#pick-backend`, `#pick-language` (each a `.field` with a 10px uppercase label above) and the
`#live-toggle` checkbox with "Show live transcript". `.hint` line below at 12.5px `var(--faint)`.

Mode meanings, used for labels and tooltips: **light** fix fillers and grammar · **edit**
reorganize into headings and lists (default) · **summary** condense · **dictation** plain text,
fast, no title · **raw** the transcript, no LLM.

### C. Recording · `data-state="recording"` (and `"paused"`, and `data-live="off"`)

Screenshots: `02-recording-live-on.png`, `03-paused.png`, `04-live-transcript-off.png`

The live transcript **is** the stage. `.topbar` (min-height 60px, `padding 10px 26px`) turns:
`border-bottom:2px solid var(--accent)` and `background: color-mix(in srgb, var(--accent) 10%, transparent)`;
`.dot` 11px filled `var(--accent)` blinking on a 1.6s step (suppressed under
`prefers-reduced-motion`); `#rec-status` "Recording" 11px/800 .16em uppercase in `var(--accent-hi)`;
`#timer` mono 22px/600 counting **recorded** time only; `#pause` secondary and `#stop` accent;
right side `#live-copy-status` + `#live-copy`.

`#live` is `flex:1; overflow-y:auto; padding:36px 26px 26px`. `.live-inner` is max-width 74ch:
a "Live transcript" micro-label, then `.live-text` at **19px/1.78**, `#live-committed` in
`var(--text)` and `#live-tail` in `var(--dim)` with a 1px dotted underline. Both are ordinary
selectable text — never a canvas, never `user-select:none`; the user copies mid-sentence.

**Paused** (`data-state="paused"`): top bar and status switch to `var(--accent-hi)`, the dot
becomes a 2px hollow ring, `#timer` drops to `var(--dim)` and freezes, `#pause` gets an
accent-hi border (its label becomes "Resume" — set by app.js). Text stays selectable. There is
deliberately **no second hue**: the design system is mono, so paused reads from shape + label +
frozen timer rather than amber.

**Live transcript off** (`data-live="off"`): `.live-inner` hides and `.big-timer` (mono 64px/600)
takes its place. Everything else is identical.

### D. Stop pressed — processing · `data-state="processing"`

Screenshot: `05-processing.png`

The live text stays exactly where it was, dimmed to `opacity:.72`, still copyable. Top bar goes
neutral (`background var(--panel)`, hollow grey dot, dim timer); transport buttons hide. A 2px
`.progress` rail with a 22%-wide accent bar crawling left→right (2.2s linear, static at 40%
opacity under reduced motion) sits under the bar — deliberately indeterminate, because no honest
percentage exists. `#process-status` below it: "Transcribing 6:41 of audio, then cleaning with
edit — usually 20–40 s." When the note arrives, `data-state` becomes `note`.

### E. A finished note · `data-state="note"`

Screenshot: `06-finished-note.png` (desktop), `09-phone-note.png` (390px)

- `.note-head` — `padding:18px 26px 14px`, `border-bottom:2px solid var(--rule)`, wraps.
  `#note-title` is `contenteditable`, 27px/800, max 34ch, 1px dashed bottom border (the edit
  affordance). `#note-meta` 12px `var(--dim)`, gap 14px: created time, duration, mode, backend,
  whisper model + language, transcribe/clean seconds.
  Right: `#version-select` (mono 12px, max 280px — `v3 · revise: "shorter" · 14:32` / `v2 ·
  regenerate: summary` / `v1 · original`), `#version-restore`, and `#note-continue`, **disabled**
  with a dashed border and a dot glyph ("Continue recording" — coming later; it keeps its place
  in the layout now).
- `#note-warning` — hidden by default; the "cleanup failed, this is the raw transcript" strip
  (accent-hi 2px top rule + 10% tint).
- `.note-body` — `display:flex`. `.pane-note` `flex:1.55` with `border-right:2px solid var(--rule)`;
  `.pane-raw` `flex:1; max-width:420px; background var(--panel)`.
  - `.pane-head` (10px 20px, 1px bottom rule): micro-label, then `#note-save-status` and
    `#note-copy-status` (11.5px `var(--faint)`), then **`#note-save` (accent) and `#note-copy`
    (secondary) paired at the right edge**.
  - `#note-editor` — a plain `<textarea>`, `flex:1; min-height:0`, no border, `background var(--bg)`,
    **16.5px/1.72 Archivo**, padding 24px 22px. Markdown is edited as text; no rich text.
  - `#note-raw` — mono 12.5px/1.72 `var(--dim)`, `overflow-y:auto`, its own `#note-raw-copy` and
    `#note-raw-toggle` ("Hide" → sets `.note[data-raw="hidden"]`).
- `.note-foot` — `border-top:2px solid var(--rule)`, `background var(--panel)`.
  - `.reprocess` row: `#regenerate-mode` + `#regenerate` (re-reads the raw transcript), a 2px
    `.vrule`, `#revise-instructions` (placeholder "turn the second half into a checklist") +
    `#revise` (applies to what is in the editor). Either produces a new version.
  - `.artefacts` row (1px top rule): `#note-audio` (native `<audio controls>`, 34px tall, 340px
    wide), `#note-path` as bordered mono text, `#note-path-copy`, `#note-reveal` ("Open folder").

Copy confirmations are the inline `*-status` spans next to each button ("copied", "saved 14:33"),
cleared after a couple of seconds. **No toasts.**

### F. Settings · `.app[data-view="settings"]`

Screenshot: `07-settings-vocabulary.png`

`#settings-table` is a `<tbody>` the app fills, one row per setting: name 13.5px/600 with the env
var below it in mono 11px `var(--faint)`; description in `var(--dim)`; the value as a `select` or
`input` (full width) when editable, or as mono text plus a `.why` line ("set VNOTE_MODEL and
restart the daemon" / "overridden by VNOTE_DEVICE") when not; `.badge` source chip (`file`,
`default`, or `env` in accent-hi). Header cells are 10px uppercase with a 2px bottom rule; rows
are separated by 1px rules. `#settings-save` + `#settings-status` live in the top bar.

Below a 2px rule, the vocabulary block: h2 19px, `#vocab-status`, `#vocab-save`, a two-line
explanation, and `#vocab` — a mono 13px textarea, 180px tall, resizable vertically. One entry per
line; a bare word biases transcription, `wrong -> right` corrects it afterwards.

### G. Edge states

Screenshots: `11-edge-mic-blocked.png`, `12-edge-upload-failed.png`,
`13-edge-daemon-unreachable.png`, `14-edge-cleanup-failed.png`

All four are the same `.banner` treatment — 2px `var(--accent-hi)` bottom rule over a 10% tint,
a 16px/800 headline and a `var(--dim)` line of body copy — never a modal.

- **Microphone blocked** — `#mic-help`: permission denied or not a secure context; tells the user
  to open `http://localhost:8760` (not the WSL IP) and allow the mic from the address-bar icon.
- **Upload failed** — `#retry-wrap` + `#retry`: the recording is still held in the tab; Retry
  re-sends the same audio; don't close the tab.
- **Daemon unreachable** — `.app[data-daemon="down"]`: `#record`, `#stop`, `#new-note` drop to 45%
  and stop taking pointer events; the sidebar and everything on disk still work; `#daemon-info`
  reads "daemon unreachable".
- **Cleanup failed** — `#note-warning` visible on a note whose editor holds the raw transcript.

## Interactions and behaviour

- **Space** toggles pause while recording, unless focus is in a field. Enter/Escape unbound.
  Everything is reachable by keyboard; focus is `2px solid var(--accent)` with 2px offset — never
  the browser default.
- `#pause` toggles its own label Pause/Resume; `#sidebar-toggle` collapses the sidebar (below
  860px it becomes an off-canvas overlay driven by `#sidebar[data-open="true"]`).
- Copy exists everywhere text does: live transcript, note, raw transcript, path. Each confirms in
  its adjacent inline status span.
- State colours: **red = recording**, **accent-hi hollow = paused**, **neutral grey = processing**.
  The timer counts recorded time only (it must not advance while paused).
- Long text: `#note-editor`, `#note-raw`, `#live`, `#notes-list`, `.settings-scroll` each scroll
  inside themselves. The page never scrolls horizontally.
- Responsive: **≤1100px** the raw pane stops being a column and stacks under the editor (the whole
  `.note` becomes the scroll container, `.note-body` sizes to content); on phone that stack is
  presented as Note / Raw / Audio tabs (see `09-phone-note.png` — implement the tabs by toggling
  `.note[data-raw]` plus a small active-tab class). **≤860px** the sidebar becomes an overlay,
  `#sidebar-toggle` pins to the top-left, the reprocess row stacks, and every button gets a
  44px minimum height. Phone recording: timer + Pause/Stop as two full-width buttons at the bottom
  (`08-phone-recording.png`).

## State the app must hold

`view` (note | settings) · `noteState` (idle | recording | paused | processing | note) ·
`liveOn` · recorded seconds · committed transcript + current tail · the open note (title, markdown,
raw, meta, versions, audio URL, path) · dirty flag for the editor · per-recording picks (mode,
backend, language) · daemon health (version, device, whisper model) · notes list grouped by day ·
settings rows (key, env var, value, default, description, kind, choices, source, editable) ·
vocabulary text.

## Design tokens (from the Modernist design system)

Dark is primary; the light block is the `prefers-color-scheme: light` mirror. Both live at the top
of `style.css` — take values from the variables, not from these numbers, when you can.

| Token | Dark | Light |
|---|---|---|
| `--bg` | `#201e1d` | `#f3f2f2` |
| `--panel` | `#191817` | `#eae9e9` |
| `--surface` | `#2a2827` | `#f8f4f4` |
| `--text` | `#f3f2f2` | `#201e1d` |
| `--dim` | `#9b9797` | `#605d5d` |
| `--faint` | `#8f8b8b` | `#6f6c6c` |
| `--line` | `rgba(243,242,242,.16)` | `rgba(32,30,29,.18)` |
| `--rule` | `rgba(243,242,242,.34)` | `rgba(32,30,29,.4)` |
| `--accent` | `#ec3013` | `#ec3013` |
| `--accent-hi` | `#ff9783` | `#ae1800` |

- **Type**: Archivo 400 / 600 / 800 (Google Fonts `@import` at the top of the stylesheet — the only
  external request; drop it and the system stack takes over). Body 15px/1.55. Headings 800,
  `letter-spacing:-.015em`. Note editor 16.5px/1.72. Live transcript 19px/1.78. Micro-labels 10px/800
  at .14em uppercase. Mono (`ui-monospace, SFMono-Regular, Menlo, Consolas`) for raw transcript,
  paths, env vars, vocabulary and timers.
- **Radius: 0 everywhere.** No shadows except the off-canvas sidebar. Rules are 2px between major
  sections, 1px within a section — never hairlines.
- **Spacing**: 4 / 8 / 12 / 16 / 24 / 32; stage padding 26px, sidebar 16px, panes 20–22px.
- **Buttons**: labels flush left even when the button is wider than its label. Primary = solid
  accent, hover `#dd2b0f`, active `#ae1800`. Secondary = 1px `var(--line)` with a 7% / 14% text tint
  on hover / active. Disabled = 45% opacity.
- **Accent discipline**: red is the primary action and the recording state, nothing else.

## Assets

None. Icons are inline Lucide SVG paths (`panel-left`, `plus`, settings gear) at 15px, `stroke-width:2`,
`fill:none`, `stroke:currentColor`. No images, no illustrations, no icon font.

## Notes on the content model

- Versions are a straight line: a `<select>` plus Restore, where the option label carries op and
  instruction. No tree, no diff view.
- Regenerate reads the **raw transcript**; Revise edits **what is in the editor**. Both create a new
  version, so Restore is never destructive.
- The stats strip is one muted line and is trivially removable (one `div`).
- Worth questioning: "live transcript off" leaves a stage with nothing but a giant timer. If nobody
  uses it, cutting the toggle removes an entire state.
