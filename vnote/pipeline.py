"""The record-free core: transcribe → clean → write a session folder.

This module is deliberately free of printing, clipboard, editor and temp-file
handling — those belong to the caller. Both the CLI (``vnote.cli``) and the
daemon's HTTP handlers run the *same* functions here, so a note made either way
lands in the same shape on disk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, output, versions


class EmptyTranscriptError(ValueError):
    """Raised when transcription produced no text (no speech detected?)."""


class TranscriptionError(RuntimeError):
    """The transcription backend failed; the original exception is chained."""


def resolved_model(backend: str, model: str | None) -> str:
    """The model name to record in meta.json for a finished cleanup."""
    if model:
        return model
    if backend == "ollama":
        return config.ollama_model()
    if backend == "claude-code":
        return "claude-code (session default)"  # the CLI picks; vnote doesn't pin it
    return str(config.get("claude_model"))


# --- a fresh note (transcribe + clean + write) -------------------------------


@dataclass
class NoteResult:
    session_dir: Path
    title: str
    transcript: str
    note_body: str | None  # None when raw=True or when cleanup failed
    note_text: str  # what a caller would put on the clipboard / stdout
    meta: dict
    written: dict[str, Path]
    cleanup_error: str | None  # the exception text when we fell back to the raw transcript
    transcribe_seconds: float
    cleanup_seconds: float | None


def _no_stage(event: str, **info: object) -> None:
    pass


def note_markdown(title: str, body: str, mode: str) -> str:
    """The note as it goes to the clipboard / note.md: titled Markdown, or plain text for dictation."""
    body = body.strip() + "\n"
    return body if mode == "dictation" else f"# {title}\n\n{body}"


def _fallback_title(transcript: str) -> str:
    words = transcript.split()
    return " ".join(words[:6]) if words else "voice note"


def make_note(
    audio_path: Path,
    *,
    transcribe_fn,
    clean_fn,
    mode: str = "edit",
    backend: str,
    model: str | None = None,
    language: str | None = None,
    raw: bool = False,
    source: str = "mic",
    source_path: str | None = None,
    rec_duration: float | None = None,
    started: datetime | None = None,
    on_stage=None,
    instructions: str | None = None,
) -> NoteResult:
    """Transcribe ``audio_path``, clean it up (unless ``raw``), write the session folder.

    ``transcribe_fn(audio_path, language=...) -> (text, meta)`` and
    ``clean_fn(transcript, mode=, backend=, model=) -> CleanResult`` are injected so
    the caller decides between the warm daemon and in-process models.

    Transcription errors surface as TranscriptionError; an empty transcript raises EmptyTranscriptError.
    A failing cleanup is *not* fatal — the raw transcript is kept and the exception
    text is reported in ``cleanup_error``.

    ``on_stage(event, **info)`` (optional) is called as the stages complete, so a CLI
    can print progress while the pipeline runs: ``transcribed`` (chars, seconds,
    language), ``cleaning`` (backend, mode), ``cleaned`` (seconds), ``cleanup_failed``
    (error). The pipeline itself never prints.
    """
    started = started or datetime.now()
    on_stage = on_stage or _no_stage

    t0 = time.monotonic()
    try:
        transcript, tmeta = transcribe_fn(audio_path, language=language)
    except Exception as exc:  # noqa: BLE001 - whatever the backend raised, it is a transcription failure
        raise TranscriptionError(str(exc)) from exc
    transcribe_s = round(time.monotonic() - t0, 1)
    if not transcript:
        raise EmptyTranscriptError("transcript is empty (no speech detected?)")
    on_stage("transcribed", chars=len(transcript), seconds=transcribe_s, language=tmeta.get("language"))

    note_body: str | None = None
    title: str
    cleanup_s: float | None = None
    cleanup_backend: str | None = None
    cleanup_model: str | None = None
    cleanup_error: str | None = None
    if raw:
        title = _fallback_title(transcript)
    else:
        on_stage("cleaning", backend=backend, mode=mode)
        t0 = time.monotonic()
        try:
            result = clean_fn(transcript, mode=mode, backend=backend, model=model, instructions=instructions)
        except Exception as exc:  # noqa: BLE001 - never fatal: an LLM HTTP 500 or timeout keeps the raw transcript
            cleanup_error = str(exc)
            title = _fallback_title(transcript)
            on_stage("cleanup_failed", error=cleanup_error)
        else:
            cleanup_s = round(time.monotonic() - t0, 1)
            on_stage("cleaned", seconds=cleanup_s)
            title = result.title
            note_body = result.body
            cleanup_backend = backend
            cleanup_model = resolved_model(backend, model)

    session_dir = output.make_session_dir(title, when=started)
    meta = {
        "created": started.isoformat(timespec="seconds"),
        "source": source,
        "source_path": source_path,
        "recording_duration_s": rec_duration,
        "transcribe_seconds": transcribe_s,
        "cleanup_mode": None if raw else mode,
        "cleanup_backend": cleanup_backend,
        "cleanup_model": cleanup_model,
        "cleanup_seconds": cleanup_s,
        "title": title,
        **tmeta,
    }
    if note_body is not None:
        # An empty history up front, so the commit below appends v1 instead of
        # taking the note.md write_session just made for a pre-versioning folder.
        meta["versions"] = []
    written = output.write_session(
        session_dir,
        audio_src=audio_path,
        transcript=transcript,
        note_md=note_body,
        title=title,
        meta=meta,
        heading=mode != "dictation",
    )

    note_text = transcript if note_body is None else note_markdown(title, note_body, mode)
    if note_body is not None:  # raw notes (and failed cleanups) have no note.md, so no history
        _, meta = versions.commit(
            session_dir, note_text, op="clean", title=title,
            mode=mode, backend=backend, model=cleanup_model, when=started,
            instructions=instructions,
        )
    return NoteResult(
        session_dir=session_dir,
        title=title,
        transcript=transcript,
        note_body=note_body,
        note_text=note_text,
        meta=meta,
        written=written,
        cleanup_error=cleanup_error,
        transcribe_seconds=transcribe_s,
        cleanup_seconds=cleanup_s,
    )


# --- re-clean an existing note (no transcription) ---------------------------


@dataclass
class RecleanResult:
    session_dir: Path | None  # None when the target was a bare transcript file
    title: str
    note_text: str
    transcript: str
    version: int | None = None  # the version this write created, when it went to a session folder


def resolve_redo(path: Path) -> tuple[str, Path | None]:
    """Return (transcript_text, session_dir_or_None) for a --redo target.

    ``path`` may be a session directory (uses its transcript.txt) or a transcript
    file directly. session_dir is returned only when we can write a note back.
    """
    path = path.expanduser()
    if path.is_dir():
        tx = path / "transcript.txt"
        if not tx.is_file():
            raise FileNotFoundError(f"no transcript.txt in {path}")
        return tx.read_text(encoding="utf-8").strip(), path
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        session = path.parent if path.name == "transcript.txt" and (path.parent / "meta.json").exists() else None
        return text, session
    raise FileNotFoundError(f"no such path: {path}")


def reclean(
    path: Path,
    *,
    clean_fn,
    mode: str,
    backend: str,
    model: str | None = None,
    instructions: str | None = None,
) -> RecleanResult:
    """Re-run cleanup on a saved note (or a bare transcript file); no transcription.

    The transcript is the input, so this is a *regenerate*: the result becomes a new
    version of the note (``op: "regenerate"``). ``instructions`` is free text appended
    to the cleanup prompt ("make it longer").

    Missing paths raise FileNotFoundError, an empty transcript raises
    EmptyTranscriptError, and cleanup failures propagate to the caller.
    """
    transcript, session_dir = resolve_redo(path)
    if not transcript:
        raise EmptyTranscriptError("transcript is empty")

    result = clean_fn(transcript, mode=mode, backend=backend, model=model, instructions=instructions)
    note_text = note_markdown(result.title, result.body, mode)
    version: int | None = None
    if session_dir is not None:
        version, _ = versions.commit(
            session_dir, note_text, op="regenerate", title=result.title, mode=mode,
            backend=backend, model=resolved_model(backend, model), instructions=instructions,
        )
    return RecleanResult(session_dir=session_dir, title=result.title, note_text=note_text,
                         transcript=transcript, version=version)


# --- edit / revise / restore an existing note --------------------------------


def _session_transcript(session_dir: Path) -> str:
    try:
        return (session_dir / "transcript.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _committed(session_dir: Path, title: str, version: int, text: str) -> RecleanResult:
    # ``text`` is what this commit wrote, not a re-read of note.md: a concurrent
    # commit may already have moved note.md on to a later version.
    return RecleanResult(
        session_dir=session_dir,
        title=title,
        note_text=text,
        transcript=_session_transcript(session_dir),
        version=version,
    )


def save_edit(session_dir: Path, text: str) -> RecleanResult:
    """Save hand-edited note text as a new version (``op: "edit"``)."""
    session_dir = Path(session_dir)
    if not text or not text.strip():
        raise ValueError("note text is empty")
    meta = versions.read_meta(session_dir)
    title = versions.heading_title(text) or meta.get("title") or session_dir.name
    text = versions.normalized(text)
    version, _ = versions.commit(session_dir, text, op="edit", title=title)
    return _committed(session_dir, title, version, text)


def revise(
    session_dir: Path,
    *,
    revise_fn,
    instructions: str,
    backend: str,
    model: str | None = None,
) -> RecleanResult:
    """Rewrite the *current note* per ``instructions`` — a new version (``op: "revise"``).

    Unlike :func:`reclean`, the input is the note as it stands (edits included), not
    the transcript. ``revise_fn(note_text, instructions, backend=, model=) -> CleanResult``.
    """
    session_dir = Path(session_dir)
    note_path = session_dir / "note.md"
    if not note_path.is_file():
        raise ValueError("no note to revise")
    if not instructions or not instructions.strip():
        raise ValueError("instructions are empty")
    meta = versions.read_meta(session_dir)
    result = revise_fn(note_path.read_text(encoding="utf-8"), instructions, backend=backend, model=model)
    note_text = versions.normalized(note_markdown(result.title, result.body,
                                                  mode=meta.get("cleanup_mode") or "edit"))
    version, _ = versions.commit(
        session_dir, note_text, op="revise", title=result.title, mode=meta.get("cleanup_mode"),
        backend=backend, model=resolved_model(backend, model), instructions=instructions,
    )
    return _committed(session_dir, result.title, version, note_text)


def restore(session_dir: Path, n: int) -> RecleanResult:
    """Make version ``n`` current again — itself a new version (``op: "restore"``).

    ValueError when there is no version ``n``.
    """
    session_dir = Path(session_dir)
    versions.ensure_history(session_dir)  # a pre-versions folder gets its v1 before we look for n
    text = versions.normalized(versions.read(session_dir, n))
    meta = versions.read_meta(session_dir)
    title = versions.heading_title(text) or meta.get("title") or session_dir.name
    version, _ = versions.commit(session_dir, text, op="restore", title=title, restored_from=n)
    return _committed(session_dir, title, version, text)
