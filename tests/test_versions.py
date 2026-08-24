"""Tests for the note's linear version history (versions/note-<n>.md + meta.versions)."""

import json
from datetime import datetime

import pytest

from vnote import versions


def _session(tmp_path, *, note="# One\n\nfirst body\n", meta=None):
    d = tmp_path / "2026-08-20-0900-a-note"
    d.mkdir()
    if note is not None:
        (d / "note.md").write_text(note, encoding="utf-8")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return d


def test_commit_sequence_writes_files_note_md_and_entries(tmp_path):
    d = _session(tmp_path, note=None, meta={"title": "Placeholder", "created": "2026-08-20T09:00:00"})

    n, meta = versions.commit(d, "# One\n\nfirst body", op="clean", title="One", mode="edit",
                              backend="ollama", model="llama3.2:3b",
                              when=datetime(2026, 8, 20, 9, 0, 0))
    assert n == 1
    assert (d / "versions" / "note-1.md").read_text(encoding="utf-8") == "# One\n\nfirst body\n"
    assert (d / "note.md").read_text(encoding="utf-8") == "# One\n\nfirst body\n"
    assert meta["title"] == "One"
    assert (meta["cleanup_mode"], meta["cleanup_backend"], meta["cleanup_model"]) == \
        ("edit", "ollama", "llama3.2:3b")
    assert "recleaned" not in meta  # only a regenerate sets the 0.5.0 flag
    assert meta["versions"] == [{
        "n": 1, "created": "2026-08-20T09:00:00", "op": "clean", "mode": "edit",
        "backend": "ollama", "model": "llama3.2:3b", "instructions": None, "restored_from": None,
    }]

    n, meta = versions.commit(d, "# Two\n\nsecond body\n\n\n", op="regenerate", title="Two",
                              mode="summary", backend="claude-code", model="claude-code (session default)",
                              instructions="make it shorter")
    assert n == 2
    assert (d / "versions" / "note-2.md").read_text(encoding="utf-8") == "# Two\n\nsecond body\n"  # normalized
    assert (d / "note.md").read_text(encoding="utf-8") == "# Two\n\nsecond body\n"
    assert (d / "versions" / "note-1.md").read_text(encoding="utf-8") == "# One\n\nfirst body\n"  # untouched
    assert meta["recleaned"] is True and meta["cleanup_mode"] == "summary"
    assert meta["title"] == "Two"
    assert [e["op"] for e in meta["versions"]] == ["clean", "regenerate"]
    assert meta["versions"][1]["instructions"] == "make it shorter"
    assert meta["versions"][1]["restored_from"] is None
    assert versions.entries(d) == meta["versions"]
    # written the way output.write_session writes it: indent=2 and a trailing newline
    raw = (d / "meta.json").read_text(encoding="utf-8")
    assert raw.endswith("\n") and "\n  \"versions\"" in raw


def test_commit_without_a_title_keeps_the_meta_title(tmp_path):
    d = _session(tmp_path, note=None, meta={"title": "Kept"})
    _, meta = versions.commit(d, "plain text", op="edit")
    assert meta["title"] == "Kept"
    assert meta["versions"][0]["mode"] is None and meta["versions"][0]["op"] == "edit"
    assert "cleanup_mode" not in meta  # an edit does not claim the note was cleaned


def test_restore_entry_records_its_source(tmp_path):
    d = _session(tmp_path, note=None, meta={})
    versions.commit(d, "v1", op="clean")
    versions.commit(d, "v2", op="edit")
    n, meta = versions.commit(d, versions.read(d, 1), op="restore", restored_from=1)
    assert n == 3
    assert meta["versions"][2]["restored_from"] == 1
    assert (d / "note.md").read_text(encoding="utf-8") == "v1\n"


