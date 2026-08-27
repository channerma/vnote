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


def _fake_continue(note_text, new_transcript, mode="edit", backend=None, model=None, instructions=None):
    _seen["continue"] = (note_text, new_transcript, mode, backend, model, instructions)
    return "Continued body."


def _fake_merge(note_text, new_transcript, mode="edit", backend=None, model=None, instructions=None):
    _seen["merge"] = (note_text, new_transcript, mode, backend, model, instructions)
    return CleanResult(title="Merged Title", body="Merged body.")


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
    monkeypatch.setattr(cleanup, "continue_note", _fake_continue)  # Phase 10 F: the two take prompts
    monkeypatch.setattr(cleanup, "merge_note", _fake_merge)
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
    # No model is loaded in the test server, so /health reports what *would* load.
    assert h["whisper_model"] == config.whisper_model()
    assert "device" in h and "uptime_s" in h
    assert h["warm"] is False  # no warm thread runs in tests: the model is never loaded
    assert h["warm_error"] is None  # ... and nothing failed either
    assert h["ollama"] == "unknown"  # ... so the background warm never reached Ollama


def test_warm_in_background_survives_a_failed_whisper_load(monkeypatch):
    """A model that never loads must be *reported*, not left as a page warming forever."""
    monkeypatch.setattr(server, "_warm_error", None)
    monkeypatch.setattr(server, "_ollama_state", "unknown")

    def boom():
        raise RuntimeError("no such model: tiny-typo")

    monkeypatch.setattr(server, "_warm", boom)
    monkeypatch.setattr(server, "_warm_ollama", lambda: pytest.fail("ollama warmed after a failed load"))

    server._warm_in_background()  # never raises: the daemon keeps serving

    assert server._warm_error == "no such model: tiny-typo"
    assert server._ollama_state == "skipped"


def test_health_reports_a_warm_error(live_server, monkeypatch):
    monkeypatch.setattr(server, "_warm_error", "no such model: tiny-typo")
    h = daemon.health(timeout=5)
    assert h["warm_error"] == "no such model: tiny-typo"


def test_warm_ollama_skipped_when_the_backend_is_not_ollama(monkeypatch):
    monkeypatch.setattr(server, "_ollama_state", "unknown")
    monkeypatch.setenv("VNOTE_BACKEND", "claude-code")

    def boom(model):  # nothing may reach the Ollama client
        raise AssertionError("preload_ollama called for a non-Ollama backend")

    monkeypatch.setattr(cleanup, "preload_ollama", boom)
    server._warm_ollama()
    assert server._ollama_state == "skipped"


def test_warm_ollama_ready_and_absent(monkeypatch):
    monkeypatch.setattr(server, "_ollama_state", "unknown")
    monkeypatch.setenv("VNOTE_BACKEND", "ollama")
    monkeypatch.setenv("VNOTE_OLLAMA_MODEL", "fake:1b")
    seen: list[str] = []
    monkeypatch.setattr(cleanup, "preload_ollama", lambda model: seen.append(model))
    server._warm_ollama()
    assert seen == ["fake:1b"]
    assert server._ollama_state == "ready"

    def fail(model):
        raise RuntimeError("ollama is not installed")

    monkeypatch.setattr(cleanup, "preload_ollama", fail)
    server._warm_ollama()  # a failed warm is reported, never raised: the daemon keeps serving
    assert server._ollama_state == "absent"


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


def test_stream_finish_rejects_a_bad_style(live_server):
    # A rejected Stop must not take the recording with it: the options are validated
    # while the session is still alive, so the user can fix the style and stop again.
    sess = daemon.StreamSession()
    sess.append(SPEECH_SAMPLE * 8_000)
    status, _, body = _request("POST", f"/stream/finish?sid={sess.sid}&note=1&mode=nope", b"")
    assert status == 400 and b"bad style" in body

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
    assert status == 400 and b"bad style" in body
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
    assert rows["backend"]["choices"] == ["ollama", "claude-code", "opencode", "claude"]
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


# --- styles: the registry behind the dropdowns and the Settings editor ---------


@pytest.fixture
def styles_env(clean_env):
    """A config dir of this test's own, and no registry cache from another test."""
    from vnote import styles

    styles._invalidate()
    yield styles
    styles._invalidate()


def test_styles_list_groups_the_registry(live_server, styles_env):
    styles_env.write("terse", "---\ndescription: short\n---\nBe brief.")
    status, data = _get_json("/api/styles")
    assert status == 200
    assert data["mine_dir"] == str(styles_env.mine_dir()) and data["problems"] == []
    assert [g["label"] for g in data["groups"]] == ["Mine", "Built-in"]
    assert [s["name"] for s in data["groups"][0]["styles"]] == ["terse"]
    built_in = {s["name"]: s for s in data["groups"][1]["styles"]}
    assert built_in["dictation"]["output"] == "plain" and built_in["prompt"]["backend"] == "claude-code"
    assert built_in["edit"]["body"] and built_in["edit"]["path"].endswith("edit.md")


