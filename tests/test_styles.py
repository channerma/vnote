"""Tests for the style registry: the file format, the three sources, and the editor's writes."""

import os

import pytest

from vnote import config, styles


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The registry caches on (path, mtime) in a module global; each test starts clean."""
    styles._invalidate()
    yield
    styles._invalidate()


def _write(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- the file format ----------------------------------------------------------


def test_parse_front_matter_and_body():
    style = styles.parse("email", (
        "---\n"
        "description: an email draft\n"
        "output: plain\n"
        "backend: claude-code\n"
        "model: llama3.2:3b\n"
        "# a whole-line comment, and an unknown key below\n"
        "colour: red\n"
        "---\n"
        "Turn the transcript into an email.\n"
    ))
    assert style.description == "an email draft"
    assert (style.output, style.backend, style.model) == ("plain", "claude-code", "llama3.2:3b")
    assert style.body == "Turn the transcript into an email."


def test_a_description_keeps_its_hash_but_the_others_take_comments():
    """A description is prose; only the constrained fields take a trailing comment."""
    style = styles.parse("x", (
        "---\n"
        "description: sprint #12 review, tagged #done\n"
        "output: note        # note | plain\n"
        "backend: ollama     # or claude-code\n"
        "model: big:14b      # pull it once\n"
        "---\n"
        "body"
    ))
    assert style.description == "sprint #12 review, tagged #done"
    assert (style.output, style.backend, style.model) == ("note", "ollama", "big:14b")


def test_parse_without_front_matter_is_all_body():
    style = styles.parse("bare", "Just the instruction.\n")
    assert style.body == "Just the instruction."
    assert (style.description, style.output, style.backend, style.model) == ("", "note", None, None)


def test_parse_unclosed_front_matter_is_body_too():
    style = styles.parse("odd", "---\ndescription: never closed\nstill going")
    assert style.description == ""
    assert style.body.startswith("---")


def test_parse_blank_optional_fields_mean_unset():
    style = styles.parse("x", "---\nbackend:\nmodel:\n---\nbody")
    assert style.backend is None and style.model is None


def test_parse_rejects_a_bad_output():
    with pytest.raises(ValueError, match="bad output"):
        styles.parse("x", "---\noutput: notes\n---\nbody")


def test_parse_rejects_a_bad_backend():
    with pytest.raises(ValueError, match="bad backend"):
        styles.parse("x", "---\nbackend: gpt\n---\nbody")


def test_parse_rejects_a_bad_name():
    for name in ("Edit", "with space", "-leading", "x" * 41, ""):
        with pytest.raises(ValueError, match="bad style name"):
            styles.parse(name, "body")


def test_raw_is_reserved_for_the_page_and_never_a_style():
    """A raw.md would show twice in the dropdown and never run: raw skips the LLM first."""
    with pytest.raises(ValueError, match="reserved name"):
        styles.parse("raw", "body")
    with pytest.raises(ValueError, match="reserved name"):
        styles.write("raw", "body")
    assert not (styles.mine_dir() / "raw.md").exists()


def test_a_raw_file_on_disk_is_skipped_and_reported(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(extra, "raw", "body")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(extra))
    styles._invalidate()
    reg = styles.load()
    assert "raw" not in reg.names()
    assert any("reserved name" in p for p in reg.problems)


def test_parse_rejects_an_empty_body():
    with pytest.raises(ValueError, match="no instruction text"):
        styles.parse("x", "---\ndescription: nothing to say\n---\n\n")


# --- the shipped built-ins ----------------------------------------------------


def test_the_six_built_ins_ship_and_parse():
    reg = styles.load()
    assert set(reg.names()) >= {"light", "edit", "summary", "dictation", "prompt", "email"}
    assert reg.problems == []
    assert reg.get("dictation").output == "plain"
    assert reg.get("edit").output == "note"
    assert reg.get("prompt").backend == "claude-code"
    assert reg.get("light").source == "builtin"


# --- sources and precedence ---------------------------------------------------


def test_mine_overrides_a_built_in_and_an_extra_folder_overrides_mine(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(styles.mine_dir(), "edit", "---\ndescription: mine\n---\nmine wins over the built-in")
    _write(extra, "edit", "---\ndescription: team\n---\nthe folder wins over mine")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(extra))
    styles._invalidate()

    assert styles.get("edit").description == "team"
    assert styles.get("edit").source == str(extra)

    monkeypatch.delenv("VNOTE_STYLES_DIRS")
    styles._invalidate()
    assert styles.get("edit").description == "mine"
    assert styles.get("edit").source == "mine"


def test_later_extra_folder_wins(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    _write(first, "shared", "first")
    _write(second, "shared", "second")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", os.pathsep.join([str(first), str(second)]))
    styles._invalidate()
    assert styles.get("shared").body == "second"


def test_the_same_folder_listed_twice_is_one_group(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(extra, "standup", "body")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", os.pathsep.join([str(extra), str(extra) + "/.", str(extra)]))
    styles._invalidate()
    labels = [g["label"] for g in styles.load().groups()]
    assert labels == ["team", "Built-in"] and labels.count("team") == 1


def test_mine_named_again_in_the_extra_folders_is_not_a_second_group(tmp_path, monkeypatch):
    _write(styles.mine_dir(), "notes", "body")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(styles.mine_dir()))
    styles._invalidate()
    groups = styles.load().groups()
    assert [g["label"] for g in groups] == ["Mine", "Built-in"]
    assert styles.get("notes").source == "mine"


def test_groups_are_mine_then_folders_then_built_in(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(styles.mine_dir(), "notes", "mine only")
    _write(extra, "standup", "team only")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(extra))
    styles._invalidate()

    groups = styles.load().groups()
    assert [g["label"] for g in groups] == ["Mine", "team", "Built-in"]
    assert [s["name"] for s in groups[0]["styles"]] == ["notes"]
    assert [s["name"] for s in groups[1]["styles"]] == ["standup"]
    # every style appears exactly once, under the source that won it
    listed = [s["name"] for g in groups for s in g["styles"]]
    assert sorted(listed) == sorted(set(listed)) == styles.names()


def test_an_empty_group_is_not_offered():
    assert [g["label"] for g in styles.load().groups()] == ["Built-in"]  # nothing in Mine yet


# --- problems (never a failed start) ------------------------------------------


def test_an_unreadable_folder_is_a_problem_not_a_crash(tmp_path, monkeypatch):
    not_a_folder = tmp_path / "styles.txt"
    not_a_folder.write_text("I am a file", encoding="utf-8")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(not_a_folder))
    styles._invalidate()

    reg = styles.load()
    assert any("cannot read this styles folder" in p for p in reg.problems)
    assert "edit" in reg.names()  # the built-ins still load


def test_a_folder_under_an_unreadable_parent_never_raises(tmp_path, monkeypatch):
    """Path.exists() re-raises EACCES; the registry must warn or stay quiet, never take
    the daemon down (this is what /api/styles and every non-raw recording sit on)."""
    parent = tmp_path / "locked"
    (parent / "styles").mkdir(parents=True)
    parent.chmod(0o000)
    try:
        try:
            os.listdir(parent)
            pytest.skip("this filesystem (or this user) ignores chmod 000")
        except OSError:
            pass
        monkeypatch.setenv("VNOTE_STYLES_DIRS", str(parent / "styles"))
        styles._invalidate()
        reg = styles.load()  # must not raise
        assert "edit" in reg.names()  # the built-ins still load
    finally:
        parent.chmod(0o700)


def test_an_unreadable_folder_itself_is_a_problem(tmp_path, monkeypatch):
    folder = tmp_path / "locked-styles"
    folder.mkdir()
    folder.chmod(0o000)
    try:
        try:
            os.listdir(folder)
            pytest.skip("this filesystem (or this user) ignores chmod 000")
        except OSError:
            pass
        monkeypatch.setenv("VNOTE_STYLES_DIRS", str(folder))
        styles._invalidate()
        assert any("cannot read this styles folder" in p for p in styles.load().problems)
    finally:
        folder.chmod(0o700)


def test_a_missing_folder_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(tmp_path / "nope"))
    styles._invalidate()
    assert styles.load().problems == []


def test_an_invalid_file_is_skipped_and_reported(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(extra, "broken", "---\noutput: notes\n---\nbody")
    _write(extra, "fine", "body")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(extra))
    styles._invalidate()

    reg = styles.load()
    assert "broken" not in reg.names() and "fine" in reg.names()
    assert len(reg.problems) == 1 and "bad output" in reg.problems[0] and "broken.md" in reg.problems[0]


def test_a_file_whose_name_is_not_a_style_name_is_reported(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(extra, "Not A Name", "body")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(extra))
    styles._invalidate()
    assert any("bad style name" in p for p in styles.load().problems)


# --- the mtime cache ----------------------------------------------------------


def test_load_is_cached_until_a_file_changes():
    first = styles.load()
    assert styles.load() is first  # nothing moved: the same object, no re-read

    path = _write(styles.mine_dir(), "notes", "first text")
    assert styles.load().get("notes").body == "first text"

    stamp = path.stat().st_mtime
    path.write_text("second text", encoding="utf-8")
    os.utime(path, (stamp + 10, stamp + 10))  # not luck: a clock-independent bump
    assert styles.load().get("notes").body == "second text"


def test_a_new_file_invalidates_the_cache():
    before = set(styles.load().names())
    _write(styles.mine_dir(), "notes", "body")
    assert set(styles.load().names()) - before == {"notes"}


# --- writing (Mine only) ------------------------------------------------------


def test_write_lands_in_mine_and_shows_up_at_once():
    style = styles.write("terse", "---\ndescription: short\n---\nBe brief.")
    assert style.path == styles.mine_dir() / "terse.md"
    assert style.path.read_text(encoding="utf-8").endswith("\n")
    assert styles.get("terse").description == "short"


def test_write_of_a_built_in_name_creates_the_override_copy():
    styles.write("edit", "---\ndescription: my edit\n---\nDo it my way.")
    assert styles.get("edit").source == "mine"
    assert (styles.mine_dir() / "edit.md").is_file()
    # and deleting the copy brings the built-in back
    styles.delete("edit")
    assert styles.get("edit").source == "builtin"


def test_write_refuses_an_invalid_style_before_anything_lands():
    with pytest.raises(ValueError, match="bad output"):
        styles.write("nope", "---\noutput: sideways\n---\nbody")
    with pytest.raises(ValueError, match="bad style name"):
        styles.write("../escape", "body")
    assert not (styles.mine_dir() / "nope.md").exists()


def test_delete_refuses_a_built_in_and_an_unknown_name():
    with pytest.raises(PermissionError, match="not in your styles folder"):
        styles.delete("light")
    assert styles.get("light") is not None  # still there
    with pytest.raises(FileNotFoundError, match="no such style"):
        styles.delete("never-existed")


def test_delete_refuses_an_extra_folders_file(tmp_path, monkeypatch):
    extra = tmp_path / "team"
    _write(extra, "standup", "body")
    monkeypatch.setenv("VNOTE_STYLES_DIRS", str(extra))
    styles._invalidate()
    with pytest.raises(PermissionError):
        styles.delete("standup")
    assert (extra / "standup.md").is_file()


# --- the config seam ----------------------------------------------------------


def test_styles_dirs_parses_the_pathsep_list(tmp_path, monkeypatch):
    monkeypatch.setenv("VNOTE_STYLES_DIRS", os.pathsep.join([str(tmp_path / "a"), "", str(tmp_path / "b")]))
    assert config.styles_dirs() == [tmp_path / "a", tmp_path / "b"]
    monkeypatch.delenv("VNOTE_STYLES_DIRS")
    assert config.styles_dirs() == []


def test_mine_dir_sits_next_to_the_vocabulary():
    assert styles.mine_dir() == config.config_dir() / "styles"
    assert styles.mine_dir().parent == config.vocab_file().parent
