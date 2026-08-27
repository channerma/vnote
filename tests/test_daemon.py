"""Tests for the daemon client and CLI routing (stub HTTP server; no models, no GPU)."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vnote import cli, config, daemon
from vnote.cleanup import CleanResult


def _closed_port() -> int:
    """A port number nothing is listening on (bound briefly, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubHandler(BaseHTTPRequestHandler):
    responses: dict = {}  # path -> (status_code, json_payload), set per test
    last_body: dict = {}  # path -> decoded JSON of the most recent POST

    def log_message(self, *args):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            self.last_body[self.path] = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            pass

    def _reply(self):
        code, payload = self.responses.get(self.path, (404, {"error": "not found"}))
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._read_body()
        self._reply()

    do_GET = _reply


@pytest.fixture
def stub_daemon(monkeypatch):
    """Serve canned JSON on an ephemeral port and point config.daemon_addr at it."""
    _StubHandler.responses = {}
    _StubHandler.last_body = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "daemon_addr", lambda: ("127.0.0.1", httpd.server_address[1]))
    yield _StubHandler
    httpd.shutdown()
    httpd.server_close()


# --- client ------------------------------------------------------------------


def test_is_up_false_when_nothing_listening(monkeypatch):
    monkeypatch.setattr(config, "daemon_addr", lambda: ("127.0.0.1", _closed_port()))
    assert daemon.is_up() is False


def test_client_round_trip(stub_daemon):
    stub_daemon.responses = {
        "/health": (200, {"status": "ok", "device": "cuda"}),
        "/transcribe": (200, {"transcript": "hello world", "meta": {"language": "en"}}),
        "/clean": (200, {"title": "A Title", "body": "The body."}),
    }
    assert daemon.is_up(timeout=5) is True  # generous: don't let a busy test box flake the probe
    text, meta = daemon.transcribe(Path("/some/audio.wav"), language="en")
    assert text == "hello world"
    assert meta == {"language": "en"}
    result = daemon.clean("hello world", mode="edit")
    assert isinstance(result, CleanResult)
    assert (result.title, result.body) == ("A Title", "The body.")


def test_http_error_body_raises_runtime_error(stub_daemon):
    stub_daemon.responses = {"/transcribe": (400, {"error": "no such file: /nope.wav"})}
    with pytest.raises(RuntimeError, match="no such file"):
        daemon.transcribe(Path("/nope.wav"))


def test_error_in_200_body_raises_runtime_error(stub_daemon):
    stub_daemon.responses = {"/clean": (200, {"error": "model exploded"})}
    with pytest.raises(RuntimeError, match="model exploded"):
        daemon.clean("some transcript")


def test_clean_sends_instructions_in_the_body(stub_daemon):
    stub_daemon.responses = {"/clean": (200, {"title": "T", "body": "B"})}
    daemon.clean("hello world", mode="edit", instructions="shorter")
    assert stub_daemon.last_body["/clean"]["instructions"] == "shorter"


def test_revise_round_trip(stub_daemon):
    stub_daemon.responses = {"/revise": (200, {"title": "Kept Title", "body": "Shorter body."})}
    result = daemon.revise("# Kept Title\n\nlong body", "make it shorter")
    assert isinstance(result, CleanResult)
    assert (result.title, result.body) == ("Kept Title", "Shorter body.")
    sent = stub_daemon.last_body["/revise"]
    assert sent["note"] == "# Kept Title\n\nlong body"
    assert sent["instructions"] == "make it shorter"
    assert sent["backend"] is None  # no explicit pick: the daemon resolves it (style, then setting)


# --- cli routing ---------------------------------------------------------------


def test_pipeline_prefers_daemon_when_up(monkeypatch):
    monkeypatch.setattr(daemon, "is_up", lambda: True)
    transcribe_fn, clean_fn = cli._pipeline(no_daemon=False)
    assert transcribe_fn is daemon.transcribe
    assert clean_fn is daemon.clean


def test_pipeline_falls_back_in_process(monkeypatch):
    from vnote.cleanup import clean
    from vnote.transcribe import transcribe

    monkeypatch.setattr(daemon, "is_up", lambda: False)
    transcribe_fn, clean_fn = cli._pipeline(no_daemon=False)
    assert transcribe_fn is transcribe
    assert clean_fn is clean


def test_no_daemon_forces_in_process_without_probing(monkeypatch):
    from vnote.cleanup import clean
    from vnote.transcribe import transcribe

    def _boom():
        raise AssertionError("is_up() must not be called with --no-daemon")

    monkeypatch.setattr(daemon, "is_up", _boom)
    transcribe_fn, clean_fn = cli._pipeline(no_daemon=True)
    assert transcribe_fn is transcribe
    assert clean_fn is clean
