"""vnote daemon: warm models and the web UI behind a localhost HTTP API.

Run:  vnote --serve   (foreground; Ctrl-C to stop). Stdlib-only, same as the
Ollama client in cleanup.py. Single-user/localhost by design — no auth — and
inference is serialized behind a lock (one GPU; CTranslate2 models aren't
guaranteed concurrency-safe).

Routes: ``GET /`` and ``/static/*`` serve the page in ``vnote/web/``; ``/api/*``
is what that page talks to (docs/planning/PHASE8.md has the contract, PHASE9.md
the editing/revise/versions/reveal additions);
``/transcribe``, ``/clean``, ``/revise`` and ``/stream/*`` are the per-step
endpoints the CLI uses when a daemon is up.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, output, stream, versions
from .audio import BYTES_PER_S, wav_bytes

_infer_lock = threading.Lock()
_started = 0.0


class _BadRequest(ValueError):
    """A malformed request body/header; answered with 400 instead of 500."""


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


# --- streaming sessions ------------------------------------------------------
#
# Chunked HTTP instead of WebSockets (stdlib has none): the client POSTs raw PCM
# chunks into a session. The incremental model lives in stream.py — committed
# segments plus an uncommitted tail, transcribed on a per-session worker so the
# request never waits on the GPU. Partials are best-effort; only /stream/finish
# must not fail.

_STREAM_TTL_S = 1800.0  # drop sessions this long after their last touch — a long pause must survive
#                        (30 min; /stream/ping keeps a paused session alive without sending audio)
_MIN_KEEP_PCM = BYTES_PER_S // 2  # an abandoned session shorter than this is not worth saving


def _speech_spans(pcm: bytes) -> list[tuple[float, float]]:
    """VAD for the live worker; the import drags in onnxruntime, so it stays lazy."""
    from . import vad

    return vad.speech_spans(pcm)


def _keep_live_audio(pcm: bytes, sid: str) -> Path | None:
    """Park a live recording in ``failed/`` as a WAV — the daemon holds the only copy.

    The session id is in the name because a batch sweep can save several sessions
    inside the same second, and a one-second stamp alone would overwrite them.
    """
    try:
        dest_dir = _notes_dir() / "failed"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"live-{datetime.now():%Y%m%d-%H%M%S}-{sid[:8]}.wav"
        dest.write_bytes(wav_bytes(pcm))
        return dest
    except OSError:
        return None


def _save_abandoned(session: stream.LiveSession) -> None:
    """A session that expired mid-recording still holds the only copy of that audio."""
    try:
        pcm = session.pcm_path().read_bytes()
        if len(pcm) < _MIN_KEEP_PCM:
            return
        dest = _keep_live_audio(pcm, session.sid)
        if dest is not None:
            print(f"  live session {session.sid} expired; its audio is in {dest}", flush=True)
    except Exception:  # noqa: BLE001 - the sweep must never fail on a broken temp file
        pass


def _sweep_stale_spills() -> None:
    """Drop ``vnote-live-*.pcm`` files a previous daemon left behind when it was killed."""
    cutoff = time.time() - 3600.0
    removed = 0
    try:
        for path in Path(tempfile.gettempdir()).glob("vnote-live-*.pcm"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:  # an unreadable temp dir is not worth a failed start
        return
    if removed:
        print(f"  removed {removed} stale live-recording spill file(s)", flush=True)


_registry = stream.Registry(ttl_s=_STREAM_TTL_S, transcribe_pcm=_transcribe_pcm,
                            vad=_speech_spans, on_expire=_save_abandoned)


# --- the web UI: static files + note folders ---------------------------------

_WEB_DIR = Path(__file__).resolve().parent / "web"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
_AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
}
_STATIC_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_SESSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9-]+")  # what output.make_session_dir produces
_FORMAT_RE = re.compile(r"[a-z0-9]{1,8}")
_FILE_CHUNK = 64 * 1024


def _notes_dir() -> Path:
    return output.NOTES_DIR  # bound in output.py; tests monkeypatch it there


def _session_path(name: str) -> Path | None:
    """The session folder for a URL name, or None — never anything outside NOTES_DIR."""
    if not _SESSION_RE.fullmatch(name):
        return None
    root = _notes_dir().resolve()
    try:
        path = (root / name).resolve()
    except OSError:
        return None
    if path.parent != root or not path.is_dir():
        return None
    return path


def _audio_file(session: Path) -> Path | None:
    for p in sorted(session.glob("audio.*")):
        if p.is_file() and p.suffix.lower() in _AUDIO_TYPES:
            return p
    return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_meta(session: Path) -> dict:
    try:
        data = json.loads((session / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stamp_to_iso(name: str) -> str | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})", name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:00" if m else None


def _summary(session: Path) -> dict:
    """The list-item view of one session folder; every field tolerates a missing meta.json."""
    meta = _read_meta(session)
    duration = meta.get("audio_duration_s")
    if duration is None:
        duration = meta.get("recording_duration_s")
    if duration is None:
        duration = meta.get("seconds")
    return {
        "name": session.name,
        "title": meta.get("title") or session.name,
        "created": meta.get("created") or _stamp_to_iso(session.name),
        "duration_s": duration,
        "mode": meta.get("cleanup_mode") or meta.get("mode"),
        "backend": meta.get("cleanup_backend"),
        "has_audio": _audio_file(session) is not None,
        "has_note": (session / "note.md").is_file(),
    }


def _list_notes() -> list[dict]:
    root = _notes_dir()
    if not root.is_dir():
        return []
    dirs = [d for d in root.iterdir() if d.is_dir() and _SESSION_RE.fullmatch(d.name)]
    return [_summary(d) for d in sorted(dirs, key=lambda d: d.name, reverse=True)]  # stamp-first names sort by time


def _note_detail(session: Path) -> dict:
    try:
        versions.ensure_history(session)  # a 0.5.0 folder shows "v1 · original" the first time it is opened
    except (OSError, ValueError):
        pass  # best-effort: a broken meta.json or an unwritable folder must not fail the read
    audio = _audio_file(session)
    return {
        **_summary(session),
        "meta": _read_meta(session),
        "note": _read_text(session / "note.md"),
        "transcript": _read_text(session / "transcript.txt") or "",
        "audio_url": f"/api/notes/{session.name}/audio" if audio else None,
        "path": str(session),  # the page shows it (with a Copy button) next to the reveal action
        "versions": versions.entries(session),
    }


def _reveal(path: Path) -> bool:
    """Open a note's folder in the desktop file manager. Best-effort: never raises, never blocks.

    Under WSL the folder belongs in *Windows* Explorer, reached through its Windows
    path; explorer.exe returns a meaningless exit code and may outlive us, so it is
    fired and forgotten (same shape as ``_open_browser``).
    """
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if _is_wsl():
            done = subprocess.run(["wslpath", "-w", str(path)], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=5)
            winpath = (done.stdout or "").strip()
            if not winpath:
                return False
            subprocess.Popen(["explorer.exe", winpath], **quiet)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], **quiet)
        elif os.name == "nt":
            subprocess.Popen(["explorer", str(path)], **quiet)
        else:
            subprocess.Popen(["xdg-open", str(path)], **quiet)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _preserve_upload(tmp: Path) -> Path | None:
    """Keep a recording whose note could not be written — it is the only copy the user has."""
    try:
        dest_dir = _notes_dir() / "failed"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{datetime.now():%Y%m%d-%H%M%S}{tmp.suffix}"
        shutil.move(str(tmp), dest)
        return dest
    except OSError:
        return None


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _one(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    return values[0] if values and values[0] != "" else default


def _note_options(query: dict[str, list[str]]) -> tuple[str, str, str | None, bool]:
    """``(mode, backend, model, raw)`` from the query params /api/note and /stream/finish share."""
    raw = (_one(query, "raw") or "0").lower() in ("1", "true", "yes")
    if raw:  # no LLM runs, so a bad saved default mode/backend must not block a raw recording
        mode, backend = "edit", config.BUILTIN_BACKEND
    else:
        mode = _one(query, "mode") or config.default_mode()
        if mode not in config.MODES:
            raise _BadRequest(f"bad mode: {mode!r} (one of {', '.join(config.MODES)})")
        backend = _one(query, "backend") or config.backend()
        if backend not in config.setting("backend").choices:
            raise _BadRequest(f"bad backend: {backend!r}")
    return mode, backend, _one(query, "model"), raw


def _note_from_audio(tmp: Path, *, mode: str, backend: str, model: str | None, language: str | None,
                     raw: bool, source: str, extra: dict) -> tuple[int, dict]:
    """``tmp`` (an audio file we own) → a finished note folder → ``(status, payload)``.

    Shared by /api/note and /stream/finish?note=1. The reply is decided here and sent
    by the caller *after* ``tmp`` is gone: the response releases the client, which may
    observe (or assert) that the upload was removed. ``tmp`` never survives this call —
    it is either consumed, moved into ``failed/`` or unlinked.
    """
    from . import cleanup, pipeline, transcribe

    def locked_transcribe(path: Path, language: str | None = None):
        with _infer_lock:  # guards the GPU model only — an LLM round-trip must not block /transcribe
            return transcribe.transcribe(path, language=language)

    try:
        try:
            result = pipeline.make_note(
                tmp, transcribe_fn=locked_transcribe, clean_fn=cleanup.clean,
                mode=mode, backend=backend, model=model, language=language, raw=raw, source=source,
            )
        except pipeline.EmptyTranscriptError:
            return 400, {"error": "no speech detected"}
        except Exception as exc:  # noqa: BLE001 - the upload is the only copy of the recording: keep it
            kept = _preserve_upload(tmp)
            return 500, {"error": f"{type(exc).__name__}: {exc}", "audio_kept": str(kept) if kept else None}
        return 200, {
            "name": result.session_dir.name,
            "title": result.title,
            "note": result.note_text,
            "transcript": result.transcript,
            "meta": result.meta,
            "cleanup_error": result.cleanup_error,
            **extra,
        }
    finally:
        tmp.unlink(missing_ok=True)  # the session folder (or failed/) holds its own copy now


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep the console quiet; we print our own lines
        pass

    # --- response helpers ---

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        """Send a file; honours a single ``Range: bytes=a-b`` so ``<audio>`` can seek."""
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix form: the last N bytes
                start = max(size - int(m.group(2)), 0)
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = max(end - start + 1, 0)
        f = path.open("rb")  # open BEFORE any header goes out, so a failure is still a clean 500
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_FILE_CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except OSError:
                    return  # the player seeked away (broken pipe / reset); nothing to report
                remaining -= len(chunk)

    def _cross_site(self) -> bool:
        """True for a browser request from another origin (or a rebound Host): such a request can't
        read our reply, but a cross-site form post could still rewrite a note. Non-browser clients
        send no Origin and pass."""
        allowed = _LOCAL_HOSTS | {config.DAEMON_HOST}
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host and host not in allowed:
            return True
        origin = self.headers.get("Origin")
        return bool(origin) and (urlparse(origin).hostname or "") not in allowed

    def _send_static(self, name: str) -> None:
        if not _STATIC_NAME_RE.fullmatch(name) or ".." in name:
            return self._send(404, {"error": "not found"})
        path = _WEB_DIR / name
        ctype = _STATIC_TYPES.get(path.suffix.lower())
        if ctype is None or not path.is_file():
            return self._send(404, {"error": "not found"})
        self._send_file(path, ctype)

    def _body_len(self) -> int:
        try:
            return int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise _BadRequest("bad Content-Length header") from None

    def _read_json(self) -> dict:
        n = self._body_len()
        try:
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            raise _BadRequest("request body is not valid JSON") from None
        return data if isinstance(data, dict) else {}

    # --- GET ---

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send_static("index.html")
            if path.startswith("/static/"):
                return self._send_static(path[len("/static/"):])
            if path == "/health":
                from . import transcribe

                return self._send(200, {
                    "status": "ok",
                    "version": __version__,
                    "device": transcribe._device or "cpu",
                    "whisper_model": config.WHISPER_MODEL,
                    "uptime_s": round(time.monotonic() - _started, 1),
                })
            if path == "/api/settings":
                return self._send(200, {"settings": config.describe()})
            if path == "/api/vocab":
                return self._send(200, {"text": _read_text(config.vocab_file()) or ""})
            if path == "/api/notes":
                return self._send(200, {"notes": _list_notes()})
            m = re.fullmatch(r"/api/notes/([^/]+)/versions/(\d+)", path)
            if m:
                session = _session_path(m.group(1))
                if session is None:
                    return self._send(404, {"error": f"no such note: {m.group(1)}"})
                n = int(m.group(2))
                try:
                    versions.ensure_history(session)  # best-effort, exactly as in _note_detail
                except (OSError, ValueError):
                    pass
                try:
                    text = versions.read(session, n)
                except ValueError as exc:
                    return self._send(404, {"error": str(exc)})
                entry = next((e for e in versions.entries(session) if e.get("n") == n), {})
                return self._send(200, {**entry, "n": n, "text": text})
            m = re.fullmatch(r"/api/notes/([^/]+)(/audio)?", path)
            if m:
                session = _session_path(m.group(1))
                if session is None:
                    return self._send(404, {"error": f"no such note: {m.group(1)}"})
                if not m.group(2):
                    return self._send(200, _note_detail(session))
                audio = _audio_file(session)
                if audio is None:
                    return self._send(404, {"error": "this note has no audio"})
                return self._send_file(audio, _AUDIO_TYPES[audio.suffix.lower()])
            self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # --- PUT (settings + vocabulary) ---

    def do_PUT(self) -> None:
        if self._cross_site():
            return self._send(403, {"error": "cross-site request refused"})
        try:
            path = urlparse(self.path).path
            if path == "/api/settings":
                try:
                    saved = config.update(self._read_json())
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"saved": saved})
            m = re.fullmatch(r"/api/notes/([^/]+)/note", path)
            if m:
                return self._api_save_note(m.group(1))
            if path == "/api/vocab":
                text = self._read_json().get("text")
                if not isinstance(text, str):
                    return self._send(400, {"error": "body must be {\"text\": \"...\"}"})
                target = config.vocab_file()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text if not text or text.endswith("\n") else text + "\n", encoding="utf-8")
                return self._send(200, {"saved": True})
            self._send(404, {"error": "not found"})
        except _BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # --- POST ---

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
        if not _FORMAT_RE.fullmatch(fmt):
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

    def _api_note(self, query: dict[str, list[str]]) -> None:
        """The web recorder's one call: audio bytes in, a finished note folder out."""
        n = self._body_len()
        if n <= 0:
            return self._send(400, {"error": "empty audio body"})
        fmt = (_one(query, "format") or "webm").lower()
        if not _FORMAT_RE.fullmatch(fmt):
            return self._send(400, {"error": f"bad format: {fmt!r}"})
        mode, backend, model, raw = _note_options(query)
        language = _one(query, "language") or config.language()
        if language and language.lower() == "auto":  # the page's way to override a saved language
            language = None

        fd, name = tempfile.mkstemp(prefix="vnote-web-", suffix=f".{fmt}")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.rfile.read(n))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self._send(*_note_from_audio(tmp, mode=mode, backend=backend, model=model, language=language,
                                     raw=raw, source="web", extra={}))

    def _api_save_note(self, name: str) -> None:
        """PUT the note text the user edited in the page — a new version (op ``edit``)."""
        session = _session_path(name)
        if session is None:
            return self._send(404, {"error": f"no such note: {name}"})
        text = self._read_json().get("text")
        if not isinstance(text, str) or not text.strip():
            return self._send(400, {"error": "body must be {\"text\": \"...\"} with non-empty text"})
        from . import pipeline

        result = pipeline.save_edit(session, text)
        self._send(200, {"version": result.version, "title": result.title, "note": result.note_text})

    def _api_revise(self, name: str) -> None:
        """Rewrite the *current* note per a free-text instruction — a new version (op ``revise``)."""
        session = _session_path(name)
        if session is None:
            return self._send(404, {"error": f"no such note: {name}"})
        data = self._read_json()
        backend = data.get("backend") or config.backend()
        if backend not in config.setting("backend").choices:
            return self._send(400, {"error": f"bad backend: {backend!r}"})
        instructions = data.get("instructions")
        from . import cleanup, pipeline

        try:
            result = pipeline.revise(session, revise_fn=cleanup.revise,
                                     instructions=instructions if isinstance(instructions, str) else "",
                                     backend=backend, model=data.get("model"))
        except ValueError as exc:  # blank instructions, or the note was never cleaned
            return self._send(400, {"error": str(exc)})
        self._send(200, {"title": result.title, "note": result.note_text, "version": result.version})

    def _api_restore(self, name: str) -> None:
        session = _session_path(name)
        if session is None:
            return self._send(404, {"error": f"no such note: {name}"})
        n = self._read_json().get("n")
        if not isinstance(n, int) or isinstance(n, bool):
            return self._send(400, {"error": "body must be {\"n\": <version number>}"})
        from . import pipeline

        try:
            result = pipeline.restore(session, n)
        except ValueError as exc:
            return self._send(404, {"error": str(exc)})
        self._send(200, {"title": result.title, "note": result.note_text, "version": result.version})

    def _api_reveal(self, name: str) -> None:
        session = _session_path(name)
        if session is None:
            return self._send(404, {"error": f"no such note: {name}"})
        self._send(200, {"opened": _reveal(session), "path": str(session)})

    def _api_reclean(self, name: str) -> None:
        session = _session_path(name)
        if session is None:
            return self._send(404, {"error": f"no such note: {name}"})
        data = self._read_json()
        mode = data.get("mode") or config.default_mode()
        if mode not in config.MODES:
            return self._send(400, {"error": f"bad mode: {mode!r} (one of {', '.join(config.MODES)})"})
        backend = data.get("backend") or config.backend()
        if backend not in config.setting("backend").choices:
            return self._send(400, {"error": f"bad backend: {backend!r}"})
        from . import cleanup, pipeline

        try:
            result = pipeline.reclean(session, clean_fn=cleanup.clean, mode=mode, backend=backend,
                                      model=data.get("model"), instructions=data.get("instructions"))
        except FileNotFoundError as exc:
            return self._send(404, {"error": str(exc)})
        except pipeline.EmptyTranscriptError:
            return self._send(400, {"error": "transcript is empty"})
        self._send(200, {"title": result.title, "note": result.note_text, "version": result.version})

    def _stream_session(self, url) -> tuple[str, stream.LiveSession] | None:
        """Look up ?sid=...; sends the 404 itself when the session is unknown/expired."""
        sid = (parse_qs(url.query).get("sid") or [""])[0]
        sess = _registry.get(sid)  # sweeps first: the TTL is enforced on every touch
        if sess is None:
            self._send(404, {"error": f"unknown stream session: {sid!r}"})
            return None
        return sid, sess

    def _stream_finish(self, url) -> None:
        """End a live session: the daemon-held PCM is the audio, live text the fallback.

        Stop is the one request that must never lose a recording, so the order is:
        validate everything *while the session is still alive* (a 400 leaves it running
        and the user can just press Stop again), then take it out of the registry, then
        keep the audio — in the note folder on success, in ``failed/`` on anything else.
        """
        found = self._stream_session(url)
        if found is None:
            return
        sid, sess = found
        query = parse_qs(url.query)
        want_note = (_one(query, "note") or "0").lower() in ("1", "true", "yes")
        mode = backend = model = None
        raw = False
        if want_note:
            mode, backend, model, raw = _note_options(query)  # a bad mode/backend: 400, session intact
        language = _one(query, "language") or sess.language or config.language()
        if language and language.lower() == "auto":
            language = None
        if _registry.pop(sid) is None:  # a second Stop (or the TTL sweep) got here first
            return self._send(404, {"error": f"unknown stream session: {sid!r}"})
        pcm_path = sess.close(keep_audio=True)
        live = sess.live_transcript()  # after close(): the worker's last commit is in it
        pcm = pcm_path.read_bytes() if pcm_path is not None else b""
        if not pcm:
            if pcm_path is not None:
                pcm_path.unlink(missing_ok=True)
            return self._send(400, {"error": "no audio received"})
        try:
            if want_note:
                fd, name = tempfile.mkstemp(prefix="vnote-live-", suffix=".wav")
                tmp = Path(name)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(wav_bytes(pcm))
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
                status, payload = _note_from_audio(tmp, mode=mode, backend=backend, model=model,
                                                   language=language, raw=raw, source="web-live",
                                                   extra={"live_transcript": live})
            else:
                text, meta = _transcribe_pcm(pcm, language)
                status, payload = 200, {"transcript": text, "meta": meta, "live_transcript": live}
        except Exception as exc:  # noqa: BLE001 - the recording outlives every failure below
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}", "live_transcript": live}
        if status == 200:
            pcm_path.unlink(missing_ok=True)  # the note folder (or the client) has the audio now
        else:
            kept = payload.get("audio_kept") or _keep_live_audio(pcm, sid)
            if kept:
                payload["audio_kept"] = str(kept)
                pcm_path.unlink(missing_ok=True)  # failed/ holds the copy
            else:  # nothing could be written there: the spill *is* the recording, so leave it
                payload["audio_kept"] = str(pcm_path)
        self._send(status, payload)

    def do_POST(self) -> None:
        if self._cross_site():
            return self._send(403, {"error": "cross-site request refused"})
        try:
            url = urlparse(self.path)
            note_route = re.fullmatch(r"/api/notes/([^/]+)/(reclean|revise|restore|reveal)", url.path)
            if url.path == "/api/note":
                return self._api_note(parse_qs(url.query))
            elif note_route:
                handler = {"reclean": self._api_reclean, "revise": self._api_revise,
                           "restore": self._api_restore, "reveal": self._api_reveal}[note_route.group(2)]
                return handler(note_route.group(1))
            elif url.path == "/transcribe":
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
                    instructions=data.get("instructions"),
                )
                self._send(200, {"title": result.title, "body": result.body})
            elif url.path == "/revise":
                data = self._read_json()
                note = data.get("note")
                instructions = data.get("instructions")
                if not isinstance(note, str) or not note.strip():
                    raise _BadRequest("note is empty")
                if not isinstance(instructions, str) or not instructions.strip():
                    raise _BadRequest("instructions are empty")
                from .cleanup import revise

                result = revise(
                    note,
                    instructions,
                    backend=data.get("backend", "ollama"),
                    model=data.get("model"),
                )
                self._send(200, {"title": result.title, "body": result.body})
            elif url.path == "/stream/start":
                data = self._read_json()
                language = str(data.get("language") or "").strip()
                if not language or language.lower() == "auto":  # same rule as finish and /api/note
                    language = None
                self._send(200, {"session_id": _registry.start(language).sid})
            elif url.path == "/stream/ping":
                found = self._stream_session(url)
                if found is None:
                    return
                found[1].ping()  # a paused recorder keeps its session alive
                self._send(200, {"ok": True})
            elif url.path == "/stream/append":
                found = self._stream_session(url)
                if found is None:
                    return
                n = self._body_len()
                self._send(200, found[1].append(self.rfile.read(n) if n else b""))
            elif url.path == "/stream/cancel":
                sid = (parse_qs(url.query).get("sid") or [""])[0]
                sess = _registry.pop(sid)
                if sess is None:
                    return self._send(404, {"error": f"unknown stream session: {sid!r}"})
                sess.close(keep_audio=False)  # the user backed out: drop the session and its audio
                self._send(200, {"cancelled": True})
            elif url.path == "/stream/finish":
                self._stream_finish(url)
            else:
                self._send(404, {"error": "not found"})
        except _BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


