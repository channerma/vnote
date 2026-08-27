"""Tests for the shared capture loop, the keypress map and the terminal owner.

No mic and no real recorder: ``_capture`` only ever talks to the ``read_chunk``
callable it is handed, so a scripted list of chunks exercises pause/resume, EOF
and the console-status callback. ``_capture_session`` gets a real pty (POSIX
only) so the termios save/restore contract is actually checked, and ``_stop_proc``
gets a fake process that refuses to die.
"""

import io
import os
import signal
import subprocess
import sys
import threading
import time
import wave

import pytest

import vnote.record as record
from vnote.audio import BYTES_PER_S
from vnote.config import CHANNELS, SAMPLE_RATE
from vnote.record import _capture, _CaptureState, _handle_key, _key_action

try:
    import pty
    import termios
except ImportError:  # pragma: no cover - Windows
    pty = None
    termios = None

_needs_pty = pytest.mark.skipif(
    sys.platform == "win32" or pty is None, reason="pty/termios are POSIX-only"
)


def _scripted(state, script):
    """Build a ``read_chunk`` that applies an action, *then* returns its chunk.

    ``script`` is a list of ``(action, chunk)``; the action ("pause", "resume",
    "stop" or None) fires just before the chunk is handed to the loop, which is
    how a test stands in for a keypress arriving mid-stream.
    """
    calls = []
    it = iter(script)

    def read_chunk():
        action, chunk = next(it)
        calls.append(chunk)
        if action == "pause":
            state.paused.set()
        elif action == "resume":
            state.paused.clear()
        elif action == "stop":
            state.stop.set()
        return chunk

    return read_chunk, calls


