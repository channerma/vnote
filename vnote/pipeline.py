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

from . import config, output, styles, takes, versions

HOWS = ("continue", "append", "merge")  # what a new take does to the note it lands in


class EmptyTranscriptError(ValueError):
    """Raised when transcription produced no text (no speech detected?)."""


class TranscriptionError(RuntimeError):
    """The transcription backend failed; the original exception is chained."""


class TakeCleanupError(RuntimeError):
    """A cleanup failed *after* the take was written; the recording is safe on disk.

    The take (audio + transcript) is already in the note folder and the note itself
    is untouched — the caller reports the failure with ``take`` so the page can offer
    a re-run instead of implying the recording was lost.
    """

    def __init__(self, message: str, *, take: int, audio_path: Path | None) -> None:
        super().__init__(message)
        self.take = take
        self.audio_path = audio_path


def resolved_backend(mode: str | None, backend: str | None) -> str:
    """Which backend a cleanup will actually use: an explicit pick > the style's > the setting."""
    if backend:
        return backend
    style = styles.get(mode)
    return (style.backend if style else None) or config.backend()


def resolved_model(backend: str, model: str | None, mode: str | None = None) -> str:
    """The model name to record in meta.json for a finished cleanup.

    Same precedence as the backend: what the caller asked for, else the style's
    ``model:`` line, else whatever that backend falls back to.
    """
    if model:
        return model
    style = styles.get(mode)
    if style and style.model:
        return style.model
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


def wants_heading(mode: str | None) -> bool:
    """Does this style's output carry a '# Title' heading? A style that is gone: yes."""
    style = styles.get(mode)
    return style is None or style.output == "note"


def note_markdown(title: str, body: str, mode: str, *, heading: bool | None = None) -> str:
    """The note as it goes to the clipboard / note.md: titled Markdown, or the body alone
    when the style's output is ``plain``. ``mode`` holds a style name.

    ``heading`` overrides the style's answer, for a caller that knows better than a
    style that is no longer there (see :func:`revise`).
    """
    body = body.strip() + "\n"
    if heading is None:
        heading = wants_heading(mode)
    return f"# {title}\n\n{body}" if heading else body


def _fallback_title(transcript: str) -> str:
    words = transcript.split()
    return " ".join(words[:6]) if words else "voice note"


