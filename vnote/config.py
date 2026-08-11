"""Defaults, paths, and persisted user config.

Resolution order for the settings the first-run chooser manages (``backend`` and
``ollama_model``) is: CLI flag (handled in ``cli``) > environment variable >
persisted config file > built-in default. Everything else is env-var > built-in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from a .env file into os.environ.

    Dependency-free and deliberately minimal: blank lines and ``#`` comments are
    skipped, surrounding quotes are stripped, and real environment variables
    already set always win (the file never overrides them).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


# Auto-load a .env from the current working directory, if present.
_load_dotenv(Path.cwd() / ".env")


# --- persisted config file (written by the first-run chooser) ---------------


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/vnote`` (or ``~/.config/vnote``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vnote"


def config_file() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    """Return the persisted config dict, or ``{}`` if absent/unreadable."""
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# --- built-in defaults ------------------------------------------------------

BUILTIN_BACKEND = "ollama"
BUILTIN_OLLAMA_MODEL = "qwen2.5:14b-instruct"

# Where session folders are written. Override with VNOTE_DIR.
NOTES_DIR = Path(os.environ.get("VNOTE_DIR", Path(__file__).resolve().parent.parent / "voice-notes"))

# --- Whisper ---
WHISPER_MODEL = os.environ.get("VNOTE_WHISPER_MODEL", "large-v3-turbo")
SAMPLE_RATE = 16_000  # Whisper's native rate; we record straight at it.
CHANNELS = 1

# --- LLM cleanup ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
# Metered-API backend (`--backend claude`) only.
CLAUDE_MODEL = os.environ.get("VNOTE_CLAUDE_MODEL", "claude-sonnet-5")
# Subscription backend (`--backend claude-code`): the CLI to shell out to. The
# model is deliberately not pinned here — Claude Code uses the user's own choice.
CLAUDE_CODE_BIN = os.environ.get("VNOTE_CLAUDE_CODE_BIN", "claude")

# --- warm daemon (`vnote --serve`) ---
DAEMON_HOST = os.environ.get("VNOTE_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("VNOTE_DAEMON_PORT", "8760"))


def daemon_addr() -> tuple[str, int]:
    return DAEMON_HOST, DAEMON_PORT


# --- flow client (`vnote-flow`) ---
def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_on(name: str) -> bool:
    """Default-on switch: only an explicit 0/false/no/off disables it."""
    return os.environ.get(name, "").strip().lower() not in ("0", "false", "no", "off")


HOTKEY = os.environ.get("VNOTE_HOTKEY", "ctrl+shift+space")
INJECT = os.environ.get("VNOTE_INJECT", "auto")  # auto | paste | type
VAD = _env_bool("VNOTE_VAD")
VAD_SILENCE = float(os.environ.get("VNOTE_VAD_SILENCE", "1.0"))  # trailing-silence stop window, seconds
STREAM = _env_bool("VNOTE_STREAM")
TRAY = _env_bool("VNOTE_TRAY")
HISTORY = _env_on("VNOTE_HISTORY")  # flow takes -> voice-notes/flow/ (PHASE6)
HISTORY_AUDIO = _env_on("VNOTE_HISTORY_AUDIO")
HISTORY_RAW = _env_on("VNOTE_HISTORY_RAW")
HISTORY_CLEAN = _env_on("VNOTE_HISTORY_CLEAN")


# Cleanup intensity modes.
MODES = ("light", "edit", "summary")
DEFAULT_MODE = "edit"


# --- resolvers for chooser-managed settings (env > file > built-in) ---------


def backend() -> str:
    """Resolve the cleanup backend."""
    return os.environ.get("VNOTE_BACKEND") or load_config().get("backend") or BUILTIN_BACKEND


def ollama_model() -> str:
    """Resolve the Ollama cleanup model."""
    return os.environ.get("VNOTE_OLLAMA_MODEL") or load_config().get("ollama_model") or BUILTIN_OLLAMA_MODEL


def dictation_model() -> str:
    """Resolve the model for `dictation` cleanup — ideally small and fast (a warm 3B).

    Falls back to the regular note-cleanup model so dictation works with no extra setup.
    """
    return os.environ.get("VNOTE_DICTATION_MODEL") or load_config().get("dictation_model") or ollama_model()


def app_tones() -> dict[str, str]:
    """The per-app tone map from the config file: window-title substring -> tone."""
    tones = load_config().get("app_tones")
    return tones if isinstance(tones, dict) else {}


def app_tone_for(window_title: str) -> str | None:
    """First tone whose substring appears in the title (case-insensitive), else None."""
    title = window_title.lower()
    for needle, tone in app_tones().items():
        if needle.lower() in title:
            return tone
    return None
