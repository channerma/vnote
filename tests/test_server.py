"""Tests for the daemon's HTTP handlers, driven through the real client (no models, no GPU).

Runs the actual server._Handler on an ephemeral port with vnote.transcribe.transcribe
and vnote.cleanup.clean monkeypatched — the handlers import them at call time, so the
fakes are picked up without touching any heavy code path.
"""

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vnote import cleanup, config, daemon, server, transcribe
from vnote.cleanup import CleanResult

_seen: dict = {}  # what the fake pipeline functions were called with, per test


def _fake_transcribe(audio_path, language=None):
    _seen["path"] = Path(audio_path)
    _seen["bytes"] = Path(audio_path).read_bytes()
    _seen["language"] = language
    return "fake transcript", {"language": language or "en", "device": "fake"}


def _fake_clean(transcript, mode="edit", backend="ollama", model=None, tone=None):
    _seen["clean"] = (transcript, mode, backend, model, tone)
    return CleanResult(title="Fake Title", body="Fake body.")


@pytest.fixture
def live_server(monkeypatch):
    _seen.clear()
    server._sessions.clear()
    monkeypatch.setattr(transcribe, "transcribe", _fake_transcribe)
    monkeypatch.setattr(cleanup, "clean", _fake_clean)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "daemon_addr", lambda: ("127.0.0.1", httpd.server_address[1]))
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_health(live_server):
    h = daemon.health(timeout=5)  # generous: the 0.3s production probe can flake on a busy test box
    assert h is not None
    assert h["status"] == "ok"
    assert h["whisper_model"] == config.WHISPER_MODEL
    assert "device" in h and "uptime_s" in h


