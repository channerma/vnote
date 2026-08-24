"""Tests for the daemon's HTTP handlers, driven through the real client (no models, no GPU).

Runs the actual server._Handler on an ephemeral port with vnote.transcribe.transcribe
and vnote.cleanup.clean monkeypatched — the handlers import them at call time, so the
fakes are picked up without touching any heavy code path.
"""

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vnote import cleanup, config, daemon, output, server, transcribe
from vnote.audio import BYTES_PER_S as server_BYTES_PER_S
from vnote.cleanup import CleanResult

_seen: dict = {}  # what the fake pipeline functions were called with, per test


def _fake_transcribe(audio_path, language=None):
    _seen["path"] = Path(audio_path)
    _seen["bytes"] = Path(audio_path).read_bytes()
    _seen["language"] = language
    return "fake transcript", {"language": language or "en", "device": "fake"}


def _fake_clean(transcript, mode="edit", backend="ollama", model=None, tone=None, instructions=None):
    _seen["clean"] = (transcript, mode, backend, model, tone, instructions)
    return CleanResult(title="Fake Title", body="Fake body.")


def _fake_revise(note_text, instructions, backend="ollama", model=None):
    _seen["revise"] = (note_text, instructions, backend, model)
    return CleanResult(title="Revised", body="Shorter body.")


