"""Render each note folder's minutes (<folder>_minutes.md, or legacy minutes.md) into a standalone
HTML page <folder>_minutes.html, ready to publish as an artifact (one self-contained file each).

    uv run --with markdown python scripts/minutes_to_html.py ~/voice-notes OUT_DIR

Needs the `markdown` package (not a vnote dependency, hence `--with`). Raw '<' in the minutes is
escaped before rendering, so a transcript can't inject HTML. Fonts load from Google Fonts with
system fallbacks; colours are theme tokens with light and dark variants.
"""
# ruff: noqa: E501  (the embedded CSS template has long declaration lines)
import argparse
import json
import re
from pathlib import Path

import markdown

ap = argparse.ArgumentParser()
ap.add_argument("root", type=Path, help="notes dir (e.g. ~/voice-notes)")
ap.add_argument("out", type=Path, help="directory for the .html files (created if missing)")
args = ap.parse_args()
root, out = args.root.expanduser(), args.out.expanduser()
out.mkdir(parents=True, exist_ok=True)
TEMPLATE = """<title>@@NAME@@</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Serif:wght@500;600&display=swap">
<style>
/* Layout: one narrow reading column; a monospaced file-tag strip on top carries the folder name, date and audio length. */
:root {
  --bg: #f5f6f7; --surface: #ffffff; --fg: #1d2327; --muted: #5d6870; --rule: #d9dee2;
  --accent: #1b6b86; --accent-soft: #e3f0f5;
  --font-display: "IBM Plex Serif", Georgia, serif;
  --font-body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #14181b; --surface: #1b2024; --fg: #e4e8ea; --muted: #97a3ab; --rule: #2d353a;
  --accent: #6fc0da; --accent-soft: #1d2f37; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #14181b; --surface: #1b2024; --fg: #e4e8ea; --muted: #97a3ab; --rule: #2d353a;
  --accent: #6fc0da; --accent-soft: #1d2f37; color-scheme: dark; }
body { background: var(--bg); color: var(--fg); font-family: var(--font-body); font-size: 16px; line-height: 1.65; padding-inline: 16px; padding-block: 28px 64px; }
main { max-width: 44rem; margin-inline: auto; }
.tag { display: flex; flex-wrap: wrap; gap: 6px 18px; font: 500 12px/1.5 var(--font-mono); color: var(--muted); padding-bottom: 14px; border-bottom: 1px solid var(--rule); margin-bottom: 26px; }
.tag .file { color: var(--accent); overflow-wrap: anywhere; min-width: 0; }
.doc h1 { font: 600 clamp(1.7rem, 4.6vw, 2.25rem)/1.2 var(--font-display); text-wrap: balance; margin: 0 0 1.4rem; }
.doc h2 { font: 600 1.3rem/1.3 var(--font-display); text-wrap: balance; margin: 2.4rem 0 .6rem; padding-top: 1.2rem; border-top: 1px solid var(--rule); }
.doc h3 { font: 600 .82rem/1.4 var(--font-body); letter-spacing: .07em; text-transform: uppercase; color: var(--accent); margin: 1.6rem 0 .4rem; }
.doc p, .doc ul, .doc ol { margin: 0 0 .95rem; }
.doc ul, .doc ol { padding-left: 1.3rem; }
.doc li { margin-bottom: .3rem; min-width: 0; }
.doc li > ul, .doc li > ol { margin: .3rem 0 .3rem; }
.doc li::marker { color: var(--muted); }
.doc strong { font-weight: 600; }
.doc blockquote { margin: 1rem 0; padding: .1rem 1rem; background: var(--accent-soft); border-radius: 4px; }
.doc a { color: var(--accent); }
.doc code { font: 400 .9em var(--font-mono); background: var(--surface); border: 1px solid var(--rule); border-radius: 3px; padding: .05em .3em; }
.doc table { display: block; overflow-x: auto; border-collapse: collapse; margin: 0 0 1rem; }
.doc th, .doc td { border: 1px solid var(--rule); padding: .35rem .7rem; text-align: left; }
.doc pre { overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); padding: .8rem 1rem; border-radius: 4px; }
.doc { overflow-wrap: break-word; }
</style>
<main>
  <div class="tag"><span class="file">@@NAME@@.md</span><span>@@DATE@@</span>@@DUR@@</div>
  <article class="doc">
@@BODY@@
  </article>
</main>
"""
n = 0
for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
    md = next((p for p in (d / f"{d.name}_minutes.md", d / "minutes.md") if p.is_file()), None)
    if md is None:
        continue
    name = f"{d.name}_minutes"
    text = md.read_text(encoding="utf-8").replace("<", "&lt;")
    body = markdown.markdown(text, extensions=["sane_lists", "tables"])
    m = re.match(r"(\d{4}-\d{2}-\d{2})", d.name)
    date = m.group(1) if m else ""
    dur = ""
    try:
        s = json.loads((d / "meta.json").read_text()).get("audio_duration_s")
        if s:
            dur = f"<span>{int(s)//3600}h {int(s)%3600//60:02d}m of audio</span>" if s >= 3600 else f"<span>{max(1, round(s/60))} min of audio</span>"
    except Exception:
        pass
    (out / f"{name}.html").write_text(
        TEMPLATE.replace("@@NAME@@", name).replace("@@DATE@@", date).replace("@@DUR@@", dur).replace("@@BODY@@", body),
        encoding="utf-8")
    n += 1
print(n, "pages")
