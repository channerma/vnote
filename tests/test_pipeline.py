"""Tests for the printing-free core: make_note, reclean, resolved_model."""

import json

import pytest

import vnote.output as output
from vnote import config, pipeline
from vnote.cleanup import CleanResult
from vnote.pipeline import EmptyTranscriptError, make_note, reclean, resolved_model


def _audio(tmp_path, name="memo.m4a"):
    p = tmp_path / name
    p.write_bytes(b"not really audio")
    return p


def _transcriber(text, meta=None):
    def transcribe_fn(audio_path, language=None):
        return text, dict(meta or {"language": "en"})

    return transcribe_fn


def _cleaner(title="A Tidy Title", body="the cleaned body"):
    def clean_fn(transcript, mode="edit", backend="ollama", model=None):
        return CleanResult(title=title, body=body)

    return clean_fn


def test_make_note_writes_full_session(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setattr(output, "NOTES_DIR", notes)
    src = _audio(tmp_path)

    res = make_note(
        src,
        transcribe_fn=_transcriber("hello there this is a voice note"),
        clean_fn=_cleaner(),
        mode="edit",
        backend="ollama",
        source="file",
        source_path=str(src),
        rec_duration=12.5,
    )

    assert res.session_dir.parent == notes
    assert (res.session_dir / "audio.m4a").exists()
    assert (res.session_dir / "transcript.txt").read_text().strip() == "hello there this is a voice note"
    assert (res.session_dir / "note.md").read_text() == "# A Tidy Title\n\nthe cleaned body\n"
    assert res.note_text == "# A Tidy Title\n\nthe cleaned body\n"
    assert res.title == "A Tidy Title"
    assert res.cleanup_error is None
    assert set(res.written) == {"audio", "transcript", "note", "meta"}

    meta = json.loads((res.session_dir / "meta.json").read_text())
    assert meta["cleanup_mode"] == "edit"
    assert meta["cleanup_backend"] == "ollama"
    assert meta["cleanup_model"] == config.ollama_model()
    assert meta["source"] == "file"
    assert meta["source_path"] == str(src)
    assert meta["recording_duration_s"] == 12.5
    assert meta["title"] == "A Tidy Title"
    assert meta["language"] == "en"  # passed through from the transcriber's meta
    assert isinstance(meta["transcribe_seconds"], float)
    assert isinstance(meta["cleanup_seconds"], float)
    assert "created" in meta


def test_make_note_raw_skips_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)

    def _boom(*a, **k):
        raise AssertionError("clean_fn must not be called with raw=True")

    transcript = "one two three four five six seven eight"
    res = make_note(
        _audio(tmp_path, "a.wav"),
        transcribe_fn=_transcriber(transcript),
        clean_fn=_boom,
        backend="ollama",
        raw=True,
    )

    assert not (res.session_dir / "note.md").exists()
    assert res.title == "one two three four five six"
    assert res.note_body is None
    assert res.note_text == transcript
    assert res.meta["cleanup_mode"] is None
    assert res.meta["cleanup_backend"] is None
    assert res.meta["cleanup_model"] is None
    assert res.meta["cleanup_seconds"] is None


def test_make_note_keeps_transcript_when_cleanup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)

    def _fail(transcript, mode="edit", backend="ollama", model=None):
        raise RuntimeError("ollama is not running")

    transcript = "alpha bravo charlie delta echo foxtrot golf"
    res = make_note(
        _audio(tmp_path, "a.wav"),
        transcribe_fn=_transcriber(transcript),
        clean_fn=_fail,
        backend="ollama",
    )

    assert res.note_body is None
    assert res.cleanup_error == "ollama is not running"
    assert res.title == "alpha bravo charlie delta echo foxtrot"
    assert not (res.session_dir / "note.md").exists()
    assert (res.session_dir / "transcript.txt").read_text().strip() == transcript
    assert res.note_text == transcript
    assert res.meta["cleanup_backend"] is None
    assert res.meta["cleanup_model"] is None
    assert res.meta["cleanup_seconds"] is None
    assert res.meta["cleanup_mode"] == "edit"  # what was asked for, even though it failed


