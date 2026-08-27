"""Tests for the printing-free core: make_note, reclean, resolved_model."""

import json

import pytest

import vnote.output as output
from vnote import config, pipeline, versions
from vnote.cleanup import CleanResult
from vnote.pipeline import EmptyTranscriptError, make_note, reclean, resolved_model

_seen: dict = {}  # what the injected fakes were called with


def _audio(tmp_path, name="memo.m4a"):
    p = tmp_path / name
    p.write_bytes(b"not really audio")
    return p


def _transcriber(text, meta=None):
    def transcribe_fn(audio_path, language=None):
        return text, dict(meta or {"language": "en"})

    return transcribe_fn


def _cleaner(title="A Tidy Title", body="the cleaned body"):
    def clean_fn(transcript, mode="edit", backend="ollama", model=None, instructions=None):
        _seen["clean"] = {"transcript": transcript, "mode": mode, "backend": backend,
                          "model": model, "instructions": instructions}
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

    def _fail(transcript, mode="edit", backend="ollama", model=None, instructions=None):
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
    assert meta["title"] == "Redone"  # the new version's title becomes the note's title
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
    def clean(t, mode, backend, model, **kw):
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
        clean_fn=lambda t, mode, backend, model, **kw: CleanResult(title="T", body="B"),
        mode="edit", backend="ollama", on_stage=lambda event, **info: events.append((event, info)),
    )
    assert [e for e, _ in events] == ["transcribed", "cleaning", "cleaned"]
    assert events[0][1] == {"chars": 10, "seconds": events[0][1]["seconds"], "language": "en"}
    assert events[1][1] == {"backend": "ollama", "mode": "edit"}

    events.clear()

    def boom(t, mode, backend, model, **kw):
        raise RuntimeError("ollama down")

    make_note(audio, transcribe_fn=lambda p, language=None: ("some words", {}), clean_fn=boom,
              mode="edit", backend="ollama", on_stage=lambda event, **info: events.append((event, info)))
    assert [e for e, _ in events] == ["transcribed", "cleaning", "cleanup_failed"]
    assert events[2][1] == {"error": "ollama down"}


# --- version history: clean → regenerate → edit → revise → restore -----------


