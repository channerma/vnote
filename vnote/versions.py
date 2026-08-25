"""Linear version history for a note's ``note.md``.

Every version of the processed note — including the current one — lives in
``versions/note-<n>.md`` (n from 1); ``note.md`` is always a copy of the newest
version's text, so every reader that predates this module keeps working.
``meta.json["versions"]`` is the log: one entry per version with the operation
that produced it (``clean`` · ``regenerate`` · ``revise`` · ``edit`` ·
``restore``) and the mode/backend/model/instructions it used.

Folders written before versioning existed are migrated on first touch
(:func:`ensure_history`): their ``note.md`` becomes v1 with op ``clean``.

The folder's lock lives here too, so the other read-modify-write files of a note
(``meta.json``, ``transcript.txt``) are written under the same one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path

OPS = ("clean", "regenerate", "revise", "edit", "restore", "continue", "merge")

# The daemon is multithreaded; commits are read-modify-write. Re-entrant because
# commit() calls ensure_history(), which takes the same lock on its own.
_commit_lock = threading.RLock()

# The same lock under its public name: takes.py does its own read-modify-write on
# a note folder (meta.json, the derived transcript.txt, the takes/ tree) and must
# serialize against every commit here, not against a second lock of its own.
folder_lock = _commit_lock


def read_meta(session_dir: Path) -> dict:
    """``meta.json`` as a dict — ``{}`` when it is missing or unreadable."""
    try:
        data = json.loads((Path(session_dir) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_meta_strict(session_dir: Path) -> dict:
    """``meta.json`` as a dict — ``{}`` only when the file is *missing*.

    The read-modify-write paths (:func:`commit`, :func:`ensure_history`) must never
    mistake an unreadable meta.json for an empty one: doing so would rewrite it from
    scratch and drop the version log along with every other field.
    """
    path = Path(session_dir) / "meta.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("meta.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("meta.json is not valid JSON")
    return data


def write_meta(session_dir: Path, meta: dict) -> None:
    """Write ``meta.json`` in the same shape output.write_session uses — atomically.

    A truncate-then-write would let a concurrent reader see an empty/half-written
    file and conclude the note has no history, so the new bytes land in a temp file
    in the same directory and are moved into place with :func:`os.replace`.
    """
    session_dir = Path(session_dir)
    body = json.dumps(meta, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=session_dir, prefix=".meta-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, session_dir / "meta.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _keep_original(current: Path, original: Path) -> None:
    """Copy ``current`` to ``original`` atomically — a half-written keep is worse than none.

    A truncated ``transcript.original.txt`` would still *exist*, so every later edit
    would skip the copy and Whisper's words would survive only as that truncation.
    The bytes land in a temp file and are flushed to disk before the name is taken.
    """
    try:
        src = current.open("rb")
    except FileNotFoundError:
        return  # a folder with no transcript.txt has nothing to keep
    with src:
        fd, tmp = tempfile.mkstemp(dir=original.parent, prefix=".original-", suffix=".txt")
        try:
            with os.fdopen(fd, "wb") as fh:
                shutil.copyfileobj(src, fh)  # byte-for-byte; encodings are not our business here
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, original)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def write_transcript(session_dir: Path, text: str) -> None:
    """Replace ``transcript.txt`` with the user's edit, keeping Whisper's output once.

    ``transcript.original.txt`` is written the first time a transcript is edited and
    never again: the first edit is the only moment Whisper's own words still exist.
    Same lock as :func:`commit` — the transcript is what a regenerate reads, so a
    commit must not see it half-replaced — and the same atomic tmp + ``os.replace``
    as :func:`write_meta`. Nothing here ever unlinks or truncates either file.
    """
    session_dir = Path(session_dir)
    current = session_dir / "transcript.txt"
    original = session_dir / "transcript.original.txt"
    with _commit_lock:
        if not original.exists():
            _keep_original(current, original)
        fd, tmp = tempfile.mkstemp(dir=session_dir, prefix=".transcript-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, current)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def entries(session_dir: Path) -> list[dict]:
    """The version log, newest last; ``[]`` when the note has no history."""
    versions = read_meta(session_dir).get("versions")
    return versions if isinstance(versions, list) else []


def heading_title(text: str) -> str | None:
    """``"Title"`` when ``text`` starts with a ``# Title`` line, else None."""
    first = text.lstrip().split("\n", 1)[0].strip()
    m = re.match(r"#[ \t]+(.+)", first)  # same shape cleanup._split_heading accepts, tabs included
    return m.group(1).strip() or None if m else None


