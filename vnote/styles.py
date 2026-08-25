"""Cleanup styles: one Markdown file per style, discovered from three sources.

A style file is an optional front matter block plus a body; the body *is* the
instruction the model gets (what a hardcoded "mode" held until 0.7.0):

    ---
    description: an email draft   # the line the dropdown shows
    output: plain                 # note | plain (default note)
    backend: claude-code          # optional; blank = the backend setting
    model:                        # optional; blank = the backend's default
    ---
    Turn the transcript into an email the speaker could send ...

``output: note`` keeps the ``TITLE:``/``---`` contract and a ``# Title`` heading
in note.md; ``output: plain`` asks for the text alone and keeps the title in
meta.json only.

Sources, each overriding the one before it *by name*: the built-ins shipped in
``vnote/styles/`` · **Mine** = ``<config dir>/styles/`` (next to vocab.txt) ·
every folder in ``VNOTE_STYLES_DIRS``, in order. Mine is what the in-app editor
writes, so "edit" on a built-in creates an override there — and a later extra
folder can still win the name.

Loaded with an mtime cache, like vocab.py: edits apply to the next note, no
daemon restart. A folder that will not open and a file that will not parse are
``problems`` lines the Settings page shows — never a failed start.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

# A style's name is its file stem, and it travels in URLs and query strings.
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
# The record panel's "raw — no LLM" entry is not a style: a style file of that name
# would appear twice in the dropdown and never run, because raw short-circuits first.
RESERVED_NAMES = ("raw",)
OUTPUTS = ("note", "plain")
FRONT_MATTER_KEYS = ("description", "output", "backend", "model")
# Only the constrained fields take a trailing "# ..." comment. A description is prose:
# "sprint #12 review" has to survive intact.
COMMENTED_KEYS = ("output", "backend", "model")

BUILTIN_DIR = Path(__file__).parent / "styles"


@dataclass(frozen=True)
class Style:
    name: str
    description: str
    output: str  # note | plain
    backend: str | None  # None = the backend setting (or an explicit pick)
    model: str | None
    body: str  # the instruction text
    source: str  # "builtin" | "mine" | the extra folder's path
    path: Path

    def as_dict(self) -> dict:
        """The wire shape the page reads (GET /api/styles)."""
        return {
            "name": self.name,
            "description": self.description,
            "output": self.output,
            "backend": self.backend,
            "model": self.model,
            "body": self.body,
            "source": self.source,
            "path": str(self.path),
        }


def mine_dir() -> Path:
    """Where new and edited styles are written: ``<config dir>/styles/``."""
    return config.config_dir() / "styles"


# --- parsing -----------------------------------------------------------------


def _strip_comment(value: str) -> str:
    """``plain   # note | plain`` -> ``plain``: whitespace then '#' starts a comment."""
    return re.split(r"\s+#", value.strip(), maxsplit=1)[0].strip()


def _split_front_matter(text: str) -> tuple[str, str]:
    """(front matter, body). No leading ``---`` (or no closing one) = all body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return "", text


def _parse_front_matter(block: str) -> dict[str, str]:
    """``key: value`` lines. Blank and ``#`` lines and unknown keys are ignored."""
    fields: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in FRONT_MATTER_KEYS:
            fields[key] = _strip_comment(value) if key in COMMENTED_KEYS else value.strip()
    return fields


def parse(name: str, text: str, *, source: str = "mine", path: Path | None = None) -> Style:
    """One style file -> a Style. ValueError says what is wrong, for ``problems``."""
    if not NAME_RE.fullmatch(name):
        raise ValueError(
            f"bad style name {name!r} — lowercase letters, digits, '-' and '_', up to 40 characters"
        )
    if name in RESERVED_NAMES:
        raise ValueError(f"{name!r} is a reserved name — the record panel uses it for 'no LLM'")
    front, body = _split_front_matter(text)
    fields = _parse_front_matter(front)

    output = fields.get("output") or "note"
    if output not in OUTPUTS:
        raise ValueError(f"bad output: {output!r} (expected {' or '.join(OUTPUTS)})")

    backend = fields.get("backend") or None
    choices = config.setting("backend").choices
    if backend is not None and backend not in choices:
        raise ValueError(f"bad backend: {backend!r} (expected one of {', '.join(choices)})")

    if not body.strip():
        raise ValueError("no instruction text — the body below the front matter is what the model gets")

    return Style(
        name=name,
        description=fields.get("description", ""),
        output=output,
        backend=backend,
        model=fields.get("model") or None,
        body=body.strip(),
        source=source,
        path=path if path is not None else mine_dir() / f"{name}.md",
    )


# --- the registry ------------------------------------------------------------