def _wait(pred, what="condition"):
    """Block until a listener thread has observed a keypress (or give up)."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def test_capture_drops_chunks_read_while_paused():
    state = _CaptureState()
    read_chunk, _ = _scripted(state, [
        (None, b"aaaa"),
        ("pause", b"bbbb"),
        (None, b"cccc"),
        ("resume", b"dddd"),
        (None, None),
    ])
    assert _capture(read_chunk, state) == b"aaaa" + b"dddd"


def test_capture_stops_at_none():
    state = _CaptureState()
    read_chunk, calls = _scripted(state, [(None, b"aa"), (None, None), (None, b"never")])
    assert _capture(read_chunk, state) == b"aa"
    assert calls == [b"aa", None]  # nothing read past the end of the source


def test_capture_stops_after_current_chunk_when_stop_is_set():
    state = _CaptureState()
    read_chunk, calls = _scripted(state, [(None, b"aa"), ("stop", b"bb"), (None, b"never")])
    assert _capture(read_chunk, state) == b"aabb"
    assert calls == [b"aa", b"bb"]


def test_capture_ignores_empty_ticks():
    state = _CaptureState()
    read_chunk, calls = _scripted(state, [
        (None, b""),
        (None, b"aa"),
        (None, b""),
        (None, b"bb"),
        (None, None),
    ])
    assert _capture(read_chunk, state) == b"aabb"
    assert len(calls) == 5  # the empty ticks did not break the loop


def test_status_sees_growing_seconds_and_the_paused_span(monkeypatch):
    class _FakeTime:
        """Advance a full second per call so no redraw is throttled away."""

        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            self.now += 1.0
            return self.now

    monkeypatch.setattr(record, "time", _FakeTime())

    second = b"\x00" * BYTES_PER_S  # 32000 bytes == 1.0 s of 16 kHz mono s16le
    state = _CaptureState()
    read_chunk, _ = _scripted(state, [
        (None, second),
        ("pause", second),
        (None, second),
        ("resume", second),
        (None, None),
    ])

    seen = []
    assert record._capture(read_chunk, state, status=lambda s, p: seen.append((s, p))) == second * 2
    assert seen == [(1.0, False), (1.0, True), (1.0, True), (2.0, False)]


def test_key_action_map():
    assert _key_action(" ") == "pause"
    assert _key_action("\r") == "stop"
    assert _key_action("\n") == "stop"
    assert _key_action("q") == "stop"
    assert _key_action("x") is None


def test_handle_key_toggles_pause_and_stops():
    state = _CaptureState()

    _handle_key(" ", state)
    assert state.paused.is_set() and not state.stop.is_set()

    _handle_key(" ", state)  # double toggle == back to recording
    assert not state.paused.is_set()

    _handle_key("x", state)  # ignored keys change nothing
    assert not state.paused.is_set() and not state.stop.is_set()

    _handle_key("\r", state)
    assert state.stop.is_set()


def test_status_line_is_padded_so_a_shorter_line_leaves_no_residue(capsys):
    record._print_status(12.3, True)
    record._print_status(12.5, False)
    paused, running = capsys.readouterr().out.split("\r")[1:]
    assert len(paused) == len(running)  # the timer fully covers "⏸ paused ..."
    assert "\033" not in paused  # no \033[K: conhost prints it literally


# --- _capture_session against a real terminal -------------------------------


def _cleanup_pty(master, stdin):
    for thread in threading.enumerate():
        if thread.name == "vnote-keys":
            thread.join(timeout=5.0)
    stdin.close()  # closes the slave fd
    os.close(master)


@_needs_pty
def test_capture_session_pauses_and_stops_from_real_keypresses(monkeypatch):
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "rb", buffering=0)
    before = termios.tcgetattr(slave)
    monkeypatch.setattr(sys, "stdin", stdin)

    states = []
    real_state = record._CaptureState
    monkeypatch.setattr(record, "_CaptureState", lambda: states.append(real_state()) or states[-1])

    reads = []

    def read_chunk():
        i = len(reads)
        reads.append(i)
        state = states[0]
        if i == 0:
            # The session owns the terminal by now, so cbreak is already on.
            assert termios.tcgetattr(slave) != before
            return b"aaaa"
        if i == 1:
            os.write(master, b" ")
            _wait(state.paused.is_set, "pause")
            return b"bbbb"
        if i == 2:
            return b"cccc"  # still paused
        if i == 3:
            os.write(master, b" ")
            _wait(lambda: not state.paused.is_set(), "resume")
            return b"dddd"
        os.write(master, b"\n")
        _wait(state.stop.is_set, "stop")
        return b"eeee"

    try:
        pcm = record._capture_session(read_chunk)
        after = termios.tcgetattr(slave)  # while the slave fd is still open
    finally:
        _cleanup_pty(master, stdin)

    assert pcm == b"aaaa" + b"dddd" + b"eeee"  # the paused chunks were dropped
    assert after == before  # terminal handed back untouched


@_needs_pty
def test_capture_session_restores_the_terminal_when_capture_raises(monkeypatch):
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "rb", buffering=0)
    before = termios.tcgetattr(slave)
    monkeypatch.setattr(sys, "stdin", stdin)

    reads = []

    def read_chunk():
        reads.append(1)
        if len(reads) == 1:
            assert termios.tcgetattr(slave) != before  # cbreak is on
            return b"aa"
        raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            record._capture_session(read_chunk)
        after = termios.tcgetattr(slave)  # while the slave fd is still open
    finally:
        _cleanup_pty(master, stdin)

    assert after == before


# --- stopping the recorder --------------------------------------------------


class _StubbornProc:
    """A recorder that ignores SIGINT and SIGTERM: draining times out twice."""

    def __init__(self):
        self.calls = []
        self.returncode = None
        self.stdout = io.BytesIO(b"pcm")
        self.stderr = io.BytesIO(b"")
        self._timeouts = 2

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.calls.append("signal")

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("rec", timeout)

    def communicate(self, timeout=None):
        if self._timeouts:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired("rec", timeout)
        return b"", b""


def test_stop_proc_escalates_to_kill_and_never_raises(capsys):
    proc = _StubbornProc()
    record._stop_proc(proc)  # must not raise: the PCM still has to reach the WAV
    assert "kill" in proc.calls
    assert proc.stdout.closed and proc.stderr.closed
    assert capsys.readouterr().err == ""  # -9 is a death we asked for


class _DeadProc:
    """A recorder that already exited on its own, with a complaint on stderr."""

    returncode = 1

    def __init__(self):
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return b"", b"pa_context_connect() failed\nconnection refused\n"


def test_stop_proc_reports_a_recorder_that_failed_on_its_own(capsys):
    record._stop_proc(_DeadProc())
    err = capsys.readouterr().err
    assert "recorder exited with code 1" in err
    assert "connection refused" in err  # the last stderr line


# --- the single WAV write site ---------------------------------------------


def test_write_wav_round_trip(tmp_path):
    pcm = b"\x01\x02" * 1000
    dest = tmp_path / "nested" / "audio.wav"

    duration = record._write_wav(dest, pcm)

    assert duration == pytest.approx(len(pcm) / BYTES_PER_S)
    with wave.open(str(dest), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (CHANNELS, 2, SAMPLE_RATE)
        assert w.readframes(w.getnframes()) == pcm


# --- the pipe backend end to end (real child processes, real pty) ---------------------


def test_expected_codes_accept_the_sigint_family_even_when_we_did_not_signal():
    # A terminal Ctrl-C reaches the recorder before _stop_proc does, so "already exited on
    # SIGINT" must not read as a recorder failure.
    codes = record._expected_codes(signalled=False)
    assert {0, 130, 255, -signal.SIGINT} <= codes
    assert -signal.SIGTERM not in codes
    assert -signal.SIGTERM in record._expected_codes(signalled=True)


@_needs_pty
def test_record_via_pipe_silent_source_still_stops_on_enter(monkeypatch, tmp_path):
    # Regression: BufferedReader.read(4096) used to wait for a full 4096 bytes, so a recorder
    # that produced nothing hid Enter/Space until EOF. select + os.read must not.
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "rb", buffering=0)
    before = termios.tcgetattr(slave)
    monkeypatch.setattr(sys, "stdin", stdin)
    dest = tmp_path / "out.wav"

    def press_enter_soon():
        time.sleep(0.4)
        os.write(master, b"\n")

    threading.Thread(target=press_enter_soon, daemon=True).start()
    t0 = time.monotonic()
    try:
        seconds = record._record_via_pipe(["sleep", "30"], dest)  # a recorder that never yields audio
        after = termios.tcgetattr(slave)
    finally:
        _cleanup_pty(master, stdin)
    assert time.monotonic() - t0 < 5.0  # Enter was honoured while the pipe stayed silent
    assert seconds == 0.0 and dest.exists()
    assert after == before


@_needs_pty
def test_record_via_pipe_ends_at_source_eof_without_a_key(monkeypatch, tmp_path):
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "rb", buffering=0)
    monkeypatch.setattr(sys, "stdin", stdin)
    dest = tmp_path / "out.wav"
    try:
        seconds = record._record_via_pipe(["head", "-c", "8000", "/dev/zero"], dest)
    finally:
        _cleanup_pty(master, stdin)
    assert seconds == pytest.approx(8000 / BYTES_PER_S)
    with wave.open(str(dest), "rb") as w:
        assert w.getnframes() == 8000 // (2 * CHANNELS) and w.getframerate() == SAMPLE_RATE


# --- backend selection (the Mac/Homebrew-ffmpeg regression) ------------------


def _which_only(tool: str) -> object:
    """shutil.which stub: only ``tool`` exists (ffmpeg at the Homebrew path on a Mac)."""
    return lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" and tool == "ffmpeg" else None


def test_macos_never_selects_the_pulse_ffmpeg_backend(monkeypatch):
    """ffmpeg's -f pulse is Linux-only. With Homebrew ffmpeg on PATH and no
    parec/pw-record, macOS must fall through to sounddevice — selecting ffmpeg
    makes every recording abort at 0.2s with 'Nothing recorded (too short)'."""
    monkeypatch.setattr(record.sys, "platform", "darwin")
    monkeypatch.setattr(record.shutil, "which", _which_only("ffmpeg"))
    assert record.selected_backend() == "sounddevice"


def test_linux_still_uses_ffmpeg_when_no_pulse_cli_is_present(monkeypatch):
    monkeypatch.setattr(record.sys, "platform", "linux")
    monkeypatch.setattr(record.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    assert record.selected_backend() == "ffmpeg"


def test_parec_still_wins_on_wsl(monkeypatch):
    monkeypatch.setattr(record.sys, "platform", "linux")
    monkeypatch.setattr(record.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert record.selected_backend() == "parec"


def test_record_to_wav_uses_the_gated_selection(monkeypatch, tmp_path):
    # record_to_wav must respect the gate too: the helpers aren't dead code.
    monkeypatch.setattr(record.sys, "platform", "darwin")
    monkeypatch.setattr(record.shutil, "which", _which_only("ffmpeg"))
    calls: list[str] = []

    def recorded(dest):
        calls.append("sounddevice")

    monkeypatch.setattr(record, "_record_via_sounddevice", recorded)
    monkeypatch.setattr(record, "_record_via_pipe", lambda cmd, dest: calls.append("pipe"))
    record.record_to_wav(tmp_path / "x.wav")
    assert calls == ["sounddevice"]