# --- process entry point -----------------------------------------------------


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _open_browser(url: str) -> None:
    """Best-effort, never fatal. Under WSL the page belongs in the *Windows* browser, so
    go through cmd.exe first — Python's webbrowser would pick a Linux (or text-mode)
    browser inside WSL, which is never what you want."""
    if _is_wsl():
        for exe in ("cmd.exe", "/mnt/c/Windows/System32/cmd.exe"):
            try:
                subprocess.Popen([exe, "/c", "start", "", url], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    try:
        if webbrowser.open(url):
            return
    except Exception:  # noqa: BLE001
        pass
    print(f"  (could not open a browser here — visit {url})", flush=True)


def serve(open_browser: bool = False) -> int:
    global _started
    host, port = config.daemon_addr()
    try:
        httpd = ThreadingHTTPServer((host, port), _Handler)  # bind first: fail fast if the port is taken
    except OSError as exc:
        print(f"error: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        print("       (is another `vnote --serve` already running?)", file=sys.stderr)
        return 1
    _started = time.monotonic()
    url = f"http://{host}:{port}"
    _sweep_stale_spills()  # a killed daemon leaves its live spills behind; nobody else will
    print(f"vnote daemon — warming {config.WHISPER_MODEL} ...", flush=True)
    device = _warm()
    print(f"  warm on {device}; web UI + API at {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
    finally:
        httpd.server_close()
    return 0
