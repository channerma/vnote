"""A note as a sequence of takes: the folder mechanics, no HTTP and no models.

A note stays in the flat 0.6.0 layout (``audio.*`` + ``transcript.txt`` at the
root) for as long as it has one take. The first Continue migrates it into::

    <note>/takes/1/{audio.*, transcript.txt, transcript.original.txt}
    <note>/takes/2/…
    <note>/transcript.txt   ← derived: the takes' transcripts joined by a blank line

The root ``transcript.txt`` stays a real file that every older reader (``--redo``,
Regenerate, :func:`pipeline.resolve_redo`) keeps working on; it is rewritten
whenever a take is added, edited or deleted. ``meta.json["takes"]`` is the log
(``{n, created, duration_s}`` each) and ``meta["audio_duration_s"]`` their sum.

Two rules run through everything here (VNOTE-002/003):

* **Nothing in this module ever unlinks or truncates audio or a transcript.** A
  delete is a *move* into ``<notes_dir>/trash/``, and a name that is already taken
  gets a ``-2``/``-3`` suffix rather than being overwritten.
* Every read-modify-write takes the same folder lock ``versions`` uses, so a
  commit never sees a half-rebuilt join and two takes never claim one number.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import wave
from pathlib import Path

from . import output, versions

_TAKE_RE = re.compile(r"\d+")
_JOIN = "\n\n"  # takes are joined by a blank line, as the wire contract fixes it
# Trashed takes live beside trashed notes, never inside one: a session name cannot contain
# a dot (see server._SESSION_RE), so "<name>.takes" can never collide with a trashed note.
_TAKES_TRASH_SUFFIX = ".takes"
_AUDIO_SUFFIXES = (".wav", ".webm", ".ogg", ".mp4", ".m4a", ".flac", ".mp3")


# --- where things live -------------------------------------------------------


def takes_dir(session_dir: Path) -> Path:
    return Path(session_dir) / "takes"


def take_dir(session_dir: Path, n: int) -> Path:
    return takes_dir(session_dir) / str(n)


def is_multi(session_dir: Path) -> bool:
    """Has this note been migrated to ``takes/``? A note that has never goes back."""
    return takes_dir(session_dir).is_dir()


def numbers(session_dir: Path) -> list[int]:
    """The take numbers on disk, ascending; ``[]`` for a flat note."""
    try:
        names = [p.name for p in takes_dir(session_dir).iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(int(name) for name in names if _TAKE_RE.fullmatch(name))


def audio_file(directory: Path) -> Path | None:
    """The first ``audio.*`` in a folder (a note root or a take), or None."""
    for p in sorted(Path(directory).glob("audio.*")):
        if p.is_file() and p.suffix.lower() in _AUDIO_SUFFIXES:
            return p
    return None


def take_audio(session_dir: Path, n: int) -> Path | None:
    """The audio of take ``n`` — the root file when the note is still flat."""
    if not is_multi(session_dir):
        return audio_file(session_dir) if n == 1 else None
    return audio_file(take_dir(session_dir, n))


def first_take_audio(session_dir: Path) -> Path | None:
    """The audio the note-level ``/audio`` route falls back to: the earliest take's."""
    for n in numbers(session_dir):
        found = audio_file(take_dir(session_dir, n))
        if found is not None:
            return found
    return None


def trash_dir() -> Path:
    """``<notes_dir>/trash`` — resolved on every call; tests rebind ``output.NOTES_DIR``."""
    return Path(output.NOTES_DIR) / "trash"


def wav_duration(path: Path) -> float | None:
    """Seconds of a WAV file, or None for anything we cannot read cheaply.

    Only WAVs are measured here: decoding a webm would mean pulling in ffmpeg, and
    the transcriber already reports a duration for those.
    """
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return round(w.getnframes() / rate, 2) if rate else None
    except (OSError, wave.Error):
        return None


# --- the migration -----------------------------------------------------------


