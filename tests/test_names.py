"""Per-note file names: prefixed for new notes, bare legacy names still read and edited in place."""

from vnote import names, takes, versions


def test_new_note_gets_prefixed_paths(tmp_path):
    d = tmp_path / "2026-09-29-1200-a-note"
    d.mkdir()
    assert names.note_path(d).name == "2026-09-29-1200-a-note_note.md"
    assert names.minutes_path(d).name == "2026-09-29-1200-a-note_minutes.md"
    assert names.audio_stem(d) == "2026-09-29-1200-a-note_audio"


def test_legacy_bare_files_are_used_until_a_prefixed_one_exists(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    (d / "note.md").write_text("legacy")
    (d / "minutes.md").write_text("legacy")
    assert names.note_path(d) == d / "note.md"
    assert names.minutes_path(d) == d / "minutes.md"
    # A legacy double_clean folder also holds a FROZEN baseline <folder>_note.md next to the live
    # note.md: the bare file must win or every later edit is orphaned.
    (d / "old_note.md").write_text("frozen baseline")
    assert names.note_path(d) == d / "note.md"
    assert names.note_path(d).read_text() == "legacy"


def test_audio_file_finds_prefixed_then_bare_and_take_folders_keep_bare(tmp_path):
    d = tmp_path / "n"
    (d / "takes" / "1").mkdir(parents=True)
    (d / "n_audio.wav").write_bytes(b"RIFF")
    assert takes.audio_file(d) == d / "n_audio.wav"
    (d / "takes" / "1" / "audio.wav").write_bytes(b"RIFF")
    assert takes.audio_file(d / "takes" / "1") == d / "takes" / "1" / "audio.wav"
    (d / "n_audio.wav").unlink()
    (d / "audio.wav").write_bytes(b"RIFF")
    assert takes.audio_file(d) == d / "audio.wav"  # legacy root


def test_commit_edits_a_legacy_note_in_place(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "note.md").write_text("# Old\n\nbody\n")
    versions.commit(d, "# New\n\nbody two\n", op="edit", title="New")
    assert (d / "note.md").read_text() == "# New\n\nbody two\n"
    assert not (d / "legacy_note.md").exists()  # no second copy appears


def test_a_folder_renamed_after_creation_still_finds_its_files(tmp_path):
    d = tmp_path / "renamed-by-hand"
    d.mkdir()
    (d / "orig_note.md").write_text("n")
    (d / "orig_minutes.md").write_text("m")
    (d / "orig_audio.wav").write_bytes(b"RIFF")
    (d / "orig_note_variant_t0p3.md").write_text("v")  # must not be mistaken for the note
    assert names.note_path(d) == d / "orig_note.md"
    assert names.minutes_path(d) == d / "orig_minutes.md"
    assert takes.audio_file(d) == d / "orig_audio.wav"


def test_glob_special_characters_in_a_folder_name(tmp_path):
    d = tmp_path / "weird [1]*name?"
    d.mkdir()
    (d / f"{d.name}_audio.wav").write_bytes(b"RIFF")
    assert takes.audio_file(d) == d / f"{d.name}_audio.wav"


def test_ensure_takes_moves_prefixed_root_audio_into_take_1(tmp_path):
    d = tmp_path / "2026-09-29-1200-n"
    d.mkdir()
    (d / f"{d.name}_audio.wav").write_bytes(b"RIFFone")
    (d / "meta.json").write_text("{}")
    takes.ensure_takes(d)
    assert (d / "takes" / "1" / "audio.wav").read_bytes() == b"RIFFone"  # takes keep the bare name
    assert takes.take_audio(d, 1) == d / "takes" / "1" / "audio.wav"