def test_ensure_history_migrates_a_0_5_0_folder(tmp_path):
    d = _session(tmp_path, note="# Legacy\n\nlegacy body\n",
                 meta={"title": "Legacy", "created": "2026-07-01T08:30:00", "cleanup_mode": "light",
                       "cleanup_backend": "ollama", "cleanup_model": "llama3.2:3b"})
    versions.ensure_history(d)

    assert (d / "versions" / "note-1.md").read_text(encoding="utf-8") == "# Legacy\n\nlegacy body\n"
    assert versions.entries(d) == [{
        "n": 1, "created": "2026-07-01T08:30:00", "op": "clean", "mode": "light",
        "backend": "ollama", "model": "llama3.2:3b", "instructions": None, "restored_from": None,
    }]

    versions.ensure_history(d)  # idempotent
    assert len(versions.entries(d)) == 1

    n, _ = versions.commit(d, "# New\n\nnew body\n", op="edit", title="New")
    assert n == 2  # the migrated v1 counts


def test_ensure_history_is_a_no_op_without_a_note(tmp_path):
    d = _session(tmp_path, note=None, meta={"title": "Raw"})
    versions.ensure_history(d)
    assert versions.entries(d) == []
    assert not (d / "versions").exists()


def test_commit_migrates_before_appending(tmp_path):
    d = _session(tmp_path, note="# Old\n\nold body\n", meta={"title": "Old", "created": "2026-07-02T10:00:00"})
    n, meta = versions.commit(d, "# New\n\nnew body\n", op="regenerate", title="New", mode="edit",
                              backend="ollama", model="m")
    assert n == 2
    assert [e["op"] for e in meta["versions"]] == ["clean", "regenerate"]
    assert versions.read(d, 1) == "# Old\n\nold body\n"
    assert versions.read(d, 2) == "# New\n\nnew body\n"


def test_read_missing_version_raises(tmp_path):
    d = _session(tmp_path, note=None, meta={})
    versions.commit(d, "only one", op="clean")
    assert versions.read(d, 1) == "only one\n"
    for bad in (0, -1, 2, 99):
        with pytest.raises(ValueError, match=f"no version {bad}"):
            versions.read(d, bad)


def test_read_meta_and_entries_tolerate_junk(tmp_path):
    d = tmp_path / "2026-08-21-0900-junk"
    d.mkdir()
    assert versions.read_meta(d) == {} and versions.entries(d) == []
    (d / "meta.json").write_text("{not json", encoding="utf-8")
    assert versions.read_meta(d) == {} and versions.entries(d) == []
    (d / "meta.json").write_text('["a list"]', encoding="utf-8")
    assert versions.read_meta(d) == {} and versions.entries(d) == []
    (d / "meta.json").write_text('{"versions": "nope"}', encoding="utf-8")
    assert versions.entries(d) == []


def test_write_meta_matches_write_session(tmp_path):
    d = tmp_path / "2026-08-21-0901-meta"
    d.mkdir()
    versions.write_meta(d, {"a": 1, "b": [2]})
    assert (d / "meta.json").read_text(encoding="utf-8") == json.dumps({"a": 1, "b": [2]}, indent=2) + "\n"


@pytest.mark.parametrize(("text", "expected"), [
    ("# A Tidy Title\n\nbody\n", "A Tidy Title"),
    ("\n\n# Leading blanks\n\nbody", "Leading blanks"),
    ("#NoSpace\n\nbody", None),
    ("just text\n# not the first line\n", None),
    ("", None),
    ("# \n\nbody", None),
    ("## Second level\n", None),
    ("#\tTabbed\n\nbody", "Tabbed"),  # cleanup._split_heading accepts a tab after the '#'
    ("#  Padded  \n", "Padded"),
])
def test_heading_title(text, expected):
    assert versions.heading_title(text) == expected