def _now_iso(when: datetime | None) -> str:
    return (when or datetime.now()).isoformat(timespec="seconds")


def _version_path(session_dir: Path, n: int) -> Path:
    return Path(session_dir) / "versions" / f"note-{n}.md"


def normalized(text: str) -> str:
    """``text`` with LF line endings and exactly one trailing newline."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _existing_version_files(session_dir: Path) -> list[int]:
    """The ``n`` of every ``versions/note-<n>.md`` on disk (unsorted)."""
    try:
        names = [p.name for p in (Path(session_dir) / "versions").iterdir()]
    except OSError:
        return []
    return [int(m.group(1)) for name in names if (m := re.fullmatch(r"note-(\d+)\.md", name))]


def ensure_history(session_dir: Path) -> None:
    """Migrate a pre-versioning folder: an existing note.md becomes v1 (``clean``).

    A no-op when the note already has a history, has no note.md at all, or already
    has ``versions/note-*.md`` files (a half-written history is never overwritten).
    Takes the commit lock — the same re-entrant one :func:`commit` holds.

    Raises ValueError when meta.json exists but cannot be parsed; OSError on I/O.
    """
    session_dir = Path(session_dir)
    with _commit_lock:
        meta = read_meta_strict(session_dir)
        if isinstance(meta.get("versions"), list) or not (session_dir / "note.md").is_file():
            return
        if _existing_version_files(session_dir):
            return  # version files without a log: keep the files, let commit number past them
        _migrate(session_dir, meta)


def _migrate(session_dir: Path, meta: dict) -> None:
    text = normalized((session_dir / "note.md").read_text(encoding="utf-8"))
    path = _version_path(session_dir, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    meta["versions"] = [{
        "n": 1,
        "created": meta.get("created") or _now_iso(None),
        "op": "clean",
        "mode": meta.get("cleanup_mode"),
        "backend": meta.get("cleanup_backend"),
        "model": meta.get("cleanup_model"),
        "instructions": None,
        "restored_from": None,
    }]
    write_meta(session_dir, meta)


# The fields commit() writes itself; ``extra`` may add to an entry, never rewrite these.
_ENTRY_FIELDS = frozenset(
    {"n", "created", "op", "mode", "backend", "model", "instructions", "restored_from"}
)


def commit(
    session_dir: Path,
    text: str,
    *,
    op: str,
    title: str | None = None,
    mode: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    instructions: str | None = None,
    restored_from: int | None = None,
    when: datetime | None = None,
    extra: dict | None = None,
) -> tuple[int, dict]:
    """Write ``text`` as the next version (and as note.md); return (n, the new meta).

    ``op`` is one of :data:`OPS`. A ``clean``/``regenerate`` also refreshes the
    ``cleanup_*`` fields in meta.json (and ``regenerate`` sets ``recleaned``, the
    0.5.0 field the page still reads).

    ``extra`` adds fields to the log entry — Phase 10 F records ``take``/``how`` on
    a continue and ``takes`` on a regenerate from a subset. It cannot overwrite the
    fields above: a version entry's own shape is this function's to decide.
    """
    session_dir = Path(session_dir)
    text = normalized(text)
    with _commit_lock:
        ensure_history(session_dir)
        meta = read_meta_strict(session_dir)
        log = meta.get("versions")
        if not isinstance(log, list):
            log = []
        # Never reuse an n: a crash between the file write and the meta write leaves a
        # version file the log does not know about, and overwriting it would lose text.
        n = max(len(log), *_existing_version_files(session_dir), 0) + 1

        path = _version_path(session_dir, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        (session_dir / "note.md").write_text(text, encoding="utf-8")

        log.append({
            "n": n,
            "created": _now_iso(when),
            "op": op,
            "mode": mode,
            "backend": backend,
            "model": model,
            "instructions": instructions,
            "restored_from": restored_from,
            **{k: v for k, v in (extra or {}).items() if k not in _ENTRY_FIELDS},
        })
        meta["versions"] = log
        if title:
            meta["title"] = title
        if op in ("clean", "regenerate"):
            meta["cleanup_mode"] = mode
            meta["cleanup_backend"] = backend
            meta["cleanup_model"] = model
            if op == "regenerate":
                meta["recleaned"] = True
        write_meta(session_dir, meta)
        return n, meta


def read(session_dir: Path, n: int) -> str:
    """The text of version ``n``; ValueError when it does not exist."""
    if n < 1:
        raise ValueError(f"no version {n}")
    try:
        return _version_path(session_dir, n).read_text(encoding="utf-8")
    except OSError:
        raise ValueError(f"no version {n}") from None
