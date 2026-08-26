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
    # No model is loaded in the test server, so /health reports what *would* load.
    assert h["whisper_model"] == config.whisper_model()
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


# --- flow history ----------------------------------------------------------------


def test_history_round_trip(live_server, tmp_path, monkeypatch):
    from vnote import history

    monkeypatch.setattr(history, "NOTES_DIR", tmp_path)
    daemon.log_history(wav=b"RIFFDATA", raw="um hello", clean="Hello.",
                       seconds=3.2, mode="dictation", tone="casual")
    md = next((tmp_path / "flow").glob("*.md")).read_text(encoding="utf-8")
    assert "**raw:** um hello" in md and "**clean:** Hello." in md
    wavs = list((tmp_path / "flow" / "audio").glob("*.wav"))
    assert len(wavs) == 1 and wavs[0].read_bytes() == b"RIFFDATA"
    assert f"[audio](audio/{wavs[0].name})" in md


def test_history_without_audio_writes_text_only(live_server, tmp_path, monkeypatch):
    from vnote import history

    monkeypatch.setattr(history, "NOTES_DIR", tmp_path)
    daemon.log_history(wav=None, raw="just text", clean=None, seconds=1.5)
    md = next((tmp_path / "flow").glob("*.md")).read_text(encoding="utf-8")
    assert "**raw:** just text" in md
    assert not (tmp_path / "flow" / "audio").exists()


def test_promote_round_trip(live_server, tmp_path, monkeypatch):
    from vnote import history, output

    monkeypatch.setattr(history, "NOTES_DIR", tmp_path)
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    daemon.log_history(wav=b"RIFFDATA", raw="promote me", clean="Promote me.",
                       seconds=2.0, mode="dictation", tone=None)
    name = daemon.promote("last")
    assert (tmp_path / name / "note.md").exists()
    assert (tmp_path / name / "audio.wav").read_bytes() == b"RIFFDATA"
    day = next((tmp_path / "flow").glob("*.md")).read_text(encoding="utf-8")
    assert f"[note](../{name}/note.md)" in day


def test_promote_empty_store_is_client_error(live_server, tmp_path, monkeypatch):
    from vnote import history, output

    monkeypatch.setattr(history, "NOTES_DIR", tmp_path)
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="no flow history"):
        daemon.promote("last")


def test_client_sends_an_absolute_path(monkeypatch, tmp_path):
    """The daemon opens the path in its own cwd — wherever `vnote --serve` was
    started. A relative path from anywhere else failed with a bare 'no such file'."""
    sent: dict = {}
    monkeypatch.setattr(
        daemon, "_post", lambda path, payload, timeout: sent.update(payload) or {"transcript": "", "meta": {}}
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "memo.wav").write_bytes(b"")

    daemon.transcribe(Path("memo.wav"))

    assert sent["audio_path"] == str((tmp_path / "memo.wav").resolve())
    assert Path(sent["audio_path"]).is_absolute()