def test_transcribe_path_mode(live_server, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")
    text, meta = daemon.transcribe(audio, language="en")
    assert text == "fake transcript"
    assert meta["language"] == "en"
    assert _seen["path"] == audio  # daemon read the file in place, no copy


def test_transcribe_path_mode_missing_file(live_server):
    with pytest.raises(RuntimeError, match="no such file"):
        daemon.transcribe(Path("/definitely/not/here.wav"))


def test_transcribe_bytes_mode(live_server):
    text, meta = daemon.transcribe_bytes(b"FLACDATA", fmt="flac", language="en")
    assert text == "fake transcript"
    assert _seen["bytes"] == b"FLACDATA"  # body landed in the temp file intact
    assert _seen["language"] == "en"
    assert _seen["path"].suffix == ".flac"
    assert not _seen["path"].exists()  # temp upload removed after the call


def test_transcribe_bytes_mode_empty_body(live_server):
    with pytest.raises(RuntimeError, match="empty audio body"):
        daemon.transcribe_bytes(b"")


def test_transcribe_bytes_mode_bad_format(live_server):
    with pytest.raises(RuntimeError, match="bad format"):
        daemon.transcribe_bytes(b"x", fmt="../../etc")


def test_clean_round_trip(live_server):
    result = daemon.clean("hello", mode="summary", backend="ollama", model="m", tone="formal")
    assert result == CleanResult(title="Fake Title", body="Fake body.")
    assert _seen["clean"] == ("hello", "summary", "ollama", "m", "formal")


def test_unknown_path_is_404(live_server):
    with pytest.raises(RuntimeError, match="not found"):
        daemon._post("/bogus", {}, timeout=5)


# --- streaming sessions --------------------------------------------------------


def _wav_frames(wav: bytes) -> int:
    import wave
    from io import BytesIO

    with wave.open(BytesIO(wav), "rb") as w:
        return w.getnframes()


def test_stream_round_trip_with_partials(live_server):
    sess = daemon.StreamSession(language="en")
    quarter_s = b"\x01\x00" * 4_000  # 0.25 s of PCM
    assert sess.append(quarter_s) == ""  # below the 0.5 s partial threshold
    assert sess.append(quarter_s) == "fake transcript"  # threshold crossed -> partial pass
    assert _wav_frames(_seen["bytes"]) == 8_000  # partial saw the full 0.5 s buffer
    assert _seen["language"] == "en"

    text, _meta = sess.finish()
    assert text == "fake transcript"
    assert _wav_frames(_seen["bytes"]) == 8_000  # final saw everything appended

    with pytest.raises(RuntimeError, match="unknown stream session"):  # finish() drops the session
        sess.append(quarter_s)


def test_stream_finish_without_audio_is_an_error(live_server):
    sess = daemon.StreamSession()
    with pytest.raises(RuntimeError, match="no audio"):
        sess.finish()


def test_stream_unknown_sid_is_404(live_server):
    sess = daemon.StreamSession()
    sess.sid = "nope"
    with pytest.raises(RuntimeError, match="unknown stream session"):
        sess.append(b"\x00\x00")


def test_stream_sessions_expire(live_server):
    sess = daemon.StreamSession()
    server._sessions[sess.sid].last_seen -= server._STREAM_TTL_S + 1
    daemon.StreamSession()  # any /stream/start sweeps expired sessions
    with pytest.raises(RuntimeError, match="unknown stream session"):
        sess.append(b"\x00\x00")


def test_stream_expired_sid_404s_without_a_new_start(live_server):
    # The TTL is enforced on the touch itself — a crashed client's buffer must
    # not linger until some future /stream/start happens to sweep it.
    sess = daemon.StreamSession()
    server._sessions[sess.sid].last_seen -= server._STREAM_TTL_S + 1
    with pytest.raises(RuntimeError, match="unknown stream session"):
        sess.append(b"\x00\x00")
    assert sess.sid not in server._sessions  # buffer freed, not just refused


# --- the web UI: static files, notes, /api/note, settings, vocab ------------------

import json as _json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from vnote import output  # noqa: E402


def _url(path: str) -> str:
    host, port = config.daemon_addr()
    return f"http://{host}:{port}{path}"


def _request(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    """(status, headers, body) — errors are returned, not raised."""
    req = urllib.request.Request(_url(path), data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _get_json(path: str):
    status, _, body = _request("GET", path)
    return status, _json.loads(body)


def _send_json(method: str, path: str, payload: dict):
    status, _, body = _request(method, path, _json.dumps(payload).encode(), {"Content-Type": "application/json"})
    return status, _json.loads(body)


def _make_session(root: Path, name: str, *, meta=None, note="# T\n\nbody\n", transcript="raw words",
                  audio: bytes | None = b"RIFF" + bytes(range(100))) -> Path:
    d = root / name
    d.mkdir()
    if meta is not None:
        (d / "meta.json").write_text(meta if isinstance(meta, str) else _json.dumps(meta), encoding="utf-8")
    if note is not None:
        (d / "note.md").write_text(note, encoding="utf-8")
    if transcript is not None:
        (d / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
    if audio is not None:
        (d / "audio.wav").write_bytes(audio)
    return d


@pytest.fixture
def notes_dir(live_server, tmp_path, monkeypatch):
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)  # output.py binds it at import; the server reads it there
    return tmp_path


def test_index_and_static_files(live_server):
    status, headers, body = _request("GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"/static/app.js" in body and b"<script" in body
    status, headers, _ = _request("GET", "/static/app.js")
    assert status == 200 and headers["Content-Type"].startswith("application/javascript")
    status, headers, _ = _request("GET", "/static/style.css")
    assert status == 200 and headers["Content-Type"].startswith("text/css")
    for bad in ("/static/../server.py", "/static/nope.js", "/static/app.py", "/static/"):
        status, _, _ = _request("GET", bad)
        assert status == 404, bad


def test_notes_list_newest_first_and_tolerant(notes_dir):
    _make_session(notes_dir, "2026-08-01-0900-oldest", meta={"title": "Oldest", "created": "2026-08-01T09:00:00",
                                                             "audio_duration_s": 12.5, "cleanup_mode": "edit",
                                                             "cleanup_backend": "ollama"})
    _make_session(notes_dir, "2026-08-02-1000-broken-meta", meta="{not json", audio=None)
    _make_session(notes_dir, "2026-08-03-1100-no-meta", note=None)
    (notes_dir / "flow").mkdir()  # not a session folder
    (notes_dir / "stray.txt").write_text("x", encoding="utf-8")
    status, data = _get_json("/api/notes")
    assert status == 200
    names = [n["name"] for n in data["notes"]]
    assert names == ["2026-08-03-1100-no-meta", "2026-08-02-1000-broken-meta", "2026-08-01-0900-oldest"]
    oldest = data["notes"][2]
    assert oldest["title"] == "Oldest" and oldest["duration_s"] == 12.5 and oldest["mode"] == "edit"
    assert oldest["backend"] == "ollama" and oldest["has_audio"] and oldest["has_note"]
    no_meta = data["notes"][0]
    assert no_meta["title"] == "2026-08-03-1100-no-meta"  # folder name stands in for a missing title
    assert no_meta["created"] == "2026-08-03T11:00:00"  # derived from the folder stamp
    assert no_meta["has_note"] is False and data["notes"][1]["has_audio"] is False


def test_note_detail_and_audio_with_ranges(notes_dir):
    payload = b"RIFF" + bytes(range(100))
    _make_session(notes_dir, "2026-08-05-1200-detail", meta={"title": "Detail", "created": "2026-08-05T12:00:00"},
                  audio=payload)
    status, data = _get_json("/api/notes/2026-08-05-1200-detail")
    assert status == 200
    assert data["title"] == "Detail" and data["note"] == "# T\n\nbody\n" and data["transcript"] == "raw words\n"
    assert data["meta"]["created"] == "2026-08-05T12:00:00"
    assert data["audio_url"] == "/api/notes/2026-08-05-1200-detail/audio"

    status, headers, body = _request("GET", data["audio_url"])
    assert status == 200 and headers["Content-Type"] == "audio/wav" and headers["Accept-Ranges"] == "bytes"
    assert body == payload
    status, headers, body = _request("GET", data["audio_url"], headers={"Range": "bytes=10-19"})
    assert status == 206 and headers["Content-Range"] == f"bytes 10-19/{len(payload)}" and body == payload[10:20]
    status, headers, body = _request("GET", data["audio_url"], headers={"Range": "bytes=90-"})
    assert status == 206 and body == payload[90:]
    status, headers, _ = _request("GET", data["audio_url"], headers={"Range": "bytes=500-600"})
    assert status == 416 and headers["Content-Range"] == f"bytes */{len(payload)}"


def test_note_routes_are_guarded(notes_dir):
    _make_session(notes_dir, "2026-08-05-1200-guarded", audio=None)
    for bad in ("/api/notes/../secret", "/api/notes/2026-08-05-1200-GUARDED", "/api/notes/2026-01-01-0000-missing",
                "/api/notes/2026-08-05-1200-guarded/../../x"):
        status, _, _ = _request("GET", bad)
        assert status == 404, bad
    status, data = _get_json("/api/notes/2026-08-05-1200-guarded/audio")
    assert status == 404 and "no audio" in data["error"]


def test_api_note_end_to_end(notes_dir):
    status, _, body = _request(
        "POST", "/api/note?format=webm&mode=summary&backend=ollama&language=en", b"WEBMDATA",
        {"Content-Type": "application/octet-stream"},
    )
    assert status == 200, body
    data = _json.loads(body)
    assert server._SESSION_RE.fullmatch(data["name"])
    assert data["title"] == "Fake Title"
    assert data["note"] == "# Fake Title\n\nFake body.\n"
    assert data["transcript"] == "fake transcript" and data["cleanup_error"] is None
    assert data["meta"]["source"] == "web" and data["meta"]["cleanup_mode"] == "summary"
    folder = notes_dir / data["name"]
    assert (folder / "audio.webm").read_bytes() == b"WEBMDATA"
    assert (folder / "note.md").read_text(encoding="utf-8") == "# Fake Title\n\nFake body.\n"
    assert (folder / "transcript.txt").exists() and (folder / "meta.json").exists()
    assert _seen["clean"][1:3] == ("summary", "ollama") and _seen["language"] == "en"
    assert not _seen["path"].exists()  # the temp upload is gone; the folder holds its own copy


def test_api_note_raw_skips_cleanup(notes_dir):
    status, _, body = _request("POST", "/api/note?format=webm&raw=1", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    data = _json.loads(body)
    assert data["note"] == "fake transcript" and "clean" not in _seen
    assert not (notes_dir / data["name"] / "note.md").exists()


def test_api_note_rejects_bad_requests(notes_dir):
    status, _, _ = _request("POST", "/api/note?format=webm", b"", {"Content-Type": "application/octet-stream"})
    assert status == 400
    status, _, body = _request("POST", "/api/note?format=webm&mode=loud", b"x")
    assert status == 400 and b"bad mode" in body
    status, _, body = _request("POST", "/api/note?format=../x", b"x")
    assert status == 400 and b"bad format" in body
    status, _, body = _request("POST", "/api/note?backend=gpt", b"x")
    assert status == 400 and b"bad backend" in body


def test_reclean_rewrites_the_note(notes_dir):
    d = _make_session(notes_dir, "2026-08-06-1300-reclean", meta={"title": "Old", "cleanup_mode": "edit"},
                      note="# Old\n\nold body\n", audio=None)
    status, data = _send_json("POST", "/api/notes/2026-08-06-1300-reclean/reclean", {"mode": "light"})
    assert status == 200, data
    assert data["title"] == "Fake Title" and data["note"] == "# Fake Title\n\nFake body.\n"
    assert (d / "note.md").read_text(encoding="utf-8") == "# Fake Title\n\nFake body.\n"
    meta = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["recleaned"] is True and meta["cleanup_mode"] == "light"
    assert _seen["clean"][0] == "raw words"
    status, data = _send_json("POST", "/api/notes/2026-01-01-0000-missing/reclean", {"mode": "light"})
    assert status == 404
    status, data = _send_json("POST", "/api/notes/2026-08-06-1300-reclean/reclean", {"mode": "loud"})
    assert status == 400


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for s in config.SETTINGS:
        monkeypatch.delenv(s.env, raising=False)
    return tmp_path


def test_settings_get_and_put(live_server, clean_env):
    status, data = _get_json("/api/settings")
    assert status == 200
    rows = {row["key"]: row for row in data["settings"]}
    assert rows["backend"]["choices"] == ["ollama", "claude-code", "claude"]
    assert rows["backend"]["source"] == "default" and rows["whisper_model"]["editable"] is False
    status, data = _send_json("PUT", "/api/settings", {"backend": "claude-code", "language": "en"})
    assert status == 200 and data["saved"] == ["backend", "language"]
    assert config.load_config() == {"backend": "claude-code", "language": "en"}
    status, data = _get_json("/api/settings")
    rows = {row["key"]: row for row in data["settings"]}
    assert rows["backend"]["value"] == "claude-code" and rows["backend"]["source"] == "file"
    status, data = _send_json("PUT", "/api/settings", {"whisper_model": "tiny"})
    assert status == 400 and "restart" in data["error"]
    status, data = _send_json("PUT", "/api/settings", {"nope": 1})
    assert status == 400 and "unknown setting" in data["error"]
    status, data = _send_json("PUT", "/api/settings", {"backend": "gpt"})
    assert status == 400 and "must be one of" in data["error"]


def test_vocab_get_and_put(live_server, clean_env):
    status, data = _get_json("/api/vocab")
    assert status == 200 and data == {"text": ""}
    status, data = _send_json("PUT", "/api/vocab", {"text": "Dymola\njason -> JSON"})
    assert status == 200 and data == {"saved": True}
    assert config.vocab_file().read_text(encoding="utf-8") == "Dymola\njason -> JSON\n"
    status, data = _get_json("/api/vocab")
    assert data["text"] == "Dymola\njason -> JSON\n"
    status, data = _send_json("PUT", "/api/vocab", {"text": 5})
    assert status == 400


def test_note_detail_carries_the_list_fields_top_level(notes_dir):
    _make_session(notes_dir, "2026-08-07-1400-fields", meta={"title": "F", "created": "2026-08-07T14:00:00",
                                                             "audio_duration_s": 3.5, "cleanup_mode": "light",
                                                             "cleanup_backend": "claude-code"})
    status, data = _get_json("/api/notes/2026-08-07-1400-fields")
    assert status == 200
    assert (data["created"], data["duration_s"], data["mode"], data["backend"]) == \
        ("2026-08-07T14:00:00", 3.5, "light", "claude-code")
    assert data["has_audio"] is True and data["has_note"] is True


def test_audio_suffix_range_has_content_length(notes_dir):
    payload = bytes(range(104))
    _make_session(notes_dir, "2026-08-07-1401-suffix", audio=payload)
    status, headers, body = _request("GET", "/api/notes/2026-08-07-1401-suffix/audio", headers={"Range": "bytes=-10"})
    assert status == 206 and body == payload[-10:]
    assert headers["Content-Range"] == "bytes 94-103/104" and headers["Content-Length"] == "10"


def test_api_note_empty_transcript_is_400_and_leaves_nothing_behind(notes_dir, monkeypatch):
    seen = {}

    def silent(audio_path, language=None):
        seen["path"] = Path(audio_path)
        return "", {}

    monkeypatch.setattr(transcribe, "transcribe", silent)
    status, _, body = _request("POST", "/api/note?format=webm", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 400 and b"no speech" in body
    assert not seen["path"].exists() and not server._infer_lock.locked()
    assert list(notes_dir.iterdir()) == []


def test_api_note_transcription_failure_keeps_the_audio(notes_dir, monkeypatch):
    def broken(audio_path, language=None):
        raise RuntimeError("CUDA device lost")

    monkeypatch.setattr(transcribe, "transcribe", broken)
    status, _, body = _request("POST", "/api/note?format=webm", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 500
    data = _json.loads(body)
    assert "CUDA device lost" in data["error"]
    kept = Path(data["audio_kept"])
    assert kept.parent == notes_dir / "failed" and kept.suffix == ".webm" and kept.read_bytes() == b"WEBMDATA"
    assert not server._infer_lock.locked()


def test_api_note_cleanup_http_failure_keeps_the_transcript(notes_dir, monkeypatch):
    import urllib.error as ue

    def ollama_500(transcript, mode="edit", backend="ollama", model=None, tone=None):
        raise ue.HTTPError("http://127.0.0.1:11434/api/chat", 500, "cudaMalloc failed", {}, None)

    monkeypatch.setattr(cleanup, "clean", ollama_500)
    status, _, body = _request("POST", "/api/note?format=webm&mode=edit&backend=ollama", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    data = _json.loads(body)
    assert data["note"] == "fake transcript" and "cudaMalloc" in data["cleanup_error"]
    assert (notes_dir / data["name"] / "transcript.txt").exists()
    assert not (notes_dir / data["name"] / "note.md").exists()


def test_raw_recording_ignores_a_bad_default_mode(notes_dir, monkeypatch):
    monkeypatch.setenv("VNOTE_MODE", "bogus")
    status, _, body = _request("POST", "/api/note?format=webm&raw=1", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    status, _, body = _request("POST", "/api/note?format=webm", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 400 and b"bad mode" in body


def test_language_auto_overrides_the_saved_default(notes_dir):
    config.save_config({"language": "de"})
    _request("POST", "/api/note?format=webm&raw=1", b"X", {"Content-Type": "application/octet-stream"})
    assert _seen["language"] == "de"
    _request("POST", "/api/note?format=webm&raw=1&language=auto", b"X", {"Content-Type": "application/octet-stream"})
    assert _seen["language"] is None
    _request("POST", "/api/note?format=webm&raw=1&language=fr", b"X", {"Content-Type": "application/octet-stream"})
    assert _seen["language"] == "fr"


def test_cross_site_writes_are_refused(notes_dir):
    _make_session(notes_dir, "2026-08-07-1402-xsite", audio=None)
    status, _, body = _request("POST", "/api/notes/2026-08-07-1402-xsite/reclean", b'{"mode": "light"}',
                               {"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert status == 403 and "clean" not in _seen
    status, _, _ = _request("PUT", "/api/settings", b'{"language": "en"}',
                            {"Content-Type": "application/json", "Host": "evil.example"})
    assert status == 403 and config.load_config() == {}
    host, port = config.daemon_addr()
    status, _, _ = _request("PUT", "/api/settings", b'{"language": "en"}',
                            {"Content-Type": "application/json", "Origin": f"http://{host}:{port}"})
    assert status == 200 and config.load_config() == {"language": "en"}
    status, _, _ = _request("GET", "/api/notes", headers={"Origin": "https://evil.example"})
    assert status == 200  # reads are harmless (and the browser blocks the response anyway)


def test_malformed_json_bodies_are_400(live_server):
    for method, path in (("PUT", "/api/vocab"), ("PUT", "/api/settings"), ("POST", "/clean")):
        status, _, body = _request(method, path, b"{not json", {"Content-Type": "application/json"})
        assert status == 400 and b"not valid JSON" in body, (method, path)
