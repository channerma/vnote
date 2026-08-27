"""Tests for the takes/ folder mechanics: migration, numbering, the join, trash.

No HTTP and no models here — takes.py is pure filesystem work, so these tests
poke at real folders under tmp_path with ``output.NOTES_DIR`` rebound (trash
lives beside the notes).
"""

import json

import pytest

from vnote import output, takes, versions


@pytest.fixture
def notes_root(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)  # takes.trash_dir() reads it at call time
    return tmp_path


def _note(root, name="2026-08-25-0900-a-note", *, meta=None, note="# T\n\nbody\n",
          transcript="first take words", original=None, audio=b"RIFFone"):
    d = root / name
    d.mkdir()
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    if note is not None:
        (d / "note.md").write_text(note, encoding="utf-8")
    if transcript is not None:
        (d / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
    if original is not None:
        (d / "transcript.original.txt").write_text(original + "\n", encoding="utf-8")
    if audio is not None:
        (d / "audio.wav").write_bytes(audio)
    return d


def _wav(path, seconds=1.0):
    """A real (silent) WAV, so wav_duration() has something to measure."""
    from vnote.audio import BYTES_PER_S, wav_bytes

    path.write_bytes(wav_bytes(b"\x00" * int(seconds * BYTES_PER_S)))
    return path


# --- the migration -----------------------------------------------------------


def test_ensure_takes_moves_the_flat_files_in_the_contract_order(notes_root):
    d = _note(notes_root, meta={"created": "2026-08-25T09:00:00", "audio_duration_s": 12.5},
              original="whisper's own words")
    assert takes.is_multi(d) is False

    takes.ensure_takes(d)

    first = d / "takes" / "1"
    assert takes.is_multi(d) is True
    assert first.joinpath("audio.wav").read_bytes() == b"RIFFone"
    assert not (d / "audio.wav").exists()  # the audio moved (a rename), it was not copied
    assert first.joinpath("transcript.original.txt").read_text(encoding="utf-8") == "whisper's own words\n"
    assert not (d / "transcript.original.txt").exists()
    # the root transcript is *copied*: for one take the join is identical, and every
    # older reader (--redo, Regenerate) keeps finding a file there
    assert first.joinpath("transcript.txt").read_text(encoding="utf-8") == "first take words\n"
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n"
    assert (d / "note.md").exists()

    meta = versions.read_meta(d)
    assert meta["takes"] == [{"n": 1, "created": "2026-08-25T09:00:00", "duration_s": 12.5}]


def test_ensure_takes_is_idempotent_and_never_goes_back(notes_root):
    d = _note(notes_root, meta={"created": "2026-08-25T09:00:00"})
    takes.ensure_takes(d)
    (d / "takes" / "1" / "transcript.txt").write_text("edited by hand\n", encoding="utf-8")

    takes.ensure_takes(d)  # a second call must not re-stamp meta or touch the take

    assert (d / "takes" / "1" / "transcript.txt").read_text(encoding="utf-8") == "edited by hand\n"
    assert takes.numbers(d) == [1]


def test_ensure_takes_takes_the_duration_from_whichever_field_meta_has(notes_root):
    d = _note(notes_root, name="2026-08-25-0901-rec", meta={"recording_duration_s": 7.0})
    takes.ensure_takes(d)
    assert versions.read_meta(d)["takes"][0]["duration_s"] == 7.0


def test_ensure_takes_copes_with_a_note_that_has_no_audio(notes_root):
    d = _note(notes_root, name="2026-08-25-0902-noaudio", meta={"created": "x"}, audio=None)
    takes.ensure_takes(d)
    assert takes.take_audio(d, 1) is None
    assert (d / "takes" / "1" / "transcript.txt").is_file()


# --- listing and reading -----------------------------------------------------


def test_list_takes_synthesizes_one_take_for_a_flat_note(notes_root):
    d = _note(notes_root, meta={"created": "2026-08-25T09:00:00", "audio_duration_s": 4.0})
    assert takes.list_takes(d) == [{"n": 1, "created": "2026-08-25T09:00:00",
                                    "duration_s": 4.0, "path": str(d)}]
    assert takes.take_transcript(d, 1) == "first take words"
    assert takes.take_audio(d, 1) == d / "audio.wav"
    with pytest.raises(FileNotFoundError):
        takes.take_transcript(d, 2)


def test_list_takes_reads_the_folders_and_annotates_them_from_meta(notes_root):
    d = _note(notes_root, meta={"created": "2026-08-25T09:00:00", "audio_duration_s": 4.0})
    takes.add_take(d, _wav(notes_root / "in.wav", 2.0), "second take words", "2026-08-25T10:00:00", 2.0)

    listed = takes.list_takes(d)
    assert [t["n"] for t in listed] == [1, 2]
    assert listed[1]["created"] == "2026-08-25T10:00:00" and listed[1]["duration_s"] == 2.0
    assert listed[1]["path"] == str(d / "takes" / "2")


# --- adding takes ------------------------------------------------------------


def test_add_take_moves_the_audio_writes_the_join_and_sums_the_duration(notes_root):
    d = _note(notes_root, meta={"created": "2026-08-25T09:00:00", "audio_duration_s": 4.0})
    src = _wav(notes_root / "in.wav", 2.0)

    n = takes.add_take(d, src, "second take words", "2026-08-25T10:00:00", 2.0)

    assert n == 2
    assert not src.exists()  # moved in, not copied: one file, one home
    assert (d / "takes" / "2" / "audio.wav").is_file()
    assert (d / "takes" / "2" / "transcript.txt").read_text(encoding="utf-8") == "second take words\n"
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nsecond take words\n"
    meta = versions.read_meta(d)
    assert meta["audio_duration_s"] == 6.0
    assert [t["n"] for t in meta["takes"]] == [1, 2]


def test_add_take_keeps_the_uploads_own_suffix(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    src = notes_root / "in.webm"
    src.write_bytes(b"WEBMDATA")
    takes.add_take(d, src, "browser words", "2026-08-25T10:00:00", None)
    assert (d / "takes" / "2" / "audio.webm").read_bytes() == b"WEBMDATA"


def test_take_numbers_are_never_reused_after_a_delete(notes_root):
    """A version entry says ``take: 2`` and must stay true, so 2 is spent for good."""
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "two", "2026-08-25T10:00:00", 1.0)
    takes.delete_take(d, 2)
    assert takes.numbers(d) == [1]

    n = takes.add_take(d, _wav(notes_root / "b.wav"), "three", "2026-08-25T11:00:00", 1.0)
    assert n == 3
    assert takes.numbers(d) == [1, 3]
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nthree\n"


# --- per-take transcript edits ----------------------------------------------


def test_write_take_transcript_keeps_the_original_once_and_rebuilds_the_join(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second take words", "2026-08-25T10:00:00", 1.0)

    takes.write_take_transcript(d, 2, "the words I meant")
    take2 = d / "takes" / "2"
    assert take2.joinpath("transcript.original.txt").read_text(encoding="utf-8") == "second take words\n"
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nthe words I meant\n"

    takes.write_take_transcript(d, 2, "third thoughts")  # the first edit was the only chance
    assert take2.joinpath("transcript.original.txt").read_text(encoding="utf-8") == "second take words\n"
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nthird thoughts\n"

    with pytest.raises(FileNotFoundError):
        takes.write_take_transcript(d, 9, "nowhere")


def test_write_take_transcript_on_a_flat_note_edits_the_root_files(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    takes.write_take_transcript(d, 1, "edited")
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "edited"
    assert (d / "transcript.original.txt").read_text(encoding="utf-8") == "first take words\n"
    assert takes.is_multi(d) is False  # editing a transcript is not a reason to migrate


def test_joined_transcript_can_be_asked_for_a_subset(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)
    takes.add_take(d, _wav(notes_root / "b.wav"), "third", "2026-08-25T11:00:00", 1.0)
    assert takes.joined_transcript(d, [1, 3]) == "first take words\n\nthird"
    assert takes.joined_transcript(d) == "first take words\n\nsecond\n\nthird"


# --- deletes are moves -------------------------------------------------------


def test_delete_take_moves_it_to_trash_and_leaves_the_note_alone(notes_root):
    d = _note(notes_root, meta={"created": "x", "audio_duration_s": 4.0})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second take words", "2026-08-25T10:00:00", 2.0)

    dest = takes.delete_take(d, 2)

    assert dest == notes_root / "trash" / f"{d.name}.takes" / "take-2"
    assert (dest / "audio.wav").is_file() and (dest / "transcript.txt").is_file()
    assert not (d / "takes" / "2").exists()
    assert (d / "note.md").read_text(encoding="utf-8") == "# T\n\nbody\n"  # untouched by design
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n"
    meta = versions.read_meta(d)
    assert [t["n"] for t in meta["takes"]] == [1] and meta["audio_duration_s"] == 4.0


def test_delete_take_refuses_the_last_one(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    with pytest.raises(ValueError, match="only take"):
        takes.delete_take(d, 1)  # still flat: one take
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)
    takes.delete_take(d, 1)
    with pytest.raises(ValueError, match="only take"):
        takes.delete_take(d, 2)
    assert (d / "takes" / "2" / "audio.wav").is_file()  # a refused delete keeps its hands off
    with pytest.raises(FileNotFoundError):
        takes.delete_take(d, 7)


def test_trash_note_moves_the_whole_folder(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    dest = takes.trash_note(d)
    assert dest == notes_root / "trash" / d.name
    assert (dest / "note.md").is_file() and (dest / "audio.wav").is_file()
    assert not d.exists()
    assert takes.trash_entries() == 1


def test_a_trashed_name_that_is_taken_gets_a_suffix(notes_root):
    """Deleting, re-recording under the same name and deleting again must not overwrite."""
    first = takes.trash_note(_note(notes_root, transcript="one"))
    second = takes.trash_note(_note(notes_root, transcript="two"))
    third = takes.trash_note(_note(notes_root, transcript="three"))

    assert first.name == "2026-08-25-0900-a-note"
    assert second.name == "2026-08-25-0900-a-note-2"
    assert third.name == "2026-08-25-0900-a-note-3"
    assert first.joinpath("transcript.txt").read_text(encoding="utf-8") == "one\n"
    assert second.joinpath("transcript.txt").read_text(encoding="utf-8") == "two\n"


def test_a_trashed_take_number_that_is_taken_gets_a_suffix(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    (notes_root / "trash" / f"{d.name}.takes" / "take-2").mkdir(parents=True)
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)
    assert takes.delete_take(d, 2).name == "take-2-2"


def test_the_audio_fallback_follows_the_earliest_take(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)
    assert takes.first_take_audio(d) == d / "takes" / "1" / "audio.wav"
    takes.delete_take(d, 1)
    assert takes.first_take_audio(d) == d / "takes" / "2" / "audio.wav"


# --- crashes and refusals: nothing may take the only copy of a transcript ------


def test_an_interrupted_migration_is_finished_by_the_next_call(notes_root, monkeypatch):
    """The takes/ folder is not the marker: a half-done migration must be completed.

    Reproduces the data-loss shape: the copy of the root transcript into takes/1 fails
    (a kill, a full disk), and the *next* Continue must not treat the empty take as the
    truth and rebuild the root transcript — the only full copy — from it.
    """
    d = _note(notes_root, meta={"created": "2026-08-25T09:00:00", "audio_duration_s": 4.0})

    def boom(src, dest):
        raise OSError("No space left on device")

    monkeypatch.setattr(takes, "_copy_atomic", boom)
    with pytest.raises(OSError, match="No space"):
        takes.add_take(d, _wav(notes_root / "in.wav"), "second", "2026-08-25T10:00:00", 1.0)

    assert (d / "takes").is_dir() and not (d / "takes" / "1" / "transcript.txt").exists()
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n"  # intact
    assert versions.read_meta(d).get("takes") is None  # the migration never claimed to be done

    monkeypatch.undo()
    n = takes.add_take(d, _wav(notes_root / "in.wav"), "second", "2026-08-25T10:00:00", 1.0)

    assert n == 2
    assert (d / "takes" / "1" / "transcript.txt").read_text(encoding="utf-8") == "first take words\n"
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nsecond\n"


def test_rebuild_join_refuses_to_join_over_a_missing_transcript(notes_root):
    """Better a loud failure than a root transcript rewritten from a take mid-write."""
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)
    (d / "takes" / "2" / "transcript.txt").unlink()

    with pytest.raises(OSError):
        takes.rebuild_join(d)
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nsecond\n"


def test_a_failed_transcript_write_leaves_the_audio_where_it_was(notes_root, monkeypatch):
    """The caller still owns that recording, so add_take may not consume it on the way out."""
    d = _note(notes_root, meta={"created": "x"})
    takes.ensure_takes(d)
    src = _wav(notes_root / "in.wav")

    def boom(path, text):
        raise OSError("No space left on device")

    monkeypatch.setattr(takes, "_write_atomic", boom)
    with pytest.raises(OSError):
        takes.add_take(d, src, "second", "2026-08-25T10:00:00", 1.0)
    assert src.is_file()  # _preserve_upload still has something to keep


def test_a_trash_move_that_fails_moves_nothing(notes_root, monkeypatch):
    """os.rename only: a copytree/rmtree fallback can half-delete a take that is being read."""
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)

    def busy(src, dst):
        raise OSError("Device or resource busy")

    monkeypatch.setattr(takes.os, "rename", busy)
    with pytest.raises(OSError, match="busy"):
        takes.delete_take(d, 2)
    with pytest.raises(OSError, match="busy"):
        takes.trash_note(d)

    assert (d / "takes" / "2" / "audio.wav").is_file() and (d / "takes" / "2" / "transcript.txt").is_file()
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first take words\n\nsecond\n"
    assert [t["n"] for t in versions.read_meta(d)["takes"]] == [1, 2]  # meta was never touched


def test_a_note_trashed_mid_recording_is_not_recreated(notes_root):
    """A DELETE that won the lock must not have its folder resurrected by an in-flight take."""
    d = _note(notes_root, meta={"created": "x"})
    takes.trash_note(d)
    src = _wav(notes_root / "in.wav")

    with pytest.raises(FileNotFoundError):
        takes.add_take(d, src, "orphan", "2026-08-25T10:00:00", 1.0)
    assert not d.exists() and src.is_file()  # no zombie folder, and the audio is still there


def test_trash_entries_counts_notes_and_takes(notes_root):
    d = _note(notes_root, meta={"created": "x"})
    takes.add_take(d, _wav(notes_root / "a.wav"), "second", "2026-08-25T10:00:00", 1.0)
    takes.delete_take(d, 2)
    assert takes.trash_entries() == 1
    takes.trash_note(d)
    assert takes.trash_entries() == 2  # one trashed take plus one trashed note