def _fake_spans(pcm: bytes) -> list[tuple[float, float]]:
    """Stand-in for vad.speech_spans: nonzero samples are speech (so onnxruntime stays out)."""
    spoken = len(pcm.rstrip(b"\x00"))
    return [(0.0, ((spoken + 1) // 2 * 2) / server_BYTES_PER_S)] if spoken else []


def _drop_live_sessions() -> None:
    for sess in list(server._registry.sessions.values()):
        sess.close(keep_audio=False)  # worker threads must never outlive a test
    server._registry.sessions.clear()


@pytest.fixture
def live_server(monkeypatch, tmp_path):
    _seen.clear()
    _drop_live_sessions()
    server._registry.ttl_s = server._STREAM_TTL_S
    monkeypatch.setattr(server._registry, "vad", _fake_spans)
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)  # nothing a test does may touch the real notes folder
    monkeypatch.setattr(transcribe, "transcribe", _fake_transcribe)
    monkeypatch.setattr(cleanup, "clean", _fake_clean)
    monkeypatch.setattr(cleanup, "revise", _fake_revise)  # the other half of Phase 9's cleanup
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "daemon_addr", lambda: ("127.0.0.1", httpd.server_address[1]))
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    _drop_live_sessions()


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
    assert _seen["clean"] == ("hello", "summary", "ollama", "m", "formal", None)


def test_unknown_path_is_404(live_server):
    with pytest.raises(RuntimeError, match="not found"):
        daemon._post("/bogus", {}, timeout=5)


# --- streaming sessions --------------------------------------------------------
#
# The live model is asynchronous now: /stream/append stores the audio and returns
# at once, and a per-session worker fills in the text. So every assertion about a
# partial polls to a deadline instead of expecting the answer in the reply.

import time  # noqa: E402

SPEECH_SAMPLE = b"\x01\x00"  # _fake_spans reads nonzero samples as speech
SILENCE_SAMPLE = b"\x00\x00"


def _wav_frames(wav: bytes) -> int:
    import wave
    from io import BytesIO

    with wave.open(BytesIO(wav), "rb") as w:
        return w.getnframes()


def _poll(fn, timeout: float = 5.0):
    """Wait for fn() to return something truthy — the live worker is a real thread."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for the live worker")
        time.sleep(0.02)


def _stream_state(sid: str) -> dict:
    """The session's full snapshot, read by appending nothing."""
    status, _, body = _request("POST", f"/stream/append?sid={sid}", b"",
                               {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    return _json.loads(body)


def test_stream_round_trip_with_partials(live_server):
    sess = daemon.StreamSession(language="en")
    half_s = SPEECH_SAMPLE * 8_000  # 0.5 s of speech — the smallest tail worth a pass
    assert sess.append(half_s) == ""  # the request returns before the worker has run
    assert _poll(lambda: sess.append(b"")) == "fake transcript"
    assert _wav_frames(_seen["bytes"]) == 8_000  # the pass saw the *tail*, not a growing buffer
    assert _seen["language"] == "en"

    sess.append(SILENCE_SAMPLE * 24_000)  # 1.5 s of silence closes the segment
    committed = _poll(lambda: _stream_state(sess.sid)["committed"])
    assert [seg["text"] for seg in committed] == ["fake transcript"]
    state = _stream_state(sess.sid)
    assert state["tail"] == "" and state["partial"] == "fake transcript"
    assert state["seconds"] == 2.0

    text, _meta = sess.finish()
    assert text == "fake transcript"
    assert _wav_frames(_seen["bytes"]) == 32_000  # the final pass saw everything appended

    with pytest.raises(RuntimeError, match="unknown stream session"):  # finish() drops the session
        sess.append(half_s)


def test_stream_finish_reports_the_live_transcript(live_server):
    sess = daemon.StreamSession(language="en")
    sess.append(SPEECH_SAMPLE * 8_000)
    _poll(lambda: sess.append(b""))
    status, _, body = _request("POST", f"/stream/finish?sid={sess.sid}", b"")
    assert status == 200, body
    data = _json.loads(body)
    assert data["transcript"] == "fake transcript" and data["meta"]["device"] == "fake"
    assert data["live_transcript"] == "fake transcript"  # what the page already had on screen


def test_stream_finish_writes_the_note_from_the_daemon_held_audio(notes_dir):
    sess = daemon.StreamSession(language="en")
    sess.append(SPEECH_SAMPLE * 8_000)
    _poll(lambda: sess.append(b""))
    status, _, body = _request(
        "POST", f"/stream/finish?sid={sess.sid}&note=1&mode=summary&backend=ollama", b"")
    assert status == 200, body
    data = _json.loads(body)
    assert server._SESSION_RE.fullmatch(data["name"])
    assert data["note"] == "# Fake Title\n\nFake body.\n" and data["transcript"] == "fake transcript"
    assert data["live_transcript"] == "fake transcript"
    assert data["meta"]["source"] == "web-live" and data["meta"]["cleanup_mode"] == "summary"
    assert data["meta"]["versions"]  # the note folder starts its version history like any other

    folder = notes_dir / data["name"]
    assert _wav_frames((folder / "audio.wav").read_bytes()) == 8_000  # no second upload on stop
    assert (folder / "note.md").exists() and (folder / "transcript.txt").exists()
    assert not _seen["path"].exists()  # the temp WAV is gone; the folder holds its own copy
    assert sess.sid not in server._registry.sessions


def test_stream_finish_rejects_a_bad_mode(live_server):
    # A rejected Stop must not take the recording with it: the options are validated
    # while the session is still alive, so the user can fix the mode and stop again.
    sess = daemon.StreamSession()
    sess.append(SPEECH_SAMPLE * 8_000)
    status, _, body = _request("POST", f"/stream/finish?sid={sess.sid}&note=1&mode=nope", b"")
    assert status == 400 and b"bad mode" in body

    live = server._registry.sessions.get(sess.sid)
    assert live is not None and live.pcm_path().read_bytes() == SPEECH_SAMPLE * 8_000
    status, _, body = _request(
        "POST", f"/stream/finish?sid={sess.sid}&note=1&mode=summary&backend=ollama", b"")
    assert status == 200, body  # the retry gets the note, from the same audio


def test_a_second_finish_is_404(live_server):
    sess = daemon.StreamSession()
    sess.append(SPEECH_SAMPLE * 8_000)
    status, _, body = _request("POST", f"/stream/finish?sid={sess.sid}", b"")
    assert status == 200, body
    status, _, body = _request("POST", f"/stream/finish?sid={sess.sid}", b"")
    assert status == 404 and b"unknown stream session" in body  # a double Stop is not a 500


def test_stream_finish_keeps_the_audio_when_transcription_fails(notes_dir, monkeypatch):
    def broken(audio_path, language=None):
        raise RuntimeError("CUDA device lost")

    sess = daemon.StreamSession()
    live = server._registry.sessions[sess.sid]
    sess.append(SPEECH_SAMPLE * 16_000)  # 1 s
    monkeypatch.setattr(transcribe, "transcribe", broken)
    status, _, body = _request("POST", f"/stream/finish?sid={sess.sid}", b"")
    assert status == 500
    data = _json.loads(body)
    assert "CUDA device lost" in data["error"]
    kept = Path(data["audio_kept"])
    assert kept.parent == notes_dir / "failed"
    assert _wav_frames(kept.read_bytes()) == 16_000  # the whole recording, not just the last tail
    assert not live.pcm_path().exists()  # the spill became that WAV; it was not simply deleted
    assert not server._infer_lock.locked()


def test_stream_finish_keeps_the_audio_when_the_transcript_is_empty(notes_dir, monkeypatch):
    def silent(audio_path, language=None):
        return "", {}

    sess = daemon.StreamSession()
    live = server._registry.sessions[sess.sid]
    sess.append(SPEECH_SAMPLE * 16_000)
    monkeypatch.setattr(transcribe, "transcribe", silent)
    status, _, body = _request(
        "POST", f"/stream/finish?sid={sess.sid}&note=1&mode=summary&backend=ollama", b"")
    assert status == 400
    data = _json.loads(body)
    assert "no speech" in data["error"]  # ... but "no speech detected" is the model's opinion, not proof
    kept = Path(data["audio_kept"])
    assert kept.parent == notes_dir / "failed" and _wav_frames(kept.read_bytes()) == 16_000
    assert not live.pcm_path().exists()
    assert [p.name for p in notes_dir.iterdir()] == ["failed"]  # no half-written note folder


def test_stream_start_normalises_the_language(live_server):
    def language_of(sess) -> str | None:
        return server._registry.sessions[sess.sid].language

    assert language_of(daemon.StreamSession(language="auto")) is None
    assert language_of(daemon.StreamSession(language="")) is None
    assert language_of(daemon.StreamSession()) is None
    assert language_of(daemon.StreamSession(language="fr")) == "fr"


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
    server._registry.sessions[sess.sid].last_seen -= server._STREAM_TTL_S + 1
    daemon.StreamSession()  # any /stream/start sweeps expired sessions
    with pytest.raises(RuntimeError, match="unknown stream session"):
        sess.append(b"\x00\x00")


def test_stream_expired_sid_404s_without_a_new_start(live_server):
    # The TTL is enforced on the touch itself — a crashed client's buffer must
    # not linger until some future /stream/start happens to sweep it.
    sess = daemon.StreamSession()
    server._registry.sessions[sess.sid].last_seen -= server._STREAM_TTL_S + 1
    with pytest.raises(RuntimeError, match="unknown stream session"):
        sess.append(b"\x00\x00")
    assert sess.sid not in server._registry.sessions  # buffer freed, not just refused


def test_an_abandoned_session_keeps_its_audio(notes_dir):
    # The daemon owns the audio: a browser that never came back must not take the
    # recording with it, so an expiring session spills its PCM into failed/.
    sess = daemon.StreamSession()
    sess.append(SPEECH_SAMPLE * 16_000)  # 1 s
    live = server._registry.sessions[sess.sid]
    live.last_seen -= server._STREAM_TTL_S + 1
    server._registry.sweep()

    kept = sorted((notes_dir / "failed").glob("live-*.wav"))
    assert len(kept) == 1 and _wav_frames(kept[0].read_bytes()) == 16_000
    assert sess.sid not in server._registry.sessions
    assert not live.pcm_path().exists()  # the temp spill is gone; the WAV is the copy


def test_two_abandoned_sessions_do_not_overwrite_each_other(notes_dir):
    # One sweep saves both inside the same second, so the second-resolution stamp
    # alone would leave one recording only. The session id keeps the names apart.
    first, second = daemon.StreamSession(), daemon.StreamSession()
    for sess in (first, second):
        sess.append(SPEECH_SAMPLE * 16_000)
        server._registry.sessions[sess.sid].last_seen -= server._STREAM_TTL_S + 1
    server._registry.sweep()

    kept = sorted((notes_dir / "failed").glob("live-*.wav"))
    assert len(kept) == 2
    assert {_wav_frames(p.read_bytes()) for p in kept} == {16_000}


# --- the web UI: static files, notes, /api/note, settings, vocab ------------------

import json as _json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


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

    def ollama_500(transcript, mode="edit", backend="ollama", model=None, tone=None, instructions=None):
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


# --- Phase 9: editing, revise, versions, reveal, keepalive ----------------------


def test_detail_carries_the_path_and_the_version_log(notes_dir):
    d = _make_session(notes_dir, "2026-08-10-0900-history", meta={"title": "History"}, audio=None)
    status, data = _get_json("/api/notes/2026-08-10-0900-history")
    assert status == 200
    assert data["path"] == str(d)
    assert [e["op"] for e in data["versions"]] == ["clean"]  # opening a pre-versioning folder snapshots note.md as v1

    _send_json("PUT", "/api/notes/2026-08-10-0900-history/note", {"text": "# Edited\n\nnew body"})
    status, data = _get_json("/api/notes/2026-08-10-0900-history")
    assert [e["op"] for e in data["versions"]] == ["clean", "edit"]  # the old note.md became v1
    assert data["title"] == "Edited"


def test_put_note_saves_an_edit_as_a_new_version(notes_dir):
    d = _make_session(notes_dir, "2026-08-10-0901-edit", meta={"title": "Old"}, audio=None)
    status, data = _send_json("PUT", "/api/notes/2026-08-10-0901-edit/note",
                              {"text": "# Typed\n\nI wrote this by hand."})
    assert status == 200, data
    assert data == {"version": 2, "title": "Typed", "note": "# Typed\n\nI wrote this by hand.\n"}
    assert (d / "note.md").read_text(encoding="utf-8") == "# Typed\n\nI wrote this by hand.\n"
    assert (d / "versions" / "note-2.md").read_text(encoding="utf-8") == "# Typed\n\nI wrote this by hand.\n"
    assert (d / "versions" / "note-1.md").read_text(encoding="utf-8") == "# T\n\nbody\n"

    for bad in ({"text": ""}, {"text": "   \n"}, {"text": 5}, {}):
        status, data = _send_json("PUT", "/api/notes/2026-08-10-0901-edit/note", bad)
        assert status == 400, bad
    status, _ = _send_json("PUT", "/api/notes/2026-01-01-0000-missing/note", {"text": "x"})
    assert status == 404


def test_reclean_passes_instructions_and_returns_a_version(notes_dir):
    d = _make_session(notes_dir, "2026-08-10-0902-redo", meta={"title": "Old", "cleanup_mode": "edit"},
                      audio=None)
    status, data = _send_json("POST", "/api/notes/2026-08-10-0902-redo/reclean",
                              {"mode": "light", "instructions": "make it longer"})
    assert status == 200, data
    assert data == {"title": "Fake Title", "note": "# Fake Title\n\nFake body.\n", "version": 2}
    assert _seen["clean"][5] == "make it longer"
    entries = _json.loads((d / "meta.json").read_text(encoding="utf-8"))["versions"]
    assert [e["op"] for e in entries] == ["clean", "regenerate"]
    assert entries[1]["instructions"] == "make it longer" and entries[1]["mode"] == "light"


def test_revise_rewrites_the_current_note(notes_dir):
    d = _make_session(notes_dir, "2026-08-10-0903-revise", meta={"title": "Old", "cleanup_mode": "light"},
                      note="# Hand Edited\n\nthe note as it stands\n", audio=None)
    status, data = _send_json("POST", "/api/notes/2026-08-10-0903-revise/revise",
                              {"instructions": "shorter", "backend": "ollama", "model": "m"})
    assert status == 200, data
    assert data == {"title": "Revised", "note": "# Revised\n\nShorter body.\n", "version": 2}
    # the reviser sees the note, not the transcript
    assert _seen["revise"] == ("# Hand Edited\n\nthe note as it stands\n", "shorter", "ollama", "m")
    assert (d / "note.md").read_text(encoding="utf-8") == "# Revised\n\nShorter body.\n"
    entries = _json.loads((d / "meta.json").read_text(encoding="utf-8"))["versions"]
    assert entries[1]["op"] == "revise" and entries[1]["instructions"] == "shorter"

    for bad in ({"instructions": ""}, {"instructions": "  "}, {}):
        status, _ = _send_json("POST", "/api/notes/2026-08-10-0903-revise/revise", bad)
        assert status == 400, bad
    status, body = _send_json("POST", "/api/notes/2026-08-10-0903-revise/revise",
                              {"instructions": "x", "backend": "gpt"})
    assert status == 400 and "bad backend" in body["error"]
    status, _ = _send_json("POST", "/api/notes/2026-01-01-0000-missing/revise", {"instructions": "x"})
    assert status == 404


def test_revise_without_a_note_is_400(notes_dir):
    _make_session(notes_dir, "2026-08-10-0904-noteless", note=None, audio=None)
    status, data = _send_json("POST", "/api/notes/2026-08-10-0904-noteless/revise", {"instructions": "x"})
    assert status == 400 and "no note to revise" in data["error"]


def test_get_and_restore_a_version(notes_dir):
    d = _make_session(notes_dir, "2026-08-10-0905-versions", meta={"title": "V1"}, audio=None)
    _send_json("PUT", "/api/notes/2026-08-10-0905-versions/note", {"text": "# V2\n\nsecond"})

    status, data = _get_json("/api/notes/2026-08-10-0905-versions/versions/1")
    assert status == 200
    assert data["n"] == 1 and data["text"] == "# T\n\nbody\n" and data["op"] == "clean"
    status, data = _get_json("/api/notes/2026-08-10-0905-versions/versions/2")
    assert data["text"] == "# V2\n\nsecond\n" and data["op"] == "edit"
    for bad in ("/versions/3", "/versions/0", "/versions/99"):
        status, _ = _get_json(f"/api/notes/2026-08-10-0905-versions{bad}")
        assert status == 404, bad
    status, _ = _get_json("/api/notes/2026-08-10-0905-versions/versions/x")
    assert status == 404  # <n> must be digits
    status, _ = _get_json("/api/notes/2026-01-01-0000-missing/versions/1")
    assert status == 404

    status, data = _send_json("POST", "/api/notes/2026-08-10-0905-versions/restore", {"n": 1})
    assert status == 200, data
    assert data == {"title": "T", "note": "# T\n\nbody\n", "version": 3}
    assert (d / "note.md").read_text(encoding="utf-8") == "# T\n\nbody\n"
    entries = _json.loads((d / "meta.json").read_text(encoding="utf-8"))["versions"]
    assert entries[2]["op"] == "restore" and entries[2]["restored_from"] == 1

    status, _ = _send_json("POST", "/api/notes/2026-08-10-0905-versions/restore", {"n": 99})
    assert status == 404
    for bad in ({"n": "1"}, {"n": 1.5}, {"n": True}, {}):
        status, _ = _send_json("POST", "/api/notes/2026-08-10-0905-versions/restore", bad)
        assert status == 400, bad


def _record_reveal(monkeypatch, *, wsl: bool, wslpath_out: str = "", popen_error: bool = False):
    calls: list[tuple] = []
    monkeypatch.setattr(server, "_is_wsl", lambda: wsl)

    class _Done:
        stdout = wslpath_out

    def fake_run(cmd, **kw):
        calls.append(("run", tuple(cmd), kw.get("timeout")))
        return _Done()

    def fake_popen(cmd, **kw):
        calls.append(("popen", tuple(cmd), kw.get("stdout")))
        if popen_error:
            raise OSError("explorer.exe: no such file")
        return None

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    return calls


def test_reveal_on_wsl_goes_through_wslpath_and_explorer(notes_dir, monkeypatch):
    d = _make_session(notes_dir, "2026-08-10-0906-reveal", audio=None)
    calls = _record_reveal(monkeypatch, wsl=True, wslpath_out="D:\\notes\\2026-08-10-0906-reveal\n")
    status, data = _send_json("POST", "/api/notes/2026-08-10-0906-reveal/reveal", {})
    assert status == 200
    assert data == {"opened": True, "path": str(d)}
    assert calls[0] == ("run", ("wslpath", "-w", str(d)), 5)  # bounded: never blocks the daemon
    assert calls[1] == ("popen", ("explorer.exe", "D:\\notes\\2026-08-10-0906-reveal"),
                        server.subprocess.DEVNULL)


def test_reveal_off_wsl_uses_xdg_open(notes_dir, monkeypatch):
    d = _make_session(notes_dir, "2026-08-10-0907-xdg", audio=None)
    calls = _record_reveal(monkeypatch, wsl=False)
    monkeypatch.setattr(server.sys, "platform", "linux")
    status, data = _send_json("POST", "/api/notes/2026-08-10-0907-xdg/reveal", {})
    assert status == 200 and data == {"opened": True, "path": str(d)}
    assert calls == [("popen", ("xdg-open", str(d)), server.subprocess.DEVNULL)]


def test_reveal_reports_failure_instead_of_raising(notes_dir, monkeypatch):
    d = _make_session(notes_dir, "2026-08-10-0908-broken", audio=None)
    _record_reveal(monkeypatch, wsl=False, popen_error=True)
    status, data = _send_json("POST", "/api/notes/2026-08-10-0908-broken/reveal", {})
    assert status == 200 and data == {"opened": False, "path": str(d)}

    _record_reveal(monkeypatch, wsl=True, wslpath_out="   \n")  # wslpath produced nothing usable
    status, data = _send_json("POST", "/api/notes/2026-08-10-0908-broken/reveal", {})
    assert status == 200 and data == {"opened": False, "path": str(d)}

    status, _ = _send_json("POST", "/api/notes/2026-01-01-0000-missing/reveal", {})
    assert status == 404


def test_clean_endpoint_passes_instructions(live_server):
    status, data = _send_json("POST", "/clean", {"transcript": "hello", "instructions": "make it longer"})
    assert status == 200 and data == {"title": "Fake Title", "body": "Fake body."}
    assert _seen["clean"][5] == "make it longer"


def test_revise_endpoint_mirrors_clean(live_server):
    status, data = _send_json("POST", "/revise", {"note": "# T\n\nbody", "instructions": "shorter",
                                                  "backend": "ollama", "model": "m"})
    assert status == 200 and data == {"title": "Revised", "body": "Shorter body."}
    assert _seen["revise"] == ("# T\n\nbody", "shorter", "ollama", "m")

    for bad in ({}, {"note": "# T\n\nbody"}, {"instructions": "shorter"},
                {"note": "  ", "instructions": "shorter"}, {"note": "# T", "instructions": " "},
                {"note": 5, "instructions": "shorter"}):
        status, data = _send_json("POST", "/revise", bad)
        assert status == 400, bad  # a missing field is a bad request, not a KeyError 500
        assert "error" in data


def test_stream_ping_keeps_a_paused_session_alive(live_server):
    assert server._STREAM_TTL_S == 1800.0  # a pause has to survive; 120 s did not
    sess = daemon.StreamSession()
    live = server._registry.sessions[sess.sid]
    live.last_seen -= 200  # far past the old 120 s TTL
    aged = live.last_seen
    status, data = _send_json("POST", f"/stream/ping?sid={sess.sid}", {})
    assert status == 200 and data == {"ok": True}
    assert live.last_seen > aged  # the ping is what moved the clock
    assert sess.append(b"\x00\x00") == ""  # still usable after the pause

    # A nearly-expired session survives the next sweep *because* of the ping; one that
    # was not pinged does not (/stream/start sweeps before it hands out a new sid).
    doomed = daemon.StreamSession()
    live.last_seen -= server._STREAM_TTL_S - 10  # ~1790 s old
    server._registry.sessions[doomed.sid].last_seen -= server._STREAM_TTL_S + 1
    status, _ = _send_json("POST", f"/stream/ping?sid={sess.sid}", {})
    assert status == 200
    daemon.StreamSession()  # starting a session sweeps the expired ones
    assert sess.sid in server._registry.sessions
    assert doomed.sid not in server._registry.sessions

    status, data = _send_json("POST", "/stream/ping?sid=nope", {})
    assert status == 404 and "unknown stream session" in data["error"]


def test_a_corrupt_meta_does_not_500_a_read(notes_dir):
    """The migration a GET performs is best-effort: an unparsable meta.json still
    serves the note (with no history) instead of failing the request."""
    d = _make_session(notes_dir, "2026-08-10-0910-corrupt", meta="{half a fi", audio=None)
    status, data = _get_json("/api/notes/2026-08-10-0910-corrupt")
    assert status == 200
    assert data["note"] == "# T\n\nbody\n" and data["versions"] == []
    status, _ = _get_json("/api/notes/2026-08-10-0910-corrupt/versions/1")
    assert status == 404  # nothing was migrated, so there is no v1 to read
    assert (d / "meta.json").read_text(encoding="utf-8") == "{half a fi"  # and nothing was rewritten


def test_cross_site_revise_is_refused(notes_dir):
    _make_session(notes_dir, "2026-08-10-0909-xsite", audio=None)
    status, _, _ = _request("POST", "/api/notes/2026-08-10-0909-xsite/revise", b'{"instructions": "shorter"}',
                            {"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert status == 403 and "revise" not in _seen
    status, _, _ = _request("PUT", "/api/notes/2026-08-10-0909-xsite/note", b'{"text": "# X\\n\\ny"}',
                            {"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert status == 403
    assert (notes_dir / "2026-08-10-0909-xsite" / "note.md").read_text(encoding="utf-8") == "# T\n\nbody\n"


def test_opening_a_pre_versions_note_migrates_it(notes_dir):
    # A 0.5.0 folder: note.md + meta.json without "versions". Opening it must show v1 and let
    # Restore work at once — not only after the first edit.
    d = _make_session(notes_dir, "2026-08-10-0900-legacy", meta={"title": "Legacy", "created": "2026-08-10T09:00:00",
                                                                  "cleanup_mode": "edit", "cleanup_backend": "ollama"},
                      note="# Legacy\n\nold text\n", audio=None)
    status, data = _get_json("/api/notes/2026-08-10-0900-legacy")
    assert status == 200
    assert [v["n"] for v in data["versions"]] == [1] and data["versions"][0]["op"] == "clean"
    assert data["versions"][0]["created"] == "2026-08-10T09:00:00" and data["versions"][0]["mode"] == "edit"
    assert (d / "versions" / "note-1.md").read_text(encoding="utf-8") == "# Legacy\n\nold text\n"
    status, v1 = _get_json("/api/notes/2026-08-10-0900-legacy/versions/1")
    assert status == 200 and v1["text"] == "# Legacy\n\nold text\n"
    status, data = _send_json("POST", "/api/notes/2026-08-10-0900-legacy/restore", {"n": 1})
    assert status == 200 and data["version"] == 2


def test_stream_cancel_drops_the_session_and_its_audio(live_server):
    sess = daemon.StreamSession()
    sess.append(b"\x01\x00" * 16000)
    spill = server._registry.sessions[sess.sid].pcm_path()
    assert spill.exists()
    status, _, body = _request("POST", f"/stream/cancel?sid={sess.sid}")
    assert status == 200 and _json.loads(body) == {"cancelled": True}
    assert sess.sid not in server._registry.sessions
    assert not spill.exists()  # cancelled = dropped, nothing kept under failed/
    failed = output.NOTES_DIR / "failed"
    assert not failed.exists() or not any(failed.iterdir())
    status, _, _ = _request("POST", f"/stream/cancel?sid={sess.sid}")
    assert status == 404