def make_note(
    audio_path: Path,
    *,
    transcribe_fn,
    clean_fn,
    mode: str = config.DEFAULT_STYLE,
    backend: str | None = None,
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
    variant: dict | None = None  # {title, body, temperature} of the second, varied pass
    if raw:
        title = _fallback_title(transcript)
    else:
        # The style may name its own backend; an explicit pick still wins, and what
        # actually ran is what meta.json records.
        backend = resolved_backend(mode, backend)
        on_stage("cleaning", backend=backend, mode=mode)
        t0 = time.monotonic()
        try:
            if config.double_clean():
                # Double-clean runs both passes in-process so the temperature actually
                # reaches the model (a warm daemon's cleaner has no temperature knob):
                # a deterministic baseline at 0.0, then a varied pass. Uses the same
                # backend/model/style chain as the single pass would.
                from .cleanup import clean as _inproc_clean

                result = _inproc_clean(transcript, mode=mode, backend=backend, model=model, temperature=0.0)
            else:
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
            cleanup_model = resolved_model(backend, model, mode)
            if config.double_clean():
                _variant_temp = config.variant_temperature()
                try:
                    from .cleanup import clean as _inproc_clean

                    vres = _inproc_clean(transcript, mode=mode, backend=backend, model=model,
                                         temperature=_variant_temp)
                except Exception as exc:  # noqa: BLE001 - the baseline note still stands
                    on_stage("variant_failed", error=str(exc))
                else:
                    variant = {"title": vres.title, "body": vres.body, "temperature": _variant_temp}

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
        **({"cleanup_variant_temperature": variant["temperature"]} if variant else {}),
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
        heading=wants_heading(mode),
    )

    note_text = transcript if note_body is None else note_markdown(title, note_body, mode)
    if note_body is not None:  # raw notes (and failed cleanups) have no note.md, so no history
        _, meta = versions.commit(
            session_dir, note_text, op="clean", title=title,
            mode=mode, backend=backend, model=cleanup_model, when=started,
            instructions=instructions,
        )
        if variant is not None:
            # The user's two named comparison files, alongside the app's canonical
            # note.md (which the web UI / versions / takes keep reading).
            #   <folder>_note.md            = the deterministic (temp-0) baseline
            #   <folder>_note_variant_tN.md = the varied pass; N encodes the temperature
            temp_frac = str(variant["temperature"]).split(".")[-1] or "0"
            (session_dir / f"{session_dir.name}_note.md").write_text(note_text, encoding="utf-8")
            (session_dir / f"{session_dir.name}_note_variant_t{temp_frac}.md").write_text(
                note_markdown(variant["title"], variant["body"], mode), encoding="utf-8"
            )
        elif config.double_clean():
            # The varied pass failed — the deterministic baseline alone still gets its
            # named file, so the pair layout stays predictable.
            (session_dir / f"{session_dir.name}_note.md").write_text(note_text, encoding="utf-8")
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
    backend: str | None = None,
    model: str | None = None,
    instructions: str | None = None,
    only_takes: list[int] | None = None,
) -> RecleanResult:
    """Re-run cleanup on a saved note (or a bare transcript file); no transcription.

    The transcript is the input, so this is a *regenerate*: the result becomes a new
    version of the note (``op: "regenerate"``). ``instructions`` is free text appended
    to the cleanup prompt ("make it longer").

    ``only_takes`` regenerates from a subset of the note's takes — their transcripts
    joined, in the order given — and records which ones on the version entry. The
    takes themselves are untouched: the raw record always keeps every take.

    Missing paths raise FileNotFoundError, an empty transcript raises
    EmptyTranscriptError, and cleanup failures propagate to the caller.
    """
    if only_takes is not None:
        session_dir = Path(path).expanduser()
        if not session_dir.is_dir():
            raise FileNotFoundError(f"no such note folder: {session_dir}")
        transcript = takes.joined_transcript(session_dir, only_takes)  # FileNotFoundError: no such take
    else:
        transcript, session_dir = resolve_redo(path)
    if not transcript:
        raise EmptyTranscriptError("transcript is empty")

    backend = resolved_backend(mode, backend)  # the style's backend unless the caller picked one
    result = clean_fn(transcript, mode=mode, backend=backend, model=model, instructions=instructions)
    note_text = note_markdown(result.title, result.body, mode)
    version: int | None = None
    if session_dir is not None:
        version, _ = versions.commit(
            session_dir, note_text, op="regenerate", title=result.title, mode=mode,
            backend=backend, model=resolved_model(backend, model, mode), instructions=instructions,
            extra={"takes": list(only_takes)} if only_takes is not None else None,
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
    backend: str | None = None,
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
    backend = backend or config.backend()  # revise is style-agnostic: no style backend to consult
    current = note_path.read_text(encoding="utf-8")
    result = revise_fn(current, instructions, backend=backend, model=model)
    mode = meta.get("cleanup_mode") or "edit"
    # A style that has since been deleted cannot say whether this note carries a heading.
    # The note itself can — revising must not grow a "# Title" the note never had.
    heading = None if styles.get(mode) else versions.heading_title(current) is not None
    note_text = versions.normalized(note_markdown(result.title, result.body, mode=mode, heading=heading))
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


# --- continue a note with another take ---------------------------------------


def _appended(current: str, body: str) -> str:
    """The note with ``body`` added under a bare ``---`` — the shape both appends share.

    A note whose last line is already a rule (the take before this one ended there, or
    the author typed one) gets a blank line instead: ``---`` twice over reads as an
    empty section, and every further take would add another.
    """
    text = versions.normalized(current)
    rule = "\n" if text.rstrip("\n").rsplit("\n", 1)[-1].strip() == "---" else "\n---\n\n"
    return text + rule + body.strip() + "\n"


def _apply_take(
    session_dir: Path,
    transcript: str,
    *,
    take: int,
    how: str,
    mode: str,
    backend: str | None,
    model: str | None,
    instructions: str | None,
    clean_fn,
    continue_fn,
    merge_fn,
) -> RecleanResult:
    """Run one take's transcript against the *current* note per ``how``; commit the result.

    Shared by :func:`continue_take` (a fresh recording) and :func:`rerun_take` (a take
    that is already on disk), so both write the same version entry. A failing model
    raises :class:`TakeCleanupError`: by this point the take exists and nothing that
    happens here may take it — or the note — with it.
    """
    session_dir = Path(session_dir)
    current = (session_dir / "note.md").read_text(encoding="utf-8")
    meta = versions.read_meta(session_dir)
    backend = resolved_backend(mode, backend)  # the style's backend unless the caller picked one
    try:
        if how == "merge":
            result = merge_fn(current, transcript, mode=mode, backend=backend, model=model,
                              instructions=instructions)
            # A style that has since been deleted cannot say whether this note carries a
            # heading; the note itself can (same rule as revise()).
            heading = None if styles.get(mode) else versions.heading_title(current) is not None
            note_text = note_markdown(result.title, result.body, mode, heading=heading)
            # Without a heading there was no TITLE line to parse: CleanResult.title is then
            # the new take's first few words, which must not become the note's title.
            titled = heading if heading is not None else wants_heading(mode)
            title = result.title if titled else (
                versions.heading_title(current) or meta.get("title") or session_dir.name)
            op = "merge"
        elif how == "append":
            note_text = _appended(current, clean_fn(transcript, mode=mode, backend=backend, model=model,
                                                    instructions=instructions).body)
            title, op = versions.heading_title(current) or meta.get("title") or session_dir.name, "continue"
        else:
            note_text = _appended(current, continue_fn(current, transcript, mode=mode, backend=backend,
                                                       model=model, instructions=instructions))
            title, op = versions.heading_title(current) or meta.get("title") or session_dir.name, "continue"
    except Exception as exc:  # noqa: BLE001 - the take is on disk; the caller reports which one
        raise TakeCleanupError(str(exc), take=take, audio_path=takes.take_audio(session_dir, take)) from exc

    version, _ = versions.commit(
        session_dir, note_text, op=op,
        title=title if how == "merge" else None,  # an append keeps the note's own title
        mode=mode, backend=backend, model=resolved_model(backend, model, mode),
        instructions=instructions, extra={"take": take, "how": how},
    )
    return _committed(session_dir, title, version, note_text)


def _take_result(session_dir: Path, take: int, result: RecleanResult | None) -> dict:
    meta = versions.read_meta(session_dir)
    return {
        "take": take,
        "version": result.version if result else None,
        "title": result.title if result else (meta.get("title") or Path(session_dir).name),
        "note": result.note_text if result else None,
        "transcript": takes.take_transcript(session_dir, take),
    }


def continue_take(
    session_dir: Path,
    *,
    audio_tmp: Path,
    transcribe_fn,
    clean_fn,
    continue_fn,
    merge_fn,
    how: str = "continue",
    mode: str = config.DEFAULT_STYLE,
    backend: str | None = None,
    model: str | None = None,
    language: str | None = None,
    raw: bool = False,
    instructions: str | None = None,
    when: datetime | None = None,
) -> dict:
    """Transcribe ``audio_tmp``, add it to ``session_dir`` as the next take, apply it.

    The audio is *moved* into the take folder, so it survives everything after that
    point: a raw note (or one that was never cleaned) simply keeps the take and
    writes no version, and a cleanup failure raises :class:`TakeCleanupError` with
    the take number rather than losing the recording.
    """
    session_dir = Path(session_dir)
    if how not in HOWS:
        raise ValueError(f"bad how: {how!r} (expected one of {', '.join(HOWS)})")
    started = when or datetime.now()
    try:
        transcript, tmeta = transcribe_fn(audio_tmp, language=language)
    except Exception as exc:  # noqa: BLE001 - same contract as make_note: a backend failure is ours
        raise TranscriptionError(str(exc)) from exc
    if not transcript:
        raise EmptyTranscriptError("transcript is empty (no speech detected?)")

    duration = tmeta.get("audio_duration_s")
    if not isinstance(duration, (int, float)):  # measured before the move: the WAV is still ours
        duration = takes.wav_duration(audio_tmp)
    take = takes.add_take(session_dir, audio_tmp, transcript,
                          started.isoformat(timespec="seconds"), duration)

    if raw or not (session_dir / "note.md").is_file():
        return _take_result(session_dir, take, None)  # a raw note grows by takes alone
    result = _apply_take(session_dir, transcript, take=take, how=how, mode=mode, backend=backend,
                         model=model, instructions=instructions, clean_fn=clean_fn,
                         continue_fn=continue_fn, merge_fn=merge_fn)
    return _take_result(session_dir, take, result)


def rerun_take(
    session_dir: Path,
    n: int,
    *,
    clean_fn,
    continue_fn,
    merge_fn,
    how: str = "continue",
    mode: str = config.DEFAULT_STYLE,
    backend: str | None = None,
    model: str | None = None,
    instructions: str | None = None,
) -> dict:
    """Apply an existing take to the *current* note again — a new version.

    There is no way back to the pre-take note here by design: restore the version
    the take produced first if that is what you want (the version list names it).
    """
    session_dir = Path(session_dir)
    if how not in HOWS:
        raise ValueError(f"bad how: {how!r} (expected one of {', '.join(HOWS)})")
    if not (session_dir / "note.md").is_file():
        raise ValueError("this note has never been cleaned; regenerate it instead")
    transcript = takes.take_transcript(session_dir, n)  # FileNotFoundError when there is no take n
    if not transcript.strip():
        raise EmptyTranscriptError("this take's transcript is empty")
    result = _apply_take(session_dir, transcript, take=n, how=how, mode=mode, backend=backend,
                         model=model, instructions=instructions, clean_fn=clean_fn,
                         continue_fn=continue_fn, merge_fn=merge_fn)
    return _take_result(session_dir, n, result)
