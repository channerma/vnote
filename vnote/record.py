"""Microphone capture. Records 16 kHz mono until you press Enter.

WSL has no real ALSA device; audio comes in over WSLg's PulseAudio bridge. So we
prefer a PulseAudio/PipeWire CLI recorder (``parec`` / ``pw-record`` / ``ffmpeg``)
and fall back to the ``sounddevice`` library when a normal ALSA stack is present
(native Linux, or WSL with the ALSA→PulseAudio plugin configured).
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

from .config import CHANNELS, SAMPLE_RATE

_INSTALL_HINT = (
    "No usable audio capture path found.\n"
    "    On WSL/Ubuntu the simplest fix is:  sudo apt install -y pulseaudio-utils\n"
    "    (that gives you `parec`, which records straight from WSLg's mic bridge).\n"
    "    Alternatively install `ffmpeg`, or set up the ALSA→PulseAudio plugin\n"
    "    (sudo apt install -y libasound2-plugins) so the `sounddevice` path works."
)


def _wait_for_enter(stop: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass
    stop.set()


def _run_with_timer(proc: subprocess.Popen, stop: threading.Event) -> None:
    """Tick a timer until Enter is pressed, then stop ``proc`` gracefully."""
    start = time.monotonic()
    while not stop.is_set() and proc.poll() is None:
        time.sleep(0.2)
        print(f"\r  {time.monotonic() - start:6.1f}s", end="", flush=True)
    print()
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=3)


# --- backend: a CLI recorder that writes raw s16le PCM to stdout ---

def _raw_pcm_cmd() -> list[str] | None:
    if shutil.which("parec"):
        return ["parec", f"--rate={SAMPLE_RATE}", f"--channels={CHANNELS}", "--format=s16le", "--latency-msec=50"]
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS), "--format", "s16", "-"]
    return None


def _record_via_raw_pcm(cmd: list[str], dest: Path) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    threading.Thread(target=_wait_for_enter, args=(stop,), daemon=True).start()
    timer = threading.Thread(target=_run_with_timer, args=(proc, stop), daemon=True)
    timer.start()
    assert proc.stdout is not None
    pcm = proc.stdout.read()  # blocks until the process is signalled and closes stdout
    timer.join()
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)  # s16le
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return len(pcm) / (2 * CHANNELS * SAMPLE_RATE)


# --- backend: ffmpeg pulling from PulseAudio ---

def _ffmpeg_cmd(dest: Path) -> list[str]:
    # -f pulse is Linux/WSL only; see _pulse_ffmpeg_available() for why this is
    # never reached elsewhere.
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "pulse", "-i", "default",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        str(dest),
    ]  # fmt: skip


def _record_via_ffmpeg(dest: Path) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = _ffmpeg_cmd(dest)
    stop = threading.Event()
    # stderr to a temp file, not DEVNULL: when ffmpeg refuses the input format the
    # message is the whole diagnosis, and swallowing it turns a clear error into a
    # bare "nothing recorded". A file rather than PIPE so a long take cannot fill
    # the pipe buffer while _run_with_timer is blocked waiting on the process.
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf)
        threading.Thread(target=_wait_for_enter, args=(stop,), daemon=True).start()
        _run_with_timer(proc, stop)
        errf.seek(0)
        err = errf.read().decode(errors="replace").strip()
    if not dest.exists():
        raise RuntimeError(f"ffmpeg captured nothing (exit {proc.returncode})" + (f":\n    {err}" if err else ""))
    with wave.open(str(dest), "rb") as w:
        return w.getnframes() / w.getframerate()


# --- backend: the sounddevice library (real ALSA / native Linux) ---

def _record_via_sounddevice(dest: Path) -> float:
    import queue

    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    blocks: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata, frames, time_info, status):  # noqa: ANN001 - sounddevice signature
        if status:
            print(f"  (audio warning: {status})", file=sys.stderr)
        blocks.put(indata.copy())

    stop = threading.Event()
    threading.Thread(target=_wait_for_enter, args=(stop,), daemon=True).start()

    chunks: list[np.ndarray] = []
    start = time.monotonic()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32", callback=callback):
        while not stop.is_set():
            try:
                chunks.append(blocks.get(timeout=0.2))
            except queue.Empty:
                pass
            print(f"\r  {time.monotonic() - start:6.1f}s", end="", flush=True)
        while not blocks.empty():
            chunks.append(blocks.get_nowait())
    print()

    audio = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, CHANNELS), dtype="float32")
    dest.parent.mkdir(parents=True, exist_ok=True)
    sf.write(dest, audio, SAMPLE_RATE, subtype="PCM_16")
    return len(audio) / SAMPLE_RATE


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
    print("● Recording — speak now. Press Enter to stop.")

    backend = selected_backend()
    if backend in ("parec", "pw-record"):
        return _record_via_raw_pcm(_raw_pcm_cmd(), dest)
    if backend == "ffmpeg":
        return _record_via_ffmpeg(dest)
    if backend is None:
        raise RuntimeError(_INSTALL_HINT)
    try:
        return _record_via_sounddevice(dest)
    except Exception as exc:  # noqa: BLE001 - PortAudio "no device" etc.
        raise RuntimeError(f"sounddevice capture failed: {exc}\n\n{_INSTALL_HINT}") from exc