def test_style_get_put_and_delete(live_server, styles_env):
    status, data = _get_json("/api/styles/edit")
    assert status == 200 and data["mine"] is False and data["source"] == "builtin"
    assert data["text"].startswith("---")

    status, data = _send_json("PUT", "/api/styles/terse", {"text": "---\ndescription: short\n---\nBe brief."})
    assert status == 201 and data["saved"] == "terse"
    assert (styles_env.mine_dir() / "terse.md").is_file()
    status, data = _get_json("/api/styles/terse")
    assert status == 200 and data["mine"] is True and "Be brief." in data["text"]

    status, data = _send_json("PUT", "/api/styles/terse", {"text": "even shorter"})
    assert status == 200  # an existing file: an update, not a creation

    status, _, body = _request("DELETE", "/api/styles/terse")
    assert status == 204 and body == b""
    assert not (styles_env.mine_dir() / "terse.md").exists()


def test_style_writes_and_deletes_are_refused_where_they_should_be(live_server, styles_env):
    status, data = _get_json("/api/styles/nope")
    assert status == 404 and "no such style" in data["error"]
    status, data = _send_json("PUT", "/api/styles/Not%20A%20Name", {"text": "body"})
    assert status == 400 and "bad style name" in data["error"]  # the name is unquoted, then checked
    status, data = _send_json("PUT", "/api/styles/terse", {"text": "---\noutput: sideways\n---\nbody"})
    assert status == 400 and "bad output" in data["error"]
    status, data = _send_json("PUT", "/api/styles/terse", {"text": 5})
    assert status == 400

    status, _, body = _request("DELETE", "/api/styles/light")  # a built-in is not ours to remove
    assert status == 403 and b"not in your styles folder" in body
    assert styles_env.get("light") is not None
    status, _, body = _request("DELETE", "/api/styles/never-existed")
    assert status == 404


def test_editing_a_built_in_writes_the_override_copy(live_server, styles_env):
    status, _ = _send_json("PUT", "/api/styles/edit", {"text": "---\ndescription: my edit\n---\nMy way."})
    assert status == 201
    status, data = _get_json("/api/styles")
    mine = {s["name"] for s in data["groups"][0]["styles"]}
    assert data["groups"][0]["label"] == "Mine" and mine == {"edit"}
    assert "edit" not in {s["name"] for s in data["groups"][-1]["styles"]}  # once, under the winner


def test_note_options_take_a_style_name_and_leave_the_backend_open():
    mode, backend, model, raw = server._note_options({"mode": ["light"]})
    assert (mode, backend, raw) == ("light", None, False)  # blank backend = the style's, then the setting
    mode, backend, _model, _raw = server._note_options({"mode": ["light"], "backend": ["claude-code"]})
    assert backend == "claude-code"
    with pytest.raises(server._BadRequest, match="bad style"):
        server._note_options({"mode": ["gone"]})
    with pytest.raises(server._BadRequest, match="bad backend"):
        server._note_options({"mode": ["light"], "backend": ["gpt"]})


def test_reclean_records_the_style_and_its_output_shape(notes_dir):
    d = _make_session(notes_dir, "2026-08-25-1300-styled", meta={"title": "Old", "cleanup_mode": "edit"},
                      note="# Old\n\nold body\n", audio=None)
    status, data = _send_json("POST", "/api/notes/2026-08-25-1300-styled/reclean", {"mode": "email"})
    assert status == 200, data
    # email is `output: plain`: the note has no heading, and the style name is recorded
    assert data["note"] == "Fake body.\n"
    meta = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["versions"][-1]["mode"] == "email"
    status, data = _send_json("POST", "/api/notes/2026-08-25-1300-styled/reclean", {"mode": "gone"})
    assert status == 400 and "bad style" in data["error"]


def test_reclean_uses_the_styles_backend_unless_one_is_picked(notes_dir):
    d = _make_session(notes_dir, "2026-08-25-1400-backend", meta={"title": "Old", "cleanup_mode": "edit"},
                      note="# Old\n\nold body\n", audio=None)
    # `prompt` names claude-code in its front matter, and nothing here overrides it
    status, data = _send_json("POST", "/api/notes/2026-08-25-1400-backend/reclean", {"mode": "prompt"})
    assert status == 200, data
    assert _seen["clean"][2] == "claude-code"
    meta = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["versions"][-1]["backend"] == "claude-code"
    status, data = _send_json("POST", "/api/notes/2026-08-25-1400-backend/reclean",
                              {"mode": "prompt", "backend": "ollama"})
    assert status == 200 and _seen["clean"][2] == "ollama"  # an explicit pick still wins


