"""Microphone capture. Records 16 kHz mono until you press Enter.

WSL has no real ALSA device; audio comes in over WSLg's PulseAudio bridge. So we
prefer a PulseAudio/PipeWire CLI recorder (``parec`` / ``pw-record`` / ``ffmpeg``)
and fall back to the ``sounddevice`` library when a normal ALSA stack is present
(native Linux, or WSL with the ALSA→PulseAudio plugin configured).

Every backend produces raw s16le PCM chunks and funnels them through one shared
capture loop (:func:`_capture`), so pause/resume, the console timer and the WAV
write all live in a single place — and the loop is testable without a mic.

Terminal state has exactly one owner: :func:`_capture_session`, running on the
main thread, snapshots termios, switches to cbreak and restores it in a
``finally`` (plus an ``atexit`` belt and a SIGTERM handler). The key thread only
ever reads.
"""

from __future__ import annotations

import atexit
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .audio import BYTES_PER_S, wav_bytes
from .config import CHANNELS, SAMPLE_RATE

_INSTALL_HINT = (
    "No usable audio capture path found.\n"
    "    On WSL/Ubuntu the simplest fix is:  sudo apt install -y pulseaudio-utils\n"
    "    (that gives you `parec`, which records straight from WSLg's mic bridge).\n"
    "    Alternatively install `ffmpeg`, or set up the ALSA→PulseAudio plugin\n"
    "    (sudo apt install -y libasound2-plugins) so the `sounddevice` path works."
)

_DRAW_INTERVAL = 0.2  # seconds between console redraws (~5x/s)
_TICK = 0.2  # seconds a blocked read waits before handing control back
_STATUS_WIDTH = 24  # pad status lines so a shorter one can't leave residue

_UNSET = object()  # "no previous SIGTERM handler was saved"


# --- the shared capture loop -----------------------------------------------


class _CaptureState:
    """Shared between the key-listener thread and the capture loop."""

    def __init__(self) -> None:
        self.paused = threading.Event()
        self.stop = threading.Event()


def _capture(
    read_chunk: Callable[[], bytes | None],
    state: _CaptureState,
    *,
    status: Callable[[float, bool], None] | None = None,
) -> bytes:
    """Pull chunks until ``state.stop`` is set or ``read_chunk()`` returns None.

    ``read_chunk`` returns raw s16le PCM — possibly ``b""`` on a timeout tick, or
    ``None`` when the source ends. Chunks read while ``state.paused`` is set are
    discarded: the device keeps flowing, so resuming is gap-free and paused time
    never lands in the recording. Returns the kept PCM.

    ``status(recorded_seconds, paused)`` redraws the console line at most ~5x/s;
    None keeps the loop silent (tests).
    """
    kept = bytearray()
    last_draw = -1.0
    while not state.stop.is_set():
        chunk = read_chunk()
        if chunk is None:
            break
        if chunk and not state.paused.is_set():
            kept += chunk
        if status is not None:
            now = time.monotonic()
            if now - last_draw >= _DRAW_INTERVAL:
                last_draw = now
                status(len(kept) / BYTES_PER_S, state.paused.is_set())
    return bytes(kept)


def _print_status(recorded: float, paused: bool) -> None:
    # Fixed width: "12.5s" redrawn over "⏸ paused  12.3s" must not leave a tail.
    # (Padding, not \033[K — legacy Windows conhost prints the escape literally.)
    text = f"⏸ paused  {recorded:6.1f}s" if paused else f"{recorded:6.1f}s"
    print(f"\r  {text:<{_STATUS_WIDTH}}", end="", flush=True)


def _print_prompt(keys: bool) -> None:
    if keys:
        print("● Recording — Space to pause/resume, Enter to stop.")
    else:
        print("● Recording — press Enter to stop.")


# --- key handling ----------------------------------------------------------


def _key_action(ch: str) -> str | None:
    """Map one keypress to an action: "pause", "stop", or None (ignored)."""
    if ch == " ":
        return "pause"
    if ch in ("\r", "\n", "q"):
        return "stop"
    return None


def _handle_key(ch: str, state: _CaptureState) -> None:
    action = _key_action(ch)
    if action == "pause":
        if state.paused.is_set():
            state.paused.clear()
        else:
            state.paused.set()
    elif action == "stop":
        state.stop.set()


def _key_listener_posix(state: _CaptureState) -> None:
    """Read single keys from an already-cbreak terminal. Never touches termios."""
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError):
        state.stop.set()
        return
    while not state.stop.is_set():
        try:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            data = os.read(fd, 1)
        except (OSError, ValueError):  # stdin closed under us at shutdown
            return
        if not data:  # EOF
            state.stop.set()
            return
        _handle_key(data.decode("utf-8", "ignore"), state)


def _key_listener_windows(state: _CaptureState) -> None:
    import msvcrt

    while not state.stop.is_set():
        if msvcrt.kbhit():
            _handle_key(msvcrt.getwch(), state)
        else:
            time.sleep(0.05)