def test_make_note_empty_transcript_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    with pytest.raises(EmptyTranscriptError):
        make_note(
            _audio(tmp_path, "a.wav"),
            transcribe_fn=_transcriber(""),
            clean_fn=_cleaner(),
            backend="ollama",
        )
    assert list(tmp_path.glob("*/meta.json")) == []  # nothing written


def _session(tmp_path):
    d = tmp_path / "2026-06-04-0900-some-note"
    d.mkdir()
    (d / "transcript.txt").write_text("the raw transcript text\n", encoding="utf-8")
    (d / "meta.json").write_text('{"title": "Some Note", "cleanup_mode": "light"}\n', encoding="utf-8")
    return d


def test_reclean_session_dir_rewrites_note_and_meta(tmp_path):
    d = _session(tmp_path)
    res = reclean(d, clean_fn=_cleaner("Redone", "redone body"), mode="summary", backend="claude-code")

    assert res.session_dir == d
    assert res.title == "Redone"
    assert res.transcript == "the raw transcript text"
    assert res.note_text == "# Redone\n\nredone body\n"
    assert (d / "note.md").read_text() == "# Redone\n\nredone body\n"

    meta = json.loads((d / "meta.json").read_text())
    assert meta["title"] == "Some Note"  # untouched
    assert meta["cleanup_mode"] == "summary"
    assert meta["cleanup_backend"] == "claude-code"
    assert meta["cleanup_model"] == resolved_model("claude-code", None)
    assert meta["recleaned"] is True


def test_reclean_bare_transcript_writes_nothing(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("just some text", encoding="utf-8")
    res = reclean(f, clean_fn=_cleaner("Bare", "bare body"), mode="edit", backend="ollama")

    assert res.session_dir is None
    assert res.note_text == "# Bare\n\nbare body\n"
    assert list(tmp_path.iterdir()) == [f]


def test_reclean_empty_transcript_raises(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n", encoding="utf-8")
    with pytest.raises(EmptyTranscriptError):
        reclean(f, clean_fn=_cleaner(), mode="edit", backend="ollama")


def test_reclean_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        reclean(tmp_path / "nope", clean_fn=_cleaner(), mode="edit", backend="ollama")


def test_resolve_redo_is_the_shared_implementation():
    from vnote import cli

    assert cli._resolve_redo is pipeline.resolve_redo
    assert cli._resolved_model is pipeline.resolved_model



def test_dictation_note_is_plain_text_without_heading(tmp_path, monkeypatch):
    from vnote import output
    from vnote.cleanup import CleanResult

    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"RIFF")
    def clean(t, mode, backend, model):
        return CleanResult(title="hello there this is a", body="Hello there, this is a test.")

    result = make_note(
        audio, transcribe_fn=lambda p, language=None: ("hello there this is a test", {}),
        clean_fn=clean, mode="dictation", backend="ollama",
    )
    assert result.note_text == "Hello there, this is a test.\n"  # no '# title' line to paste by accident
    assert (result.session_dir / "note.md").read_text(encoding="utf-8") == "Hello there, this is a test.\n"
    assert result.meta["title"] == "hello there this is a"  # the title still lives in meta.json


def test_on_stage_events_fire_in_order(tmp_path, monkeypatch):
    from vnote import output
    from vnote.cleanup import CleanResult

    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"RIFF")
    events = []
    make_note(
        audio, transcribe_fn=lambda p, language=None: ("some words", {"language": "en"}),
        clean_fn=lambda t, mode, backend, model: CleanResult(title="T", body="B"),
        mode="edit", backend="ollama", on_stage=lambda event, **info: events.append((event, info)),
    )
    assert [e for e, _ in events] == ["transcribed", "cleaning", "cleaned"]
    assert events[0][1] == {"chars": 10, "seconds": events[0][1]["seconds"], "language": "en"}
    assert events[1][1] == {"backend": "ollama", "mode": "edit"}

    events.clear()

    def boom(t, mode, backend, model):
        raise RuntimeError("ollama down")

    make_note(audio, transcribe_fn=lambda p, language=None: ("some words", {}), clean_fn=boom,
              mode="edit", backend="ollama", on_stage=lambda event, **info: events.append((event, info)))
    assert [e for e, _ in events] == ["transcribed", "cleaning", "cleanup_failed"]
    assert events[2][1] == {"error": "ollama down"}