def test_note_detail_flags_a_style_that_is_gone(notes_dir):
    _make_session(notes_dir, "2026-08-25-1500-gone", meta={"title": "G", "cleanup_mode": "retired"})
    status, data = _get_json("/api/notes/2026-08-25-1500-gone")
    assert status == 200 and data["style_missing"] is True
    _make_session(notes_dir, "2026-08-25-1501-here", meta={"title": "H", "cleanup_mode": "edit"})
    status, data = _get_json("/api/notes/2026-08-25-1501-here")
    assert data["style_missing"] is False
    _make_session(notes_dir, "2026-08-25-1502-raw", meta={"title": "R", "cleanup_mode": None})
    status, data = _get_json("/api/notes/2026-08-25-1502-raw")
    assert data["style_missing"] is False  # a raw note never had one


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


def test_raw_recording_ignores_a_bad_default_style(notes_dir, monkeypatch):
    monkeypatch.setenv("VNOTE_STYLE", "bogus")
    status, _, body = _request("POST", "/api/note?format=webm&raw=1", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    status, _, body = _request("POST", "/api/note?format=webm", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 400 and b"bad style" in body


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


def test_transcript_edit_feeds_the_next_regenerate(notes_dir):
    """Phase 10 C end to end: a raw note, an edited transcript, then Regenerate."""
    status, _, body = _request("POST", "/api/note?format=webm&raw=1", b"WEBMDATA",
                               {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    name = _json.loads(body)["name"]
    folder = notes_dir / name

    status, data = _get_json(f"/api/notes/{name}")
    assert status == 200
    assert data["note"] is None and data["meta"]["cleanup_mode"] is None  # nothing failed: no LLM ran
    assert data["transcript_edited"] is False and not (folder / "note.md").exists()

    status, data = _send_json("PUT", f"/api/notes/{name}/transcript", {"text": "the words I meant"})
    assert status == 200, data
    assert data == {"transcript": "the words I meant", "transcript_edited": True}
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "the words I meant"
    assert (folder / "transcript.original.txt").read_text(encoding="utf-8") == "fake transcript\n"

    # a second edit leaves Whisper's copy alone — the first edit was the only chance to keep it
    status, _ = _send_json("PUT", f"/api/notes/{name}/transcript", {"text": "second thoughts"})
    assert status == 200
    assert (folder / "transcript.original.txt").read_text(encoding="utf-8") == "fake transcript\n"

    status, data = _get_json(f"/api/notes/{name}")
    assert data["transcript"] == "second thoughts" and data["transcript_edited"] is True

    status, data = _send_json("POST", f"/api/notes/{name}/reclean", {"mode": "light"})
    assert status == 200, data
    assert _seen["clean"][0] == "second thoughts"  # the edit is what the model saw, not Whisper's output
    assert (folder / "note.md").read_text(encoding="utf-8") == "# Fake Title\n\nFake body.\n"
    entries = _json.loads((folder / "meta.json").read_text(encoding="utf-8"))["versions"]
    assert [e["op"] for e in entries] == ["regenerate"] and data["version"] == 1


def test_transcript_edit_rejects_bad_requests(notes_dir):
    d = _make_session(notes_dir, "2026-08-11-0900-transcript", meta={"title": "T"}, audio=None)
    for bad in ({"text": 5}, {"text": None}, {}):
        status, _ = _send_json("PUT", "/api/notes/2026-08-11-0900-transcript/transcript", bad)
        assert status == 400, bad
    assert not (d / "transcript.original.txt").exists()  # a refused write keeps its hands off both files
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "raw words\n"
    status, _ = _send_json("PUT", "/api/notes/2026-01-01-0000-missing/transcript", {"text": "x"})
    assert status == 404
    status, _, _ = _request("PUT", "/api/notes/nope/transcript", b'{"text": "x"}',
                            {"Content-Type": "application/json"})
    assert status == 404


def test_transcript_can_be_cleared(notes_dir):
    """Emptying the pane is a legitimate edit; the original stays put."""
    d = _make_session(notes_dir, "2026-08-11-0901-cleared", meta={"title": "T"}, audio=None)
    status, data = _send_json("PUT", "/api/notes/2026-08-11-0901-cleared/transcript", {"text": ""})
    assert status == 200 and data["transcript"] == ""
    assert (d / "transcript.txt").read_text(encoding="utf-8") == ""
    assert (d / "transcript.original.txt").read_text(encoding="utf-8") == "raw words\n"


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


# --- Phase 10 F: takes — continue, re-run, delete ------------------------------


def _continue_note(notes_dir, name, **kw):
    """A note ready to be continued: one take, flat, with a cleaned note.md."""
    meta = {"title": "Old", "cleanup_mode": "light", "created": "2026-08-25T16:00:00",
            "audio_duration_s": 3.0}
    meta.update(kw.pop("meta", {}))
    return _make_session(notes_dir, name, meta=meta, note=kw.pop("note", "# Old\n\nfirst body\n"),
                         transcript=kw.pop("transcript", "first words"), **kw)


def _start_continue(name: str, language: str | None = "en"):
    status, data = _send_json("POST", f"/stream/start?continue={name}", {"language": language})
    return status, data


def _record_and_finish(sid: str, query: str, pcm: bytes = SPEECH_SAMPLE * 16_000):
    _request("POST", f"/stream/append?sid={sid}", pcm, {"Content-Type": "application/octet-stream"})
    status, _, body = _request("POST", f"/stream/finish?sid={sid}&note=1&{query}", b"")
    return status, _json.loads(body)


def _versions_of(folder: Path) -> list[dict]:
    return _json.loads((folder / "meta.json").read_text(encoding="utf-8"))["versions"]


def test_continue_through_the_live_stream_adds_take_2_and_a_version(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1600-continue")
    status, data = _start_continue("2026-08-25-1600-continue")
    assert status == 200 and data["note"] == "2026-08-25-1600-continue"
    sid = data["session_id"]

    status, detail = _get_json("/api/notes/2026-08-25-1600-continue")
    assert detail["live"] is True  # the page shows the note is being recorded into

    status, data = _record_and_finish(sid, "continue=2026-08-25-1600-continue&how=continue")
    assert status == 200, data
    assert data["take"] == 2 and data["live_transcript"] == "fake transcript"
    # the reply is the note's full detail payload
    assert data["name"] == "2026-08-25-1600-continue" and data["live"] is False
    assert data["note"] == "# Old\n\nfirst body\n\n---\n\nContinued body.\n"
    assert [t["n"] for t in data["takes"]] == [1, 2]
    assert data["takes"][1]["audio_url"] == "/api/notes/2026-08-25-1600-continue/takes/2/audio"
    assert data["takes"][1]["transcript"] == "fake transcript"
    assert data["duration_s"] == 4.0  # 3 s of take 1 plus the second's measured 1 s
    assert data["has_audio"] is True  # the list view follows the migration too
    status, listing = _get_json("/api/notes")
    assert listing["notes"][0]["has_audio"] is True and listing["notes"][0]["duration_s"] == 4.0

    assert (d / "takes" / "1" / "audio.wav").is_file() and (d / "takes" / "2" / "audio.wav").is_file()
    assert not (d / "audio.wav").exists()  # the flat note migrated on the way in
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first words\n\nfake transcript\n"
    assert (d / "note.md").read_text(encoding="utf-8") == "# Old\n\nfirst body\n\n---\n\nContinued body.\n"
    entry = _versions_of(d)[-1]
    assert (entry["op"], entry["how"], entry["take"]) == ("continue", "continue", 2)
    # the model saw the note as context and only the new take's transcript, in the note's own style
    assert _seen["continue"][:3] == ("# Old\n\nfirst body\n", "fake transcript", "light")


def test_continue_how_append_cleans_only_the_new_take(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1601-append")
    _, data = _start_continue("2026-08-25-1601-append")
    status, data = _record_and_finish(data["session_id"], "continue=2026-08-25-1601-append&how=append")
    assert status == 200, data
    assert data["note"] == "# Old\n\nfirst body\n\n---\n\nFake body.\n"
    assert _seen["clean"][0] == "fake transcript"  # the take alone, not the joined transcript
    assert "continue" not in _seen
    entry = _versions_of(d)[-1]
    assert (entry["op"], entry["how"], entry["take"]) == ("continue", "append", 2)


def test_continue_how_merge_rewrites_the_whole_note(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1602-merge")
    _, data = _start_continue("2026-08-25-1602-merge")
    status, data = _record_and_finish(data["session_id"], "continue=2026-08-25-1602-merge&how=merge")
    assert status == 200, data
    assert data["note"] == "# Merged Title\n\nMerged body.\n" and data["title"] == "Merged Title"
    assert _seen["merge"][:2] == ("# Old\n\nfirst body\n", "fake transcript")
    entry = _versions_of(d)[-1]
    assert (entry["op"], entry["how"], entry["take"]) == ("merge", "merge", 2)
    assert (d / "versions" / "note-2.md").read_text(encoding="utf-8") == "# Merged Title\n\nMerged body.\n"


def test_continuing_a_raw_note_keeps_the_take_and_writes_no_version(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1603-raw", note=None,
                       meta={"title": "Raw", "cleanup_mode": None})
    _, data = _start_continue("2026-08-25-1603-raw")
    status, data = _record_and_finish(data["session_id"], "continue=2026-08-25-1603-raw&how=continue")
    assert status == 200, data
    assert data["take"] == 2 and data["versions"] == [] and data["note"] is None
    assert "continue" not in _seen and "clean" not in _seen  # nothing was cleaned
    assert (d / "takes" / "2" / "transcript.txt").read_text(encoding="utf-8") == "fake transcript\n"
    assert not (d / "note.md").exists()


def test_continue_with_raw_1_leaves_the_note_alone(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1604-rawflag")
    _, data = _start_continue("2026-08-25-1604-rawflag")
    status, data = _record_and_finish(data["session_id"], "continue=2026-08-25-1604-rawflag&raw=1")
    assert status == 200, data
    assert data["take"] == 2 and "continue" not in _seen
    assert (d / "note.md").read_text(encoding="utf-8") == "# Old\n\nfirst body\n"  # untouched
    assert [e["op"] for e in _versions_of(d)] == ["clean"]  # only the migration's v1


def test_continue_through_the_mediarecorder_fallback(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1605-upload")
    status, _, body = _request(
        "POST", "/api/note?format=webm&continue=2026-08-25-1605-upload&how=continue", b"WEBMDATA",
        {"Content-Type": "application/octet-stream"})
    assert status == 200, body
    data = _json.loads(body)
    assert data["take"] == 2 and data["name"] == "2026-08-25-1605-upload"
    assert (d / "takes" / "2" / "audio.webm").read_bytes() == b"WEBMDATA"  # the upload's own format
    assert data["takes"][1]["duration_s"] is None  # a webm's length is nobody's to guess here
    assert not _seen["path"].exists()  # the temp upload is gone; the take folder holds it
    assert _versions_of(d)[-1]["take"] == 2


def test_a_cleanup_failure_after_the_take_answers_500_with_the_take(notes_dir, monkeypatch):
    d = _continue_note(notes_dir, "2026-08-25-1606-broken")

    def boom(note_text, new_transcript, mode="edit", backend=None, model=None, instructions=None):
        raise RuntimeError("cudaMalloc failed")

    monkeypatch.setattr(cleanup, "continue_note", boom)
    _, data = _start_continue("2026-08-25-1606-broken")
    spill = server._registry.sessions[data["session_id"]].pcm_path()
    status, data = _record_and_finish(data["session_id"], "continue=2026-08-25-1606-broken&how=continue")

    assert status == 500 and data["take"] == 2 and "cudaMalloc" in data["error"]
    take = d / "takes" / "2"
    assert _wav_frames((take / "audio.wav").read_bytes()) == 16_000  # the recording is safe
    assert (take / "transcript.txt").read_text(encoding="utf-8") == "fake transcript\n"
    assert data["audio_kept"] == str(take / "audio.wav")  # ... and the reply says where
    assert (d / "note.md").read_text(encoding="utf-8") == "# Old\n\nfirst body\n"  # the note is untouched
    assert not spill.exists()  # the take holds the audio: no second copy in failed/
    assert not (notes_dir / "failed").exists()


def test_continue_validates_before_the_session_is_taken_out(notes_dir):
    """A refused Stop must leave the recording running, exactly as a bad style does."""
    _continue_note(notes_dir, "2026-08-25-1607-guard")
    _, data = _start_continue("2026-08-25-1607-guard")
    sid = data["session_id"]
    _request("POST", f"/stream/append?sid={sid}", SPEECH_SAMPLE * 16_000,
             {"Content-Type": "application/octet-stream"})

    status, _, body = _request("POST", f"/stream/finish?sid={sid}&note=1&continue=2026-08-25-1607-guard"
                                       "&how=sideways", b"")
    assert status == 400 and b"bad how" in body
    status, _, body = _request("POST", f"/stream/finish?sid={sid}&note=1&continue=2026-01-01-0000-gone", b"")
    assert status == 404 and b"no such note" in body
    assert sid in server._registry.sessions  # still recording; the user can just stop again

    status, data = _record_and_finish(sid, "continue=2026-08-25-1607-guard&how=continue", b"")
    assert status == 200 and data["take"] == 2


def test_only_one_live_session_may_continue_a_note(notes_dir):
    """Two tabs continuing at once would each add a take to a note the other is writing."""
    _continue_note(notes_dir, "2026-08-25-1609-once")
    status, data = _start_continue("2026-08-25-1609-once")
    assert status == 200
    status, second = _send_json("POST", "/stream/start?continue=2026-08-25-1609-once", {})
    assert status == 409 and "already going" in second["error"]
    assert len(server._registry.sessions) == 1  # nothing was started behind the 409

    _record_and_finish(data["session_id"], "how=append")
    status, _ = _start_continue("2026-08-25-1609-once")
    assert status == 200  # the first one finished: the note is free again


def test_stream_start_refuses_an_unknown_note(live_server):
    status, data = _send_json("POST", "/stream/start?continue=2026-01-01-0000-gone", {})
    assert status == 404 and "no such note" in data["error"]
    assert server._registry.sessions == {}  # nothing was started behind the 404


def test_a_bound_session_is_enough_to_continue_on_stop(notes_dir):
    """The binding survives Stop on its own: the page need not repeat ?continue=."""
    d = _continue_note(notes_dir, "2026-08-25-1608-bound")
    _, data = _start_continue("2026-08-25-1608-bound")
    status, data = _record_and_finish(data["session_id"], "how=append")
    assert status == 200, data
    assert data["take"] == 2 and data["name"] == "2026-08-25-1608-bound"
    assert list(notes_dir.iterdir()) == [d]  # no second note folder was made


# --- re-running a take against the current note --------------------------------


def test_rerun_a_take_makes_a_new_version(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1610-rerun")
    _, data = _start_continue("2026-08-25-1610-rerun")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1610-rerun&how=append")
    _seen.pop("continue", None)

    status, data = _send_json("POST", "/api/notes/2026-08-25-1610-rerun/takes/2/rerun",
                              {"how": "continue"})
    assert status == 200, data
    assert data["take"] == 2 and data["version"] == 3
    assert data["note"].endswith("---\n\nContinued body.\n")
    # the re-run works on the note as it stands now, with the take's transcript
    assert _seen["continue"][0] == "# Old\n\nfirst body\n\n---\n\nFake body.\n"
    assert _seen["continue"][1] == "fake transcript"
    assert _seen["continue"][2] == "light"  # no mode in the body: the note's own style decides
    entry = _versions_of(d)[-1]
    assert (entry["op"], entry["how"], entry["take"]) == ("continue", "continue", 2)


def test_rerun_rejects_what_it_cannot_do(notes_dir):
    _continue_note(notes_dir, "2026-08-25-1611-rerunbad")
    status, data = _send_json("POST", "/api/notes/2026-08-25-1611-rerunbad/takes/1/rerun",
                              {"how": "sideways"})
    assert status == 400 and "bad how" in data["error"]
    status, data = _send_json("POST", "/api/notes/2026-08-25-1611-rerunbad/takes/1/rerun",
                              {"how": "continue", "mode": "gone"})
    assert status == 400 and "bad style" in data["error"]
    status, data = _send_json("POST", "/api/notes/2026-08-25-1611-rerunbad/takes/9/rerun", {})
    assert status == 404 and "no take 9" in data["error"]
    status, data = _send_json("POST", "/api/notes/2026-01-01-0000-missing/takes/1/rerun", {})
    assert status == 404

    _make_session(notes_dir, "2026-08-25-1612-noclean", note=None, audio=None)
    status, data = _send_json("POST", "/api/notes/2026-08-25-1612-noclean/takes/1/rerun", {})
    assert status == 400 and "regenerate" in data["error"]  # the page offers Regenerate instead


# --- regenerating from a subset of the takes -----------------------------------


def test_reclean_from_a_subset_of_takes_records_which_ones(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1620-subset")
    _, data = _start_continue("2026-08-25-1620-subset")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1620-subset&how=append")

    status, data = _send_json("POST", "/api/notes/2026-08-25-1620-subset/reclean",
                              {"mode": "light", "takes": [1]})
    assert status == 200, data
    assert _seen["clean"][0] == "first words"  # only take 1's text reached the model
    entry = _versions_of(d)[-1]
    assert entry["op"] == "regenerate" and entry["takes"] == [1]

    status, data = _send_json("POST", "/api/notes/2026-08-25-1620-subset/reclean",
                              {"mode": "light", "takes": [1, 2]})
    assert status == 200 and _seen["clean"][0] == "first words\n\nfake transcript"
    assert _versions_of(d)[-1]["takes"] == [1, 2]

    for bad in ([], "1", [1, "2"], [True]):
        status, data = _send_json("POST", "/api/notes/2026-08-25-1620-subset/reclean",
                                  {"mode": "light", "takes": bad})
        assert status == 400, bad
    status, data = _send_json("POST", "/api/notes/2026-08-25-1620-subset/reclean",
                              {"mode": "light", "takes": [9]})
    assert status == 404 and "no take 9" in data["error"]


# --- per-take audio and transcripts --------------------------------------------


def test_take_audio_is_served_with_ranges(notes_dir):
    _continue_note(notes_dir, "2026-08-25-1630-audio")
    _, data = _start_continue("2026-08-25-1630-audio")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1630-audio&how=append")

    url = "/api/notes/2026-08-25-1630-audio/takes/2/audio"
    status, headers, body = _request("GET", url)
    assert status == 200 and headers["Content-Type"] == "audio/wav"
    assert _wav_frames(body) == 16_000
    status, headers, ranged = _request("GET", url, headers={"Range": "bytes=0-9"})
    assert status == 206 and ranged == body[:10] and headers["Accept-Ranges"] == "bytes"

    # the note-level route still works after the migration: it follows take 1
    status, headers, body = _request("GET", "/api/notes/2026-08-25-1630-audio/audio")
    assert status == 200 and body == b"RIFF" + bytes(range(100))
    status, data = _get_json("/api/notes/2026-08-25-1630-audio/takes/9/audio")
    assert status == 404 and "no audio for take 9" in data["error"]
    status, _ = _get_json("/api/notes/2026-01-01-0000-missing/takes/1/audio")
    assert status == 404


def test_put_a_take_transcript_keeps_the_original_and_rebuilds_the_join(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1631-edit")
    _, data = _start_continue("2026-08-25-1631-edit")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1631-edit&how=append")

    status, data = _send_json("PUT", "/api/notes/2026-08-25-1631-edit/takes/2/transcript",
                              {"text": "the words I meant"})
    assert status == 200 and data == {"transcript": "the words I meant", "transcript_edited": True}
    assert (d / "takes" / "2" / "transcript.original.txt").read_text(encoding="utf-8") == "fake transcript\n"
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first words\n\nthe words I meant\n"
    status, detail = _get_json("/api/notes/2026-08-25-1631-edit")
    assert detail["takes"][1]["transcript_edited"] is True and detail["takes"][0]["transcript_edited"] is False

    status, _ = _send_json("PUT", "/api/notes/2026-08-25-1631-edit/takes/9/transcript", {"text": "x"})
    assert status == 404
    status, _ = _send_json("PUT", "/api/notes/2026-08-25-1631-edit/takes/2/transcript", {"text": 5})
    assert status == 400


def test_a_flat_notes_take_1_is_the_note_itself(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1632-flat")
    status, detail = _get_json("/api/notes/2026-08-25-1632-flat")
    assert status == 200
    assert [t["n"] for t in detail["takes"]] == [1]
    assert detail["takes"][0]["audio_url"] == "/api/notes/2026-08-25-1632-flat/audio"
    assert detail["takes"][0]["transcript"] == "first words"

    status, _ = _send_json("PUT", "/api/notes/2026-08-25-1632-flat/takes/1/transcript", {"text": "edited"})
    assert status == 200
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "edited"
    assert (d / "transcript.original.txt").read_text(encoding="utf-8") == "first words\n"
    assert not (d / "takes").exists()  # editing a transcript is not a reason to migrate


# --- deletes: moves into trash/ -------------------------------------------------


def test_delete_a_take_moves_it_to_trash(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1640-deltake")
    _, data = _start_continue("2026-08-25-1640-deltake")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1640-deltake&how=append")

    status, data = _send_json("DELETE", "/api/notes/2026-08-25-1640-deltake/takes/2", {})
    assert status == 200, data
    assert data["take"] == 2
    trashed = Path(data["trashed"])
    # a take goes to its own namespace: never inside trash/<name>/, which is where a
    # whole trashed note of that name lives
    assert trashed == notes_dir / "trash" / "2026-08-25-1640-deltake.takes" / "take-2"
    assert _wav_frames((trashed / "audio.wav").read_bytes()) == 16_000  # moved, not unlinked
    assert not (d / "takes" / "2").exists()
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first words\n"
    assert (d / "note.md").exists()  # untouched by design: the Body regenerates or edits
    status, detail = _get_json("/api/notes/2026-08-25-1640-deltake")
    assert [t["n"] for t in detail["takes"]] == [1]
    assert detail["duration_s"] == 3.0 and detail["has_audio"] is True  # the sum, rebuilt

    status, data = _send_json("DELETE", "/api/notes/2026-08-25-1640-deltake/takes/1", {})
    assert status == 409 and "only take" in data["error"]  # the last one stays
    assert (d / "takes" / "1" / "audio.wav").is_file()
    status, _ = _send_json("DELETE", "/api/notes/2026-08-25-1640-deltake/takes/9", {})
    assert status == 404


def test_delete_a_note_moves_the_folder_and_drops_it_from_the_list(notes_dir):
    d = _make_session(notes_dir, "2026-08-25-1641-delnote", meta={"title": "Doomed"})
    status, data = _send_json("DELETE", "/api/notes/2026-08-25-1641-delnote", {})
    assert status == 200, data
    trashed = Path(data["trashed"])
    assert trashed == notes_dir / "trash" / "2026-08-25-1641-delnote"
    assert (trashed / "note.md").is_file() and (trashed / "audio.wav").is_file()
    assert not d.exists()

    status, listing = _get_json("/api/notes")
    assert listing["notes"] == []  # trash/ never matches the session regex
    status, _ = _get_json("/api/notes/2026-08-25-1641-delnote")
    assert status == 404
    status, _ = _send_json("DELETE", "/api/notes/2026-01-01-0000-missing", {})
    assert status == 404


def test_deletes_are_refused_while_a_session_is_recording_into_the_note(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1642-busy")
    _, data = _start_continue("2026-08-25-1642-busy")
    sid = data["session_id"]

    status, payload = _send_json("DELETE", "/api/notes/2026-08-25-1642-busy", {})
    assert status == 409 and "still going" in payload["error"]
    status, payload = _send_json("DELETE", "/api/notes/2026-08-25-1642-busy/takes/1", {})
    assert status == 409
    assert d.is_dir()

    _record_and_finish(sid, "continue=2026-08-25-1642-busy&how=append")
    status, payload = _send_json("DELETE", "/api/notes/2026-08-25-1642-busy", {})
    assert status == 200 and not d.exists()  # the session is gone: the delete goes through


def test_deletes_are_refused_cross_site(notes_dir):
    d = _make_session(notes_dir, "2026-08-25-1643-xsite", audio=None)
    status, _, _ = _request("DELETE", "/api/notes/2026-08-25-1643-xsite", None,
                            {"Origin": "https://evil.example"})
    assert status == 403 and d.is_dir()


def test_trash_reports_where_it_is_and_how_much_is_in_it(notes_dir, monkeypatch):
    status, data = _get_json("/api/trash")
    assert status == 200 and data == {"path": str(notes_dir / "trash"), "entries": 0}

    _make_session(notes_dir, "2026-08-25-1644-gone", audio=None)
    _send_json("DELETE", "/api/notes/2026-08-25-1644-gone", {})
    status, data = _get_json("/api/trash")
    assert data["entries"] == 1

    calls = _record_reveal(monkeypatch, wsl=False)
    monkeypatch.setattr(server.sys, "platform", "linux")
    status, data = _send_json("POST", "/api/trash/reveal", {})
    assert status == 200 and data["opened"] is True and data["entries"] == 1
    assert calls == [("popen", ("xdg-open", str(notes_dir / "trash")), server.subprocess.DEVNULL)]


def test_the_root_transcript_of_a_multi_take_note_cannot_be_edited(notes_dir):
    """It is derived from the takes: a write here would be undone by the next rebuild."""
    d = _continue_note(notes_dir, "2026-08-25-1650-derived")
    _, data = _start_continue("2026-08-25-1650-derived")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1650-derived&how=append")

    status, payload = _send_json("PUT", "/api/notes/2026-08-25-1650-derived/transcript",
                                 {"text": "rewritten by hand"})
    assert status == 409 and "edit a take" in payload["error"]
    assert (d / "transcript.txt").read_text(encoding="utf-8") == "first words\n\nfake transcript\n"
    assert not (d / "transcript.original.txt").exists()  # and no bogus "original" was minted


def test_a_stop_whose_note_vanished_saves_a_note_of_its_own(notes_dir):
    """The binding must not outlive the note: the recording is what matters."""
    d = _continue_note(notes_dir, "2026-08-25-1651-vanished")
    _, data = _start_continue("2026-08-25-1651-vanished")
    sid = data["session_id"]
    # the API refuses a delete while the session is bound, so this is the folder being
    # moved out from under the daemon by hand — the case the binding cannot recover from
    d.rename(notes_dir / "moved-away-by-hand")

    status, payload = _record_and_finish(sid, "how=append")
    assert status == 200, payload
    assert "take" not in payload and server._SESSION_RE.fullmatch(payload["name"])
    assert payload["name"] != "2026-08-25-1651-vanished"  # a note of its own, not a 404
    assert (notes_dir / payload["name"] / "note.md").is_file()


def test_a_takes_subset_is_deduplicated_and_ordered(notes_dir):
    d = _continue_note(notes_dir, "2026-08-25-1652-dupes")
    _, data = _start_continue("2026-08-25-1652-dupes")
    _record_and_finish(data["session_id"], "continue=2026-08-25-1652-dupes&how=append")

    status, data = _send_json("POST", "/api/notes/2026-08-25-1652-dupes/reclean",
                              {"mode": "light", "takes": [2, 1, 2]})
    assert status == 200, data
    assert _seen["clean"][0] == "first words\n\nfake transcript"  # each take once, in order
    assert _versions_of(d)[-1]["takes"] == [1, 2]


def test_cancel_with_keep_parks_the_recording_in_failed(notes_dir):
    """A page that drops a session it still holds a Retry for must not lose the audio."""
    d = _continue_note(notes_dir, "2026-08-25-1653-keep")
    _, data = _start_continue("2026-08-25-1653-keep")
    sid = data["session_id"]
    spill = server._registry.sessions[sid].pcm_path()
    _request("POST", f"/stream/append?sid={sid}", SPEECH_SAMPLE * 16_000,
             {"Content-Type": "application/octet-stream"})

    status, _, body = _request("POST", f"/stream/cancel?sid={sid}&keep=1")
    assert status == 200, body
    data = _json.loads(body)
    kept = Path(data["audio_kept"])
    assert data["cancelled"] is True
    assert kept.parent == notes_dir / "failed" and _wav_frames(kept.read_bytes()) == 16_000
    assert not spill.exists()  # the spill became that WAV, and only after it existed
    assert sid not in server._registry.sessions
    assert server._registry.bound("2026-08-25-1653-keep") is False  # the note is free again
    status, detail = _get_json("/api/notes/2026-08-25-1653-keep")
    assert detail["live"] is False and [t["n"] for t in detail["takes"]] == [1]
    assert not (d / "takes").exists()  # a cancel adds no take: the note is as it was

    status, _, _ = _request("POST", f"/stream/cancel?sid={sid}&keep=1")
    assert status == 404  # a second cancel is still a 404, not a 500


def test_cancel_with_keep_drops_a_recording_too_short_to_matter(notes_dir):
    _continue_note(notes_dir, "2026-08-25-1654-tiny")
    _, data = _start_continue("2026-08-25-1654-tiny")
    sid = data["session_id"]
    spill = server._registry.sessions[sid].pcm_path()
    _request("POST", f"/stream/append?sid={sid}", SPEECH_SAMPLE * 100,
             {"Content-Type": "application/octet-stream"})

    status, _, body = _request("POST", f"/stream/cancel?sid={sid}&keep=1")
    assert status == 200 and _json.loads(body)["audio_kept"] is None
    assert not spill.exists()
    failed = notes_dir / "failed"
    assert not failed.exists() or not any(failed.iterdir())