def _meta_duration(meta: dict) -> float | None:
    """The duration a flat note's meta.json already holds, under any of its names."""
    for key in ("audio_duration_s", "recording_duration_s", "seconds"):
        value = meta.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _migrated(session_dir: Path) -> bool:
    """Is the migration *finished*? ``meta["takes"]`` is the marker, written last.

    The ``takes/`` folder is not the marker: a kill (or a full disk) between the
    mkdir and the transcript copy leaves the folder there with nothing — or not
    everything — in it, and treating that as "done" would let the next Continue
    rebuild the root transcript from a take that has no text yet. That root file is
    the only full copy, so the check has to be the step that comes last.
    """
    return bool(_meta_takes(versions.read_meta(session_dir)))


def ensure_takes(session_dir: Path) -> None:
    """Move a flat note into ``takes/1``. A no-op once the migration has finished.

    The order is fixed by the wire contract so that a crash anywhere in it leaves
    every reader working: the root ``transcript.txt`` is *copied* (not moved) and
    stays where it is — for one take the join is identical to it — so the only
    window is the audio rename, and ``_audio_file`` falls back to ``takes/1``.

    Every step is conditional on its own target, so an interrupted migration is
    *finished* by the next call rather than skipped: what is already in ``takes/1``
    is kept, what is still at the root is moved (or copied) in.
    """
    session_dir = Path(session_dir)
    with versions.folder_lock:
        if not session_dir.is_dir():  # trashed while a recording was still being processed
            raise FileNotFoundError(f"no such note folder: {session_dir}")
        if _migrated(session_dir):
            return
        first = take_dir(session_dir, 1)
        first.mkdir(parents=True, exist_ok=True)
        audio = audio_file(session_dir)
        if audio is not None and not (first / audio.name).exists():
            os.replace(audio, first / audio.name)
        original = session_dir / "transcript.original.txt"
        if original.is_file() and not (first / "transcript.original.txt").exists():
            os.replace(original, first / "transcript.original.txt")
        transcript = session_dir / "transcript.txt"
        if transcript.is_file() and not (first / "transcript.txt").exists():
            # tmp + os.replace, not copy2: a copy straight to the final name can be
            # interrupted half-written, and a partial take-1 transcript is exactly what
            # would then be joined back over the root file.
            _copy_atomic(transcript, first / "transcript.txt")
        meta = versions.read_meta_strict(session_dir)
        meta["takes"] = [{
            "n": 1,
            "created": meta.get("created"),
            "duration_s": _meta_duration(meta),
        }]
        meta["takes_max"] = 1
        versions.write_meta(session_dir, meta)  # last: this is what says the migration finished


# --- reading -----------------------------------------------------------------


def _meta_takes(meta: dict) -> list[dict]:
    entries = meta.get("takes")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def list_takes(session_dir: Path) -> list[dict]:
    """``[{n, created, duration_s, path}]``, ascending.

    A flat note reports one synthesized take built from its root files, so callers
    never have to care which layout they are looking at.
    """
    session_dir = Path(session_dir)
    meta = versions.read_meta(session_dir)
    if not is_multi(session_dir):
        return [{
            "n": 1,
            "created": meta.get("created"),
            "duration_s": _meta_duration(meta),
            "path": str(session_dir),
        }]
    logged = {e.get("n"): e for e in _meta_takes(meta)}
    out = []
    for n in numbers(session_dir):  # the folders are the truth; meta only annotates them
        entry = logged.get(n) or {}
        out.append({
            "n": n,
            "created": entry.get("created"),
            "duration_s": entry.get("duration_s"),
            "path": str(take_dir(session_dir, n)),
        })
    return out