def _key_listener_readline(state: _CaptureState) -> None:
    """No per-key reads available: just wait for a line (or EOF)."""
    try:
        sys.stdin.readline()
    except (EOFError, ValueError, OSError):
        pass
    state.stop.set()


def _key_listener(state: _CaptureState, keys: bool) -> None:
    """Watch stdin for Space (pause/resume) and Enter/q (stop). Daemon thread."""
    if not keys:
        _key_listener_readline(state)
    elif sys.platform == "win32":
        _key_listener_windows(state)
    else:
        _key_listener_posix(state)


# --- terminal ownership (main thread only) ---------------------------------


def _termios_snapshot() -> tuple[int, list] | None:
    """Save stdin's termios settings, or None when there is nothing to save."""
    if sys.platform == "win32" or not sys.stdin.isatty():
        return None
    try:
        import termios

        fd = sys.stdin.fileno()
        return fd, termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001 - no tty, redirected stdin, ...
        return None


def _termios_restore(snapshot: tuple[int, list] | None) -> None:
    """Put a saved terminal mode back. Safe to call twice, and never raises."""
    if snapshot is None:
        return
    try:
        import termios

        fd, saved = snapshot
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except Exception:  # noqa: BLE001 - best effort; we must never mask the real error
        pass


def _set_cbreak(snapshot: tuple[int, list] | None) -> bool:
    """Switch stdin to cbreak so single keys arrive. True if it took."""
    if snapshot is None:
        return False
    try:
        import tty

        tty.setcbreak(snapshot[0])
    except Exception:  # noqa: BLE001 - odd tty; we fall back to line input
        return False
    return True


def _sigterm_exit(signum, frame) -> None:  # noqa: ANN001 - signal handler signature
    raise SystemExit(128 + signum)


def _install_sigterm():
    """Turn SIGTERM into SystemExit so `kill` still runs our finally blocks."""
    try:
        return signal.signal(signal.SIGTERM, _sigterm_exit)
    except (ValueError, OSError, AttributeError):
        return _UNSET  # not the main thread, or no SIGTERM on this platform


def _restore_sigterm(previous) -> None:
    if previous is _UNSET:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, TypeError):
        pass


def _capture_session(read_chunk: Callable[[], bytes | None]) -> bytes:
    """Run the shared loop with a key thread and the console timer attached.

    This is the only place that touches termios: it snapshots, switches to
    cbreak and restores, all on the main thread, so a Ctrl-C at any moment (or
    a SIGTERM, or an interpreter exit) leaves the shell the way it was found.
    """
    state = _CaptureState()
    snapshot = _termios_snapshot()
    restored = threading.Event()

    def restore() -> None:  # idempotent, never raises
        if not restored.is_set():
            _termios_restore(snapshot)  # restore first: a signal landing mid-call must not mark it done
            restored.set()

    atexit.register(restore)
    previous_sigterm = _install_sigterm()
    try:
        keys = _set_cbreak(snapshot) or (sys.platform == "win32" and sys.stdin.isatty())
        _print_prompt(keys)
        threading.Thread(target=_key_listener, args=(state, keys), name="vnote-keys", daemon=True).start()
        return _capture(read_chunk, state, status=_print_status)
    finally:
        state.stop.set()
        restore()
        atexit.unregister(restore)
        _restore_sigterm(previous_sigterm)
        print()


