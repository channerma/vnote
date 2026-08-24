"""Guardrails for the static page in vnote/web/ — the contract between markup and app.js.

The page is a design export wired by ids: index.html carries no behaviour, app.js finds
every element by id and lists them in its header comment. These checks keep a restyled
page and the script from drifting apart, and keep the page free of external requests.
"""

import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "vnote" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")
WORKLET = (WEB / "pcm-worklet.js").read_text(encoding="utf-8")
# The design keeps a documentation skeleton inside an HTML comment; comments carry no ids or behaviour.
MARKUP = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
CSS_CODE = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def _ids() -> list[str]:
    return re.findall(r'\bid="([^"]+)"', MARKUP)


def test_every_id_is_unique_and_used_by_app_js():
    ids = _ids()
    assert ids, "no ids in index.html?"
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate ids: {dupes}"
    unreferenced = [i for i in ids if f"'{i}'" not in JS and f'"{i}"' not in JS and f"#{i}" not in JS]
    assert not unreferenced, f"ids in index.html that app.js never uses: {unreferenced}"


def test_header_table_matches_the_markup():
    header = JS.split("*/", 1)[0]
    documented = set(re.findall(r"#([a-z][a-z0-9-]*)", header))
    ids = set(_ids())
    assert ids <= documented, f"ids missing from app.js's header table: {sorted(ids - documented)}"
    assert documented <= ids, f"header table lists ids the page lacks: {sorted(documented - ids)}"


def test_markup_carries_no_behaviour():
    assert len(re.findall(r"<script\b", MARKUP)) == 1
    assert re.search(r'<script src="/static/app\.js" defer>\s*</script>', MARKUP)
    assert not re.search(r"\son[a-z]+=", MARKUP), "inline event handler in index.html"
    assert not re.search(r"javascript:", MARKUP, re.I)


def test_page_makes_no_external_requests():
    for name, text in (("index.html", MARKUP), ("style.css", CSS_CODE), ("app.js", JS), ("pcm-worklet.js", WORKLET)):
        hits = [m for m in re.findall(r"https?://[^\s\"')]+", text)
                if not re.match(r"https?://(127\.0\.0\.1|localhost)", m)]
        assert not hits, f"{name} references an external URL: {hits}"
    assert "@import" not in CSS_CODE, "the page must not fetch a web font"


def test_worklet_stays_in_its_scope():
    code = "\n".join(line for line in WORKLET.splitlines() if not line.lstrip().startswith(("*", "//", "/*")))
    assert not re.search(r"\b(document|window|fetch|localStorage)\b", code)


def test_stub_dom_smoke_harness():
    """Runs app.js against a fake DOM/fetch/AudioContext (tests/web/smoke.js) when node is available."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(Path(__file__).parent / "web" / "smoke.js")],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "all checks passed" in proc.stdout
