"""`vnote --doctor`: check the moving parts and report what works.

vnote leans on several external pieces (an audio recorder, a clipboard tool, the
GPU/CUDA stack, and a cleanup backend). This walks each one and prints a clear
status so "it doesn't work on my machine" becomes self-service.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import __version__, config
from .output import _clipboard_commands

OK = "[ok]"
WARN = "[warn]"
BAD = "[FAIL]"


def _check_recorder() -> tuple[str, str]:
    # Ask record.py which backend it will actually pick rather than re-deriving
    # the order here: the lists drifted once, and doctor cheerfully reported
    # "`ffmpeg` found" on a Mac where the ffmpeg branch cannot work at all.
    from .record import selected_backend

    backend = selected_backend()
    if backend is None:
        return BAD, ("no recorder found — install pulseaudio-utils (parec) or ffmpeg, or record in the "
                    "browser via the web UI (`vnote --serve`); file mode still works")
    if backend == "sounddevice":
        return OK, "recorder: `sounddevice` (PortAudio) library available"
    return OK, f"recorder: `{backend}` found"


def _check_gpu() -> tuple[str, str]:
    if shutil.which("nvidia-smi"):
        return OK, "GPU: nvidia-smi present (CUDA path; auto-falls back to CPU on failure)"
    return WARN, "no nvidia-smi — transcription will run on CPU (works, but slower)"


def _check_clipboard() -> tuple[str, str]:
    cmds = _clipboard_commands()
    if cmds:
        return OK, f"clipboard: `{Path(cmds[0][0]).name}`"
    return WARN, "no clipboard tool (clip.exe/wl-copy/xclip/xsel/pbcopy) — notes are still saved to disk"


def _check_daemon() -> tuple[str, str]:
    from . import daemon

    host, port = config.daemon_addr()
    if daemon.is_up():
        return OK, f"daemon: up at {host}:{port} (models warm; web UI at http://{host}:{port})"
    return WARN, f"no daemon at {host}:{port} — models load per run; `vnote --serve` starts one (plus the web UI)"


def _check_backend(backend: str) -> tuple[str, str]:
    if backend == "ollama":
        from .cleanup import _ollama_get

        if _ollama_get("/api/version") is None:
            return WARN, f"Ollama not reachable at {config.get('ollama_host')} — start it: `ollama serve`"
        model = config.ollama_model()
        names = {m.get("name", "") for m in (_ollama_get("/api/tags", timeout=5.0) or {}).get("models", [])}
        if model in names or any(n == model or n.startswith(model + ":") for n in names):
            return OK, f"Ollama up; model `{model}` pulled"
        return WARN, f"Ollama up, but model `{model}` not pulled — `ollama pull {model}`"
    if backend == "claude-code":
        from .cleanup import claude_code_bin

        exe = claude_code_bin()
        if exe is None:
            return BAD, (
                f"claude-code backend selected but the Claude Code CLI "
                f"({config.get('claude_code_bin')!r}) is not on PATH"
            )
        return OK, f"claude-code backend: CLI at {exe} (uses your Claude subscription)"
    if backend == "opencode":
        from .cleanup import opencode_bin

        exe = opencode_bin()
        if exe is None:
            return BAD, (
                f"opencode backend selected but the opencode CLI "
                f"({config.OPENCODE_BIN!r}) is not on PATH"
            )
        model = config.opencode_model()
        which = f"model `{model}`" if model else "opencode's own default model"
        return OK, f"opencode backend: CLI at {exe} ({which}; `opencode models` lists them)"
    # claude (metered API)
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return BAD, "claude backend selected but `anthropic` not installed — `uv pip install -e '.[claude]'`"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return WARN, (
            "claude backend selected but ANTHROPIC_API_KEY is not set — the SDK will "
            "fall back to an `ant auth login` profile if you have one (see .env.example)"
        )
    return OK, f"claude backend: `anthropic` installed, key set, model {config.get('claude_model')}"


def run(backend: str) -> int:
    """Print the report. Returns 1 if any hard failure, else 0."""
    print(f"vnote {__version__}  (python {sys.version.split()[0]})")
    print(f"backend: {backend}")
    print(f"config : {config.config_file()} {'(exists)' if config.config_file().exists() else '(none yet)'}")
    print(f"notes  : {config.NOTES_DIR}")
    print()

    rows = [
        _check_recorder(),
        _check_gpu(),
        _check_clipboard(),
        _check_daemon(),
        _check_backend(backend),
    ]
    failures = 0
    for mark, msg in rows:
        print(f"  {mark:<6} {msg}")
        if mark == BAD:
            failures += 1

    print()
    if failures:
        print(f"{failures} blocking issue(s) above. File mode (`vnote some.wav`) may still work.")
        return 1
    print("All good. Try it:  vnote   (or:  vnote .testdata/jfk.flac)")
    return 0
