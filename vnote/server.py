"""vnote daemon: warm models behind a localhost HTTP API.

Run:  vnote --serve   (foreground; Ctrl-C to stop). Stdlib-only, same as the
Ollama client in cleanup.py. Single-user/localhost by design — no auth — and
inference is serialized behind a lock (one GPU; CTranslate2 models aren't
guaranteed concurrency-safe).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config
from .audio import BYTES_PER_S, wav_bytes

_infer_lock = threading.Lock()
_started = 0.0


def _warm() -> str:
    from . import transcribe  # heavy CUDA/model import stays inside the daemon

    transcribe._load_model()
    return transcribe._device or "cpu"


def _transcribe_pcm(pcm: bytes, language: str | None) -> tuple[str, dict]:
    """Transcribe raw s16le 16 kHz mono PCM (via a temp WAV; serialized on the lock)."""
    from .transcribe import transcribe

    fd, name = tempfile.mkstemp(prefix="vnote-stream-", suffix=".wav")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(wav_bytes(pcm))
        with _infer_lock:
            return transcribe(tmp, language=language)
    finally:
        tmp.unlink(missing_ok=True)


# --- streaming sessions (vnote-flow --stream) --------------------------------
#
# Chunked HTTP instead of WebSockets (stdlib has none): the client POSTs raw
# PCM chunks into a session; every >=0.5 s of new audio triggers a synchronous
# re-transcription of the whole buffer whose text is returned as the partial.
# Partials are best-effort; only /stream/finish must not fail.

_MIN_NEW_PCM = BYTES_PER_S // 2  # re-transcribe after >=0.5 s of new audio
_STREAM_TTL_S = 120.0  # drop sessions this long after their last chunk

_sessions: dict[str, _StreamSession] = {}
_sessions_lock = threading.Lock()


class _StreamSession:
    def __init__(self, language: str | None) -> None:
        self.language = language
        self.buf = bytearray()
        self.partial = ""
        self.last_seen = time.monotonic()
        self._transcribed = 0  # buffer length at the last partial pass

    def append(self, chunk: bytes) -> str:
        self.buf += chunk
        self.last_seen = time.monotonic()
        if len(self.buf) - self._transcribed >= _MIN_NEW_PCM:
            snapshot = bytes(self.buf)
            try:
                self.partial, _ = _transcribe_pcm(snapshot, self.language)
                self._transcribed = len(snapshot)
            except Exception:  # noqa: BLE001 - partials are best-effort
                pass
        return self.partial

    def finish(self) -> tuple[str, dict]:
        return _transcribe_pcm(bytes(self.buf), self.language)


def _sweep_sessions() -> None:
    cutoff = time.monotonic() - _STREAM_TTL_S
    with _sessions_lock:
        for sid in [s for s, sess in _sessions.items() if sess.last_seen < cutoff]:
            del _sessions[sid]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep the console quiet; we print our own lines
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_len(self) -> int:
        return int(self.headers.get("Content-Length", 0) or 0)

    def _read_json(self) -> dict:
        n = self._body_len()
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self) -> None:
        if self.path == "/health":
            from . import transcribe

            self._send(200, {
                "status": "ok",
                "version": __version__,
                "device": transcribe._device or "cpu",
                "whisper_model": transcribe._model_name or config.whisper_model(),
                "uptime_s": round(time.monotonic() - _started, 1),
            })
        else:
            self._send(404, {"error": "not found"})

    def _transcribe(self, audio: Path, language: str | None) -> dict:
        from .transcribe import transcribe

        with _infer_lock:
            text, meta = transcribe(audio, language=language)
        return {"transcript": text, "meta": meta}

    def _transcribe_body(self, query: dict[str, list[str]]) -> None:
        """Bytes mode: the request body is the audio itself (client machines don't
        share our filesystem). Written to a temp file for the duration of the call."""
        n = self._body_len()
        if n <= 0:
            return self._send(400, {"error": "empty audio body"})
        fmt = (query.get("format") or ["wav"])[0].lower()
        if not re.fullmatch(r"[a-z0-9]{1,8}", fmt):
            return self._send(400, {"error": f"bad format: {fmt!r}"})
        language = (query.get("language") or [None])[0]
        fd, name = tempfile.mkstemp(prefix="vnote-upload-", suffix=f".{fmt}")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.rfile.read(n))
            payload = self._transcribe(tmp, language)
        finally:
            # unlink BEFORE responding: the response releases the client, and
            # callers may observe (or assert) that the upload is already gone
            tmp.unlink(missing_ok=True)
        self._send(200, payload)

    def _stream_session(self, url) -> tuple[str, _StreamSession] | None:
        """Look up ?sid=...; sends the 404 itself when the session is unknown/expired."""
        _sweep_sessions()  # enforce the TTL on every touch, not only /stream/start
        sid = (parse_qs(url.query).get("sid") or [""])[0]
        with _sessions_lock:
            sess = _sessions.get(sid)
        if sess is None:
            self._send(404, {"error": f"unknown stream session: {sid!r}"})
            return None
        return sid, sess

    def do_POST(self) -> None:
        try:
            url = urlparse(self.path)
            if url.path == "/transcribe":
                ctype = (self.headers.get("Content-Type") or "").partition(";")[0].strip().lower()
                if ctype == "application/octet-stream" or ctype.startswith("audio/"):
                    return self._transcribe_body(parse_qs(url.query))
                data = self._read_json()
                audio = Path(data["audio_path"]).expanduser()
                if not audio.is_file():
                    return self._send(400, {"error": f"no such file: {audio}"})
                self._send(200, self._transcribe(audio, data.get("language")))
            elif url.path == "/clean":
                data = self._read_json()
                from .cleanup import clean

                result = clean(
                    data["transcript"],
                    mode=data.get("mode", "edit"),
                    backend=data.get("backend", "ollama"),
                    model=data.get("model"),
                    tone=data.get("tone"),
                )
                self._send(200, {"title": result.title, "body": result.body})
            elif url.path == "/history":
                data = self._read_json()
                from . import history

                history.append_take(
                    raw=data.get("raw"),
                    clean=data.get("clean"),
                    wav=base64.b64decode(data["wav_b64"]) if data.get("wav_b64") else None,
                    seconds=float(data.get("seconds") or 0.0),
                    mode=data.get("mode"),
                    tone=data.get("tone"),
                )
                self._send(200, {"saved": True})
            elif url.path == "/promote":
                data = self._read_json()
                from . import history

                try:
                    session = history.promote_take(data.get("take") or "last")
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                self._send(200, {"note": session.name})
            elif url.path == "/stream/start":
                data = self._read_json()
                _sweep_sessions()
                sid = uuid.uuid4().hex
                with _sessions_lock:
                    _sessions[sid] = _StreamSession(data.get("language"))
                self._send(200, {"session_id": sid})
            elif url.path == "/stream/append":
                found = self._stream_session(url)
                if found is None:
                    return
                n = self._body_len()
                self._send(200, {"partial": found[1].append(self.rfile.read(n) if n else b"")})
            elif url.path == "/stream/finish":
                found = self._stream_session(url)
                if found is None:
                    return
                sid, sess = found
                with _sessions_lock:
                    _sessions.pop(sid, None)
                if not sess.buf:
                    return self._send(400, {"error": "no audio received"})
                text, meta = sess.finish()
                self._send(200, {"transcript": text, "meta": meta})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def serve() -> int:
    global _started
    host, port = config.daemon_addr()
    try:
        httpd = ThreadingHTTPServer((host, port), _Handler)  # bind first: fail fast if the port is taken
    except OSError as exc:
        print(f"error: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        print("       (is another `vnote --serve` already running?)", file=sys.stderr)
        return 1
    _started = time.monotonic()
    pinned = config.whisper_model_override()
    print(f"vnote daemon — warming {pinned or 'whisper (model follows the device)'} ...", flush=True)
    device = _warm()
    from . import transcribe

    print(
        f"  warm: {transcribe._model_name} on {device}; "
        f"listening on http://{host}:{port}  (Ctrl-C to stop)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
    finally:
        httpd.server_close()
    return 0