def _write_wav(dest: Path, pcm: bytes) -> float:
    """The one WAV-writing site. Returns the duration in seconds."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(wav_bytes(pcm))
    return len(pcm) / BYTES_PER_S


# --- backend: a CLI recorder that writes raw s16le PCM to stdout ------------


def _raw_pcm_cmd() -> list[str] | None:
    if sys.platform == "win32":  # no PulseAudio/PipeWire here
        return None
    if shutil.which("parec"):
        return ["parec", f"--rate={SAMPLE_RATE}", f"--channels={CHANNELS}", "--format=s16le", "--latency-msec=50"]
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS), "--format", "s16", "-"]
    return None


def _ffmpeg_cmd() -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "pulse", "-i", "default",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-",
    ]


def _safe(fn: Callable[[], object]) -> None:
    """Run a cleanup step; swallow whatever it throws."""
    try:
        fn()
    except Exception:  # noqa: BLE001 - cleanup must never lose the recording
        pass


def _drain(proc: subprocess.Popen, timeout: float) -> bytes | None:
    """Wait for the child while emptying its pipes. Returns stderr, or None if
    it is still alive when the timeout expires.

    Draining matters: a recorder that keeps writing after SIGINT blocks on a
    full 64 KB pipe and can never reach its own exit path.
    """
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:  # noqa: BLE001 - already-closed pipes, odd fakes
        return b""
    return err or b""


def _expected_codes(signalled: bool) -> set[int]:
    """Exit codes that mean "it stopped because we asked".

    The SIGINT family is always fine: a terminal Ctrl-C reaches the recorder (same process
    group) before we do, so it may already be gone when _stop_proc polls it.
    """
    codes = {0, 130, 255}  # 130 = shell "interrupted", 255 = ffmpeg on SIGINT
    sigint = getattr(signal, "SIGINT", None)
    if sigint is not None:
        codes.add(-int(sigint))
    if signalled:
        for name in ("SIGTERM", "SIGKILL"):
            sig = getattr(signal, name, None)
            if sig is not None:
                codes.add(-int(sig))
        codes.add(143)
    return codes


def _warn_if_failed(returncode: int | None, stderr: bytes, signalled: bool) -> None:
    """Surface a recorder that died on its own — otherwise a parec that cannot
    reach the mic looks exactly like "Nothing recorded (too short)"."""
    if returncode is None or returncode in _expected_codes(signalled):
        return
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    detail = lines[-1] if lines else "no stderr output"
    print(f"  (recorder exited with code {returncode}: {detail})", file=sys.stderr)


def _stop_proc(proc: subprocess.Popen) -> None:
    """Ask the recorder to finish, drain its pipes, and report a real failure.

    Runs in a ``finally`` around the capture, so it must never raise: anything
    escaping here would throw away PCM we have already collected.
    """
    signalled = proc.poll() is None
    if signalled:
        if sys.platform == "win32":
            _safe(proc.terminate)  # SIGINT is not deliverable to a live process
        else:
            _safe(lambda: proc.send_signal(signal.SIGINT))
    stderr = _drain(proc, 3.0)
    if stderr is None:
        _safe(proc.terminate)
        stderr = _drain(proc, 3.0)
    if stderr is None:
        _safe(proc.kill)
        stderr = _drain(proc, 3.0)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            _safe(pipe.close)
    _warn_if_failed(proc.returncode, stderr or b"", signalled)


def _record_via_pipe(cmd: list[str], dest: Path) -> float:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    fd = proc.stdout.fileno()

    def read_chunk() -> bytes | None:
        # Never block on the pipe: BufferedReader.read(4096) waits for a *full*
        # 4096 bytes, so a silent or missing source would hide Space/Enter until
        # EOF. select + os.read gives us the same b""-on-tick contract as the
        # sounddevice path, and caps key latency at one tick.
        try:
            if not select.select([fd], [], [], _TICK)[0]:
                return b""
            data = os.read(fd, 4096)
        except (OSError, ValueError):
            return None
        return data or None  # os.read returns b"" only at EOF

    try:
        pcm = _capture_session(read_chunk)
    finally:
        _stop_proc(proc)
    return _write_wav(dest, pcm)


# --- backend: the sounddevice library (real ALSA / native Linux) ------------


def _record_via_sounddevice(dest: Path) -> float:
    import queue

    import sounddevice as sd

    blocks: queue.Queue[bytes] = queue.Queue()

    def callback(indata, frames, time_info, status):  # noqa: ANN001 - sounddevice signature
        if status:
            print(f"  (audio warning: {status})", file=sys.stderr)
        blocks.put(indata.tobytes())

    def read_chunk() -> bytes | None:
        try:
            return blocks.get(timeout=_TICK)
        except queue.Empty:
            return b""

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=callback):
        pcm = _capture_session(read_chunk)
    return _write_wav(dest, pcm)


def _pulse_ffmpeg_available() -> bool:
    """Whether the ``-f pulse`` ffmpeg branch can work here.

    Gated to Linux on purpose. PulseAudio is a Linux/WSL thing; macOS would need
    ``-f avfoundation`` and Windows ``-f dshow``. Before this gate, Homebrew ffmpeg
    on a Mac won the backend race and died instantly with "Unknown input format:
    'pulse'", so every `vnote` recording aborted at 0.2s with "Nothing recorded
    (too short)" (verified 2026-08-26). sounddevice is the documented and working
    path on macOS/Windows, so gate this branch rather than grow per-platform flags.
    """
    return sys.platform.startswith("linux") and shutil.which("ffmpeg") is not None


def selected_backend() -> str | None:
    """Name of the capture backend :func:`record_to_wav` will use, or ``None``.

    Single source of truth so ``--doctor`` cannot claim a recorder that recording
    will not actually choose.
    """
    if shutil.which("parec"):
        return "parec"
    if shutil.which("pw-record"):
        return "pw-record"
    if _pulse_ffmpeg_available():
        return "ffmpeg"
    try:
        import sounddevice  # noqa: F401
    except Exception:  # noqa: BLE001 - import or PortAudio load failure
        return None
    return "sounddevice"


def record_to_wav(dest: Path) -> float:
    """Record from the default mic until Enter is pressed; write a 16 kHz mono WAV.

    Returns the recording duration in seconds.
    """
    raw_cmd = _raw_pcm_cmd()
    if raw_cmd is not None:
        return _record_via_pipe(raw_cmd, dest)
    if sys.platform != "win32" and shutil.which("ffmpeg"):
        return _record_via_pipe(_ffmpeg_cmd(), dest)
    try:
        import sounddevice  # noqa: F401
    except OSError as exc:
        raise RuntimeError(f"{exc}\n\n{_INSTALL_HINT}") from exc
    try:
        return _record_via_sounddevice(dest)
    except Exception as exc:  # noqa: BLE001 - PortAudio "no device" etc.
        raise RuntimeError(f"sounddevice capture failed: {exc}\n\n{_INSTALL_HINT}") from exc