@dataclass(frozen=True)
class Registry:
    styles: dict[str, Style]  # resolved: one Style per name, the winning source
    problems: list[str]  # one line per unreadable folder / unparseable file
    folders: tuple[tuple[str, str, Path], ...]  # (source, label, dir), in load order

    def get(self, name: str) -> Style | None:
        return self.styles.get(name)

    def names(self) -> list[str]:
        return sorted(self.styles)

    def groups(self) -> list[dict]:
        """The dropdown's groups: Mine, each extra folder, Built-in. Empty ones are dropped."""
        by_source = {source: [] for source, _label, _dir in self.folders}
        for style in sorted(self.styles.values(), key=lambda s: s.name):
            by_source.setdefault(style.source, []).append(style)
        out = []
        for source, label, folder in _display_order(self.folders):
            listed = by_source.get(source) or []
            if listed:
                out.append({"label": label, "source": source, "dir": str(folder),
                            "styles": [s.as_dict() for s in listed]})
        return out


def _display_order(folders: tuple[tuple[str, str, Path], ...]) -> list[tuple[str, str, Path]]:
    """Load order is built-ins first (they lose); the page shows them last."""
    builtin = [f for f in folders if f[0] == "builtin"]
    return [f for f in folders if f[0] == "mine"] + \
           [f for f in folders if f[0] not in ("mine", "builtin")] + builtin


def _resolved(folder: Path) -> str:
    """A comparable identity for a folder, whether or not it exists yet."""
    try:
        return str(folder.resolve())
    except OSError:  # an unreachable parent: the literal path is identity enough
        return str(folder)


def _folders() -> list[tuple[str, str, Path]]:
    """(source, label, dir) in *precedence* order — a later folder overrides an earlier one.

    A folder named twice (or one that is already Mine) is listed once, at its first
    position: two identical groups in the dropdown would be nothing but confusing.
    """
    out = [("builtin", "Built-in", BUILTIN_DIR), ("mine", "Mine", mine_dir())]
    seen = {_resolved(folder) for _source, _label, folder in out}
    for folder in config.styles_dirs():
        key = _resolved(folder)
        if key in seen:
            continue
        seen.add(key)
        out.append((str(folder), folder.name or str(folder), folder))
    return out


def _list_md(folder: Path) -> tuple[list[Path], str | None]:
    """(*.md files, problem). A folder nobody created yet is not a problem; one that
    will not open is — pathlib's glob swallows that, so iterdir() does the walking.

    os.path.exists, not Path.exists: the latter re-raises anything but ENOENT/ENOTDIR
    (a styles folder under an unreadable parent would have taken the daemon down).
    """
    if not os.path.exists(folder):
        return [], None
    try:
        return sorted(p for p in folder.iterdir() if p.suffix == ".md" and p.is_file()), None
    except OSError as exc:
        return [], f"{folder}: cannot read this styles folder ({exc})"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


_cache: tuple[tuple, Registry] | None = None


def _build(listing: list[tuple[str, str, Path, list[Path], str | None]]) -> Registry:
    styles: dict[str, Style] = {}
    problems: list[str] = []
    for source, _label, _folder, files, problem in listing:
        if problem:
            problems.append(problem)
        for path in files:
            try:
                styles[path.stem] = parse(path.stem, path.read_text(encoding="utf-8"),
                                          source=source, path=path)
            except (OSError, ValueError) as exc:  # a broken file is skipped, and said out loud
                problems.append(f"{path}: {exc}")
    return Registry(styles=styles, problems=problems,
                    folders=tuple((s, lbl, f) for s, lbl, f, _files, _p in listing))


def load() -> Registry:
    """The resolved registry, cached on (path, mtime) for every candidate file."""
    global _cache
    listing = []
    for source, label, folder in _folders():
        files, problem = _list_md(folder)
        listing.append((source, label, folder, files, problem))
    key = tuple((source, str(folder), tuple((str(p), _mtime(p)) for p in files), problem)
                for source, _label, folder, files, problem in listing)
    if _cache is None or _cache[0] != key:
        _cache = (key, _build(listing))
    return _cache[1]


def get(name: str | None) -> Style | None:
    return load().styles.get(name) if name else None


def names() -> list[str]:
    return load().names()


# --- writing (Mine only) ------------------------------------------------------


def write(name: str, text: str) -> Style:
    """Write ``Mine/<name>.md``. Refuses a name or a body the loader would reject."""
    style = parse(name, text, source="mine")  # validate before anything lands on disk
    folder = mine_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{name}.md"
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    _invalidate()  # two writes inside one mtime tick must not serve the first one
    return style


def delete(name: str) -> None:
    """Delete ``Mine/<name>.md``. A built-in or an extra folder's file is not ours to remove."""
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError(f"bad style name {name!r}")
    path = mine_dir() / f"{name}.md"
    if path.is_file():
        path.unlink()
        _invalidate()
        return
    if get(name) is None:
        raise FileNotFoundError(f"no such style: {name}")
    raise PermissionError(
        f"{name} is not in your styles folder ({mine_dir()}) — copy it there before editing or deleting it"
    )


def _invalidate() -> None:
    global _cache
    _cache = None