def test_make_note_commits_the_first_version(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    res = make_note(
        _audio(tmp_path, "a.wav"),
        transcribe_fn=_transcriber("hello there this is a voice note"),
        clean_fn=_cleaner(),
        mode="edit",
        backend="ollama",
    )

    assert (res.session_dir / "versions" / "note-1.md").read_text() == "# A Tidy Title\n\nthe cleaned body\n"
    assert (res.session_dir / "note.md").read_text() == "# A Tidy Title\n\nthe cleaned body\n"
    entries = res.meta["versions"]  # the /api/note reply carries the history
    assert len(entries) == 1
    assert entries[0]["op"] == "clean" and entries[0]["n"] == 1
    assert entries[0]["mode"] == "edit" and entries[0]["backend"] == "ollama"
    assert entries[0]["model"] == config.ollama_model()
    assert entries[0]["created"] == res.meta["created"]
    assert entries[0]["instructions"] is None and entries[0]["restored_from"] is None
    assert json.loads((res.session_dir / "meta.json").read_text())["versions"] == entries


def test_raw_and_failed_notes_have_no_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    raw = make_note(_audio(tmp_path, "a.wav"), transcribe_fn=_transcriber("one two three"),
                    clean_fn=_cleaner(), backend="ollama", raw=True)
    assert not (raw.session_dir / "versions").exists()
    assert "versions" not in raw.meta

    def _fail(transcript, mode="edit", backend="ollama", model=None, instructions=None):
        raise RuntimeError("ollama is not running")

    failed = make_note(_audio(tmp_path, "b.wav"), transcribe_fn=_transcriber("four five six"),
                       clean_fn=_fail, backend="ollama")
    assert not (failed.session_dir / "versions").exists()
    assert "versions" not in failed.meta


def test_note_history_over_a_full_edit_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    made = make_note(_audio(tmp_path, "a.wav"), transcribe_fn=_transcriber("the raw transcript text"),
                     clean_fn=_cleaner("One", "first body"), mode="edit", backend="ollama")
    d = made.session_dir

    # v2 — regenerate from the transcript, with free-text instructions
    redone = reclean(d, clean_fn=_cleaner("Two", "second body"), mode="summary",
                     backend="claude-code", instructions="make it longer")
    assert redone.version == 2
    assert _seen["clean"]["transcript"] == "the raw transcript text"
    assert _seen["clean"]["instructions"] == "make it longer"
    assert (d / "versions" / "note-2.md").read_text() == "# Two\n\nsecond body\n"
    meta = versions.read_meta(d)
    assert meta["versions"][1]["op"] == "regenerate"
    assert meta["versions"][1]["instructions"] == "make it longer"
    assert meta["cleanup_mode"] == "summary" and meta["cleanup_backend"] == "claude-code"
    assert meta["cleanup_model"] == resolved_model("claude-code", None) and meta["recleaned"] is True

    # v3 — a hand edit; the heading is the new title
    edited = pipeline.save_edit(d, "# Hand Edited\n\nI typed this myself.")
    assert edited.version == 3 and edited.title == "Hand Edited"
    assert edited.note_text == "# Hand Edited\n\nI typed this myself.\n"
    assert (d / "note.md").read_text() == "# Hand Edited\n\nI typed this myself.\n"
    assert versions.read_meta(d)["title"] == "Hand Edited"
    assert versions.entries(d)[2]["op"] == "edit"

    # v4 — revise: the fake sees the *current note*, not the transcript
    def _reviser(note_text, instructions, backend="ollama", model=None):
        _seen["revise"] = {"note": note_text, "instructions": instructions,
                           "backend": backend, "model": model}
        return CleanResult(title="Revised", body="shorter body")

    revised = pipeline.revise(d, revise_fn=_reviser, instructions="shorter", backend="ollama")
    assert revised.version == 4 and revised.title == "Revised"
    assert _seen["revise"]["note"] == "# Hand Edited\n\nI typed this myself.\n"
    assert _seen["revise"]["instructions"] == "shorter" and _seen["revise"]["backend"] == "ollama"
    entry = versions.entries(d)[3]
    assert entry["op"] == "revise" and entry["instructions"] == "shorter"
    assert entry["backend"] == "ollama" and entry["model"] == resolved_model("ollama", None)
    assert versions.read_meta(d)["cleanup_mode"] == "summary"  # a revise does not re-claim the mode

    # v5 — restore v2
    back = pipeline.restore(d, 2)
    assert back.version == 5 and back.title == "Two"
    assert (d / "note.md").read_text() == versions.read(d, 2) == "# Two\n\nsecond body\n"
    assert versions.entries(d)[4] == {
        "n": 5, "created": versions.entries(d)[4]["created"], "op": "restore", "mode": None,
        "backend": None, "model": None, "instructions": None, "restored_from": 2,
    }
    assert [e["op"] for e in versions.entries(d)] == \
        ["clean", "regenerate", "edit", "revise", "restore"]


def test_save_edit_rejects_blank_text_and_falls_back_to_the_meta_title(tmp_path):
    d = _session(tmp_path)
    (d / "note.md").write_text("# Some Note\n\nbody\n", encoding="utf-8")
    for blank in ("", "   \n\n"):
        with pytest.raises(ValueError):
            pipeline.save_edit(d, blank)
    res = pipeline.save_edit(d, "no heading here")
    assert res.title == "Some Note"  # from meta.json, since the text has no '# ' line
    assert res.transcript == "the raw transcript text"


def test_revise_needs_a_note_and_instructions(tmp_path):
    d = _session(tmp_path)

    def _reviser(note_text, instructions, backend="ollama", model=None):
        return CleanResult(title="T", body="B")

    with pytest.raises(ValueError, match="no note to revise"):
        pipeline.revise(d, revise_fn=_reviser, instructions="shorter", backend="ollama")
    (d / "note.md").write_text("# Some Note\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError):
        pipeline.revise(d, revise_fn=_reviser, instructions="  ", backend="ollama")


def test_restore_unknown_version_raises(tmp_path):
    d = _session(tmp_path)
    (d / "note.md").write_text("# Some Note\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no version 3"):
        pipeline.restore(d, 3)


def test_make_note_passes_instructions_and_records_them_in_v1(tmp_path, monkeypatch):
    from vnote import output
    from vnote.cleanup import CleanResult

    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"RIFF")
    seen = {}

    def clean(t, mode, backend, model, instructions=None, **kw):
        seen["instructions"] = instructions
        return CleanResult(title="T", body="B")

    result = make_note(audio, transcribe_fn=lambda p, language=None: ("words", {}), clean_fn=clean,
                       mode="edit", backend="ollama", instructions="bullet points only")
    assert seen["instructions"] == "bullet points only"
    assert result.meta["versions"][0]["instructions"] == "bullet points only"


def _snapshot(d):
    """Every byte the session folder holds, keyed by path relative to it."""
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_a_failing_revise_leaves_the_folder_untouched(tmp_path):
    d = _session(tmp_path)
    pipeline.save_edit(d, "# Some Note\n\nbody\n")
    before = _snapshot(d)

    def _boom(note_text, instructions, backend="ollama", model=None):
        raise RuntimeError("the backend fell over")

    with pytest.raises(RuntimeError, match="fell over"):
        pipeline.revise(d, revise_fn=_boom, instructions="shorter", backend="ollama")
    assert _snapshot(d) == before  # no new version, no rewritten note.md, no touched meta


def test_a_failing_reclean_leaves_the_folder_untouched(tmp_path):
    d = _session(tmp_path)
    pipeline.save_edit(d, "# Some Note\n\nbody\n")
    before = _snapshot(d)

    def _boom(transcript, mode="edit", backend="ollama", model=None, instructions=None):
        raise RuntimeError("the backend fell over")

    with pytest.raises(RuntimeError, match="fell over"):
        reclean(d, clean_fn=_boom, mode="summary", backend="ollama")
    assert _snapshot(d) == before


def test_committed_results_report_their_own_text(tmp_path):
    """The returned note_text is the version this call wrote, not a re-read of note.md
    (which a concurrent commit may already have moved on)."""
    d = _session(tmp_path)
    first = pipeline.save_edit(d, "# One\n\nbody one")
    second = pipeline.save_edit(d, "# Two\n\nbody two")
    assert (first.version, first.note_text) == (1, "# One\n\nbody one\n")
    assert (second.version, second.note_text) == (2, "# Two\n\nbody two\n")
    assert versions.read(d, first.version) == first.note_text

    restored = pipeline.restore(d, 1)
    assert restored.version == 3 and restored.note_text == "# One\n\nbody one\n"


def test_revise_of_a_plain_note_whose_style_is_gone_keeps_it_plain(tmp_path, monkeypatch):
    """A deleted style cannot say whether the note has a heading — the note itself can."""
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    session = tmp_path / "2026-08-25-1200-plain"
    session.mkdir()
    (session / "meta.json").write_text(
        json.dumps({"title": "Plain", "cleanup_mode": "retired-style", "versions": []}), encoding="utf-8")
    (session / "note.md").write_text("just the body, no heading\n", encoding="utf-8")
    (session / "transcript.txt").write_text("raw words\n", encoding="utf-8")

    def fake_revise(note_text, instructions, backend=None, model=None):
        return CleanResult(title="Plain", body="a shorter body")

    result = pipeline.revise(session, revise_fn=fake_revise, instructions="shorter", backend="ollama")
    assert result.note_text == "a shorter body\n"  # no "# Plain" grew out of the missing style
    assert versions.heading_title((session / "note.md").read_text(encoding="utf-8")) is None

    # ... and a note that does carry one keeps it
    (session / "note.md").write_text("# Plain\n\nbody\n", encoding="utf-8")
    result = pipeline.revise(session, revise_fn=fake_revise, instructions="shorter", backend="ollama")
    assert result.note_text.startswith("# Plain\n\n")


# --- Phase 10 F: applying a take to the note it lands in ------------------------


def _take_session(tmp_path, *, note="# Deploy Notes\n\nStep one.\n", mode="light"):
    d = tmp_path / "2026-08-25-0900-takes"
    d.mkdir(parents=True)
    (d / "note.md").write_text(note, encoding="utf-8")
    (d / "transcript.txt").write_text("first words\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"title": "Deploy Notes", "cleanup_mode": mode}),
                                 encoding="utf-8")
    return d


def _merger(title, body="the merged note"):
    def merge_fn(note_text, new_transcript, mode="edit", backend=None, model=None, instructions=None):
        return CleanResult(title=title, body=body)

    return merge_fn


def test_a_plain_style_merge_keeps_the_notes_own_title(tmp_path, monkeypatch):
    """`output: plain` has no TITLE line to parse, so CleanResult.title is the take's
    first few words — it must not become the note's title."""
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    d = _take_session(tmp_path, note="just the body, no heading\n", mode="email")  # email is plain

    result = pipeline.rerun_take(
        d, 1, clean_fn=_cleaner(), continue_fn=lambda *a, **k: "x",
        merge_fn=_merger("first words of the new take"), how="merge", mode="email",
    )

    assert result["title"] == "Deploy Notes"  # the meta title, not the model's fallback
    assert versions.read_meta(d)["title"] == "Deploy Notes"
    assert (d / "note.md").read_text(encoding="utf-8") == "the merged note\n"  # still headless


def test_a_note_style_merge_takes_the_models_title(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    d = _take_session(tmp_path)
    result = pipeline.rerun_take(
        d, 1, clean_fn=_cleaner(), continue_fn=lambda *a, **k: "x",
        merge_fn=_merger("A Better Title"), how="merge", mode="light",
    )
    assert result["title"] == "A Better Title"
    assert (d / "note.md").read_text(encoding="utf-8") == "# A Better Title\n\nthe merged note\n"


def test_an_append_does_not_stack_a_second_rule(tmp_path, monkeypatch):
    """A note that already ends in `---` would otherwise grow `---\\n\\n---` per take."""
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    d = _take_session(tmp_path, note="# Deploy Notes\n\nStep one.\n\n---\n")

    result = pipeline.rerun_take(
        d, 1, clean_fn=_cleaner(), continue_fn=lambda *a, **k: "the continuation",
        merge_fn=_merger("x"), how="continue", mode="light",
    )
    assert result["note"] == "# Deploy Notes\n\nStep one.\n\n---\n\nthe continuation\n"
    assert "---\n\n---" not in result["note"]

    # the ordinary case still gets its rule
    d2 = _take_session(tmp_path / "plain-end")
    result = pipeline.rerun_take(
        d2, 1, clean_fn=_cleaner(), continue_fn=lambda *a, **k: "the continuation",
        merge_fn=_merger("x"), how="continue", mode="light",
    )
    assert result["note"] == "# Deploy Notes\n\nStep one.\n\n---\n\nthe continuation\n"


# --- double-clean (two compared outputs per saved note) ----------------------


def _double_clean_runner(calls: list):
    """A fake in-process cleanup.clean that resolves each pass by temperature."""
    from vnote.cleanup import CleanResult

    def fake(transcript, mode="edit", backend="ollama", model=None, instructions=None, temperature=None):
        calls.append({"temperature": temperature, "transcript": transcript, "mode": mode,
                      "backend": backend, "model": model})
        if temperature == 0.0:
            return CleanResult(title="Baseline Title", body="deterministic body")
        return CleanResult(title="Variant Title", body=f"varied body at {temperature}")

    return fake


def test_double_clean_writes_named_baseline_and_variant(tmp_path, monkeypatch):
    from vnote import cleanup as _inproc_mod

    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setattr(output, "NOTES_DIR", notes)
    monkeypatch.setattr(config, "double_clean", lambda: True)
    monkeypatch.setattr(config, "variant_temperature", lambda: 0.3)
    calls: list = []
    monkeypatch.setattr(_inproc_mod, "clean", _double_clean_runner(calls))
    src = _audio(tmp_path)

    res = make_note(
        src, transcribe_fn=_transcriber("a long transcript to compare"),
        clean_fn=_cleaner(),  # must NOT be reached while double-clean is on
        mode="edit", backend="ollama", source="file", source_path=str(src), rec_duration=3.0,
    )

    # Baseline runs at 0.0 (deterministic), the variant at the configured temperature.
    assert [(c["temperature"]) for c in calls] == [0.0, 0.3]
    assert all(c["backend"] == "ollama" for c in calls)

    folder = res.session_dir.name
    baseline = res.session_dir / f"{folder}_note.md"
    variant = res.session_dir / f"{folder}_note_variant_t3.md"
    assert baseline.read_text() == "# Baseline Title\n\ndeterministic body\n"
    assert variant.read_text() == "# Variant Title\n\nvaried body at 0.3\n"
    # The app's canonical note.md is the baseline copy (web UI / versions keep reading it).
    assert (res.session_dir / "note.md").read_text() == baseline.read_text()
    assert res.note_text == baseline.read_text()

    meta = json.loads((res.session_dir / "meta.json").read_text())
    assert meta["cleanup_variant_temperature"] == 0.3


def test_double_clean_variant_filename_reflects_temperature(tmp_path, monkeypatch):
    from vnote import cleanup as _inproc_mod

    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setattr(output, "NOTES_DIR", notes)
    monkeypatch.setattr(config, "double_clean", lambda: True)
    monkeypatch.setattr(config, "variant_temperature", lambda: 0.4)
    calls: list = []
    monkeypatch.setattr(_inproc_mod, "clean", _double_clean_runner(calls))
    src = _audio(tmp_path)

    res = make_note(
        src, transcribe_fn=_transcriber("x"), clean_fn=_cleaner(),
        mode="edit", backend="ollama", source="file", source_path=str(src), rec_duration=1.0,
    )
    assert (res.session_dir / f"{res.session_dir.name}_note_variant_t4.md").exists()
    assert not (res.session_dir / f"{res.session_dir.name}_note_variant_t3.md").exists()


def test_double_clean_variant_failure_keeps_the_baseline_note(tmp_path, monkeypatch):
    from vnote import cleanup as _inproc_mod
    from vnote.cleanup import CleanResult

    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setattr(output, "NOTES_DIR", notes)
    monkeypatch.setattr(config, "double_clean", lambda: True)
    monkeypatch.setattr(config, "variant_temperature", lambda: 0.3)

    def boom_then_ok(transcript, mode="edit", backend="ollama", model=None, instructions=None, temperature=None):
        if temperature == 0.0:
            return CleanResult(title="Keep This", body="the safe baseline")
        raise RuntimeError("variant blew up")

    monkeypatch.setattr(_inproc_mod, "clean", boom_then_ok)
    src = _audio(tmp_path)
    res = make_note(
        src, transcribe_fn=_transcriber("x"), clean_fn=_cleaner(),
        mode="edit", backend="ollama", source="file", source_path=str(src), rec_duration=1.0,
    )
    assert res.cleanup_error is None  # the note itself never fails
    assert (res.session_dir / "note.md").read_text().startswith("# Keep This")
    # No variant file, but the named baseline is still written.
    assert (res.session_dir / f"{res.session_dir.name}_note.md").exists()
    assert not any("_note_variant_" in p.name for p in res.session_dir.iterdir())