def take_transcript(session_dir: Path, n: int) -> str:
    """Take ``n``'s transcript — the root one for take 1 of a flat note; '' if missing."""
    session_dir = Path(session_dir)
    if not is_multi(session_dir):
        if n != 1:
            raise FileNotFoundError(f"no take {n}")
        path = session_dir / "transcript.txt"
    else:
        if n not in numbers(session_dir):
            raise FileNotFoundError(f"no take {n}")
        path = take_dir(session_dir, n) / "transcript.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def joined_transcript(session_dir: Path, wanted: list[int] | None = None, *, strict: bool = False) -> str:
    """The transcripts of ``wanted`` (default: every take) joined by a blank line.

    ``strict`` makes a take folder whose ``transcript.txt`` is missing an error rather
    than an empty contribution — what :func:`rebuild_join` needs, because the join it
    writes replaces the note's root transcript.
    """
    session_dir = Path(session_dir)
    ns = wanted if wanted is not None else [t["n"] for t in list_takes(session_dir)]
    parts = []
    for n in ns:
        if strict and is_multi(session_dir):
            # read it directly: a take that is on disk but has no transcript means the
            # folder is mid-write, and joining "" over the root file would lose text
            parts.append((take_dir(session_dir, n) / "transcript.txt").read_text(encoding="utf-8").strip())
        else:
            parts.append(take_transcript(session_dir, n))
    return _JOIN.join(text for text in parts if text)


# --- writing -----------------------------------------------------------------


