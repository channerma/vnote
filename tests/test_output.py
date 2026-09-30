"""Tests for slugging, session-dir creation, and clipboard backend selection."""

import vnote.output as output
from vnote import names
from vnote.output import _slugify, make_session_dir, write_session


def test_slugify_basic():
    assert _slugify("Project: Voyager OS!! overview ") == "project-voyager-os-overview"


def test_slugify_truncates_to_max_words():
    assert _slugify("one two three four five", max_words=3) == "one-two-three"


def test_slugify_empty_input_yields_note():
    assert _slugify("!!!") == "note"
    assert _slugify("") == "note"


def test_make_session_dir_adds_suffix_on_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    from datetime import datetime

    when = datetime(2026, 6, 4, 9, 30)
    a = make_session_dir("same title", when=when)
    b = make_session_dir("same title", when=when)
    assert a != b
    assert a.exists() and b.exists()
    assert b.name.endswith("-2")


def test_write_session_writes_expected_files(tmp_path):
    out = write_session(
        tmp_path,
        audio_src=None,
        transcript="raw transcript",
        note_md="cleaned body",
        title="A Title",
        meta={"k": "v"},
    )
    assert (tmp_path / "transcript.txt").read_text().strip() == "raw transcript"
    assert names.note_path(tmp_path).read_text().startswith("# A Title")
    assert "k" in (tmp_path / "meta.json").read_text()
    assert set(out) >= {"transcript", "note", "meta"}


def test_clipboard_commands_returns_a_list():
    cmds = output._clipboard_commands()
    assert isinstance(cmds, list)
    # every entry is a non-empty argv list
    assert all(isinstance(c, list) and c for c in cmds)