def test_migration_never_clobbers_existing_version_files(tmp_path):
    """A crash between the version write and the meta write leaves files a log-less
    folder must keep: opening it does not migrate, and the next commit numbers past them."""
    d = _session(tmp_path, note="# Three\n\nthird body\n", meta={"title": "Three"})
    (d / "versions").mkdir()
    for n in (1, 2, 3):
        (d / "versions" / f"note-{n}.md").write_text(f"body {n}\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in (d / "versions").iterdir()}

    versions.ensure_history(d)
    assert {p.name: p.read_bytes() for p in (d / "versions").iterdir()} == before  # untouched
    assert versions.entries(d) == []

    n, meta = versions.commit(d, "# Four\n\nfourth body\n", op="edit", title="Four")
    assert n == 4
    for name, body in before.items():
        assert (d / "versions" / name).read_bytes() == body  # the old three still say what they said
    assert versions.read(d, 4) == "# Four\n\nfourth body\n"
    assert meta["title"] == "Four"


def test_commit_and_ensure_history_refuse_a_corrupt_meta(tmp_path):
    """A meta.json that exists but will not parse is *not* an empty one: rewriting it
    from scratch would drop the version log and every other field."""
    d = _session(tmp_path, note="# One\n\nfirst body\n")
    (d / "meta.json").write_text("{half a fi", encoding="utf-8")
    for call in (lambda: versions.ensure_history(d),
                 lambda: versions.commit(d, "# Two\n\nsecond\n", op="edit")):
        with pytest.raises(ValueError, match="meta.json is not valid JSON"):
            call()
    assert not (d / "versions").exists()
    assert (d / "meta.json").read_text(encoding="utf-8") == "{half a fi"  # left alone
    assert versions.read_meta(d) == {}  # the lenient reader still shrugs, for the listing paths


def test_read_meta_strict_only_forgives_a_missing_file(tmp_path):
    d = _session(tmp_path, note=None)
    assert versions.read_meta_strict(d) == {}
    versions.write_meta(d, {"title": "T"})
    assert versions.read_meta_strict(d) == {"title": "T"}
    (d / "meta.json").write_text('["a list"]', encoding="utf-8")
    with pytest.raises(ValueError, match="meta.json is not valid JSON"):
        versions.read_meta_strict(d)


def test_write_meta_is_atomic(tmp_path):
    """Readers never see a truncated meta.json, and no temp file is left behind."""
    d = _session(tmp_path, note=None, meta={"title": "Old", "versions": []})
    versions.write_meta(d, {"title": "New", "versions": [{"n": 1}]})
    assert json.loads((d / "meta.json").read_text(encoding="utf-8"))["title"] == "New"
    assert sorted(p.name for p in d.iterdir()) == ["meta.json"]


def test_commit_normalizes_crlf(tmp_path):
    d = _session(tmp_path, note=None, meta={})
    versions.commit(d, "# Win\r\n\r\nline one\rline two\r\n\r\n", op="edit")
    assert (d / "note.md").read_text(encoding="utf-8") == "# Win\n\nline one\nline two\n"
    assert versions.read(d, 1) == "# Win\n\nline one\nline two\n"


def test_ensure_history_normalizes_crlf_when_migrating(tmp_path):
    d = _session(tmp_path, note="# Win\r\n\r\nold body\r\n", meta={"title": "Win"})
    versions.ensure_history(d)
    assert versions.read(d, 1) == "# Win\n\nold body\n"


def test_concurrent_commits_keep_a_dense_history(tmp_path):
    """Six threads committing at once must not reuse an n, lose a version file or
    drop a meta field — the whole point of the commit lock plus the atomic meta write."""
    import threading

    d = _session(tmp_path, note=None, meta={"title": "Start", "created": "2026-08-20T09:00:00",
                                            "source": "mic"})
    threads_n, per_thread = 6, 20
    start = threading.Barrier(threads_n)
    done: list[tuple[int, str]] = []
    lock = threading.Lock()

    def worker(w: int) -> None:
        start.wait()
        for i in range(per_thread):
            text = f"worker {w} commit {i}"
            n, _ = versions.commit(d, text, op="edit")
            with lock:
                done.append((n, text + "\n"))

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    total = threads_n * per_thread
    assert sorted(n for n, _ in done) == list(range(1, total + 1))  # dense, no reuse
    for n, text in done:
        assert versions.read(d, n) == text  # every commit's own text survived
    log = versions.entries(d)
    assert [e["n"] for e in log] == list(range(1, total + 1))
    last = max(done)[1]
    assert (d / "note.md").read_text(encoding="utf-8") == last
    assert versions.read(d, total) == last
    meta = versions.read_meta(d)
    assert (meta["title"], meta["created"], meta["source"]) == ("Start", "2026-08-20T09:00:00", "mic")