def _copy_atomic(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` through a temp file — ``dest`` never exists half-written."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.stem}-", suffix=dest.suffix)
    try:
        with open(src, "rb") as fsrc, os.fdopen(fd, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
            fdst.flush()
            os.fsync(fdst.fileno())
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_atomic(path: Path, text: str) -> None:
    """Same tmp + os.replace as versions.write_meta: no reader ever sees half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def rebuild_join(session_dir: Path) -> str:
    """Rewrite the root ``transcript.txt`` from the takes on disk; returns what it wrote.

    A flat note has nothing to derive — its root transcript *is* the take's.
    """
    session_dir = Path(session_dir)
    with versions.folder_lock:
        if not is_multi(session_dir):
            return take_transcript(session_dir, 1)
        text = joined_transcript(session_dir, strict=True)
        _write_atomic(session_dir / "transcript.txt", text + "\n" if text else "")
        return text


def _next_n(session_dir: Path, meta: dict) -> int:
    """One past the highest take number this note has *ever* used.

    Numbers are never reused: a version entry says ``take: 2`` and must stay true
    even after take 2 has been deleted, so the high-water mark lives in meta.json
    (``takes_max``) rather than being derived from the folders that are left.
    """
    seen = list(numbers(session_dir))
    seen += [e["n"] for e in _meta_takes(meta) if isinstance(e.get("n"), int)]
    high = meta.get("takes_max")
    if isinstance(high, int):
        seen.append(high)
    return max(seen, default=0) + 1


def _sum_durations(entries: list[dict]) -> float | None:
    known = [e["duration_s"] for e in entries if isinstance(e.get("duration_s"), (int, float))]
    return round(sum(known), 2) if known else None


def add_take(session_dir: Path, audio_src: Path, transcript: str, created: str,
             duration_s: float | None) -> int:
    """Move ``audio_src`` in as the next take, with its transcript; returns the take number.

    The files land before meta.json and the join are touched: a crash in between
    leaves an unlisted take folder on disk (recoverable by hand) rather than a meta
    entry pointing at audio that was never written. The transcript is written *before*
    the audio moves, so a failure here leaves ``audio_src`` where the caller left it —
    the caller is the only one who can still keep that recording.
    """
    session_dir = Path(session_dir)
    audio_src = Path(audio_src)
    with versions.folder_lock:
        if not session_dir.is_dir():  # trashed while this recording was being transcribed
            raise FileNotFoundError(f"no such note folder: {session_dir}")
        ensure_takes(session_dir)
        meta = versions.read_meta_strict(session_dir)
        n = _next_n(session_dir, meta)
        target = take_dir(session_dir, n)
        target.mkdir(parents=True)
        _write_atomic(target / "transcript.txt", transcript.strip() + "\n")
        suffix = audio_src.suffix.lower() or ".wav"
        shutil.move(str(audio_src), target / f"audio{suffix}")  # a move: never two copies to diverge

        entries = [e for e in _meta_takes(meta) if e.get("n") != n]
        entries.append({"n": n, "created": created, "duration_s": duration_s})
        entries.sort(key=lambda e: e.get("n") or 0)
        meta["takes"] = entries
        meta["takes_max"] = n
        meta["audio_duration_s"] = _sum_durations(entries)
        versions.write_meta(session_dir, meta)
        rebuild_join(session_dir)
        return n


def write_take_transcript(session_dir: Path, n: int, text: str) -> None:
    """Save an edited transcript for take ``n``; Whisper's own words are kept once.

    On a flat note this is exactly the note-level transcript edit — same files,
    same kept original (:func:`versions.write_transcript`).
    """
    session_dir = Path(session_dir)
    with versions.folder_lock:
        if not is_multi(session_dir):
            if n != 1:
                raise FileNotFoundError(f"no take {n}")
            versions.write_transcript(session_dir, text)
            return
        if n not in numbers(session_dir):
            raise FileNotFoundError(f"no take {n}")
        versions.write_transcript(take_dir(session_dir, n), text)
        rebuild_join(session_dir)


# --- deletes (moves into trash/) ---------------------------------------------


def _rename_into_trash(src: Path, dest: Path) -> None:
    """Move a folder into the trash with ``os.rename`` and nothing else.

    Not ``shutil.move``: that falls back to copytree + rmtree on *any* rename error,
    and on a Windows drive mounted into WSL a rename fails while a reader still has a
    take's audio open (the audio route streams it). The rmtree half of the fallback
    then stops at the locked file and leaves the take half-deleted, with its meta and
    join never updated. A rename either moves the whole folder or moves nothing, and
    the trash is under the notes dir by construction, so there is no filesystem to
    cross. The OSError reaches the caller, which reports it with nothing moved.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)


def _free_path(dest: Path) -> Path:
    """``dest``, or ``dest-2``/``dest-3``… — a trashed folder never overwrites another."""
    if not dest.exists():
        return dest
    for suffix in range(2, 1000):
        candidate = dest.with_name(f"{dest.name}-{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"cannot find a free name for {dest}")


def delete_take(session_dir: Path, n: int) -> Path:
    """Move take ``n`` into ``<notes_dir>/trash/<note>/take-<n>/``; returns where it went.

    ``note.md`` is untouched (the Body regenerates or edits it) and the numbers of
    the remaining takes keep their gaps — history says ``take: 2`` and must stay
    true. ValueError when this is the note's last take: a note without a recording
    is what Delete note is for.
    """
    session_dir = Path(session_dir)
    with versions.folder_lock:
        if not is_multi(session_dir):
            raise ValueError("this is the note's only take")
        present = numbers(session_dir)
        if n not in present:
            raise FileNotFoundError(f"no take {n}")
        if len(present) == 1:
            raise ValueError("this is the note's only take")
        # a namespace of its own: trash/<name>/ is where a whole trashed *note* goes, and
        # a take must never land inside one that happens to share the name
        dest = _free_path(trash_dir() / f"{session_dir.name}{_TAKES_TRASH_SUFFIX}" / f"take-{n}")
        _rename_into_trash(take_dir(session_dir, n), dest)

        meta = versions.read_meta_strict(session_dir)
        entries = [e for e in _meta_takes(meta) if e.get("n") != n]
        meta["takes"] = entries
        meta["audio_duration_s"] = _sum_durations(entries)
        versions.write_meta(session_dir, meta)
        rebuild_join(session_dir)
        return dest


def trash_note(session_dir: Path) -> Path:
    """Move the whole note folder into ``<notes_dir>/trash/<name>/``; returns where it went.

    Nothing is deleted: trash is never emptied by the daemon, and restoring a note
    is a folder move back.
    """
    session_dir = Path(session_dir)
    with versions.folder_lock:
        dest = _free_path(trash_dir() / session_dir.name)
        _rename_into_trash(session_dir, dest)
        return dest


def trash_entries() -> int:
    """How many trashed things are in the trash: notes, plus takes inside a ``.takes`` folder.

    0 when the trash does not exist yet (nothing has ever been deleted).
    """
    total = 0
    try:
        for entry in trash_dir().iterdir():
            if entry.is_dir() and entry.name.endswith(_TAKES_TRASH_SUFFIX):
                total += sum(1 for _ in entry.iterdir())  # one per trashed take, not one per note
            else:
                total += 1
    except OSError:
        return 0
    return total
