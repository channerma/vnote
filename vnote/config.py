"""Defaults, paths, persisted user config — and the settings registry.

``SETTINGS`` is the one list of user-facing settings (key, env var, default,
description). It drives the web UI's Settings page, ``vnote --config``, and a
docs-consistency test. Runtime settings resolve as CLI flag (handled in ``cli``) >
environment variable > persisted config file > built-in default, on every call.
Startup settings (``editable=False``) are plain env-var > built-in module constants
bound when the process starts — change them and restart.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
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

# --- warm daemon (`vnote --serve`) ---
DAEMON_HOST = os.environ.get("VNOTE_DAEMON_HOST", "127.0.0.1")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not a number; using {default}", file=sys.stderr)
        return default


DAEMON_PORT = _int_env("VNOTE_DAEMON_PORT", 8760)


def daemon_addr() -> tuple[str, int]:
    return DAEMON_HOST, DAEMON_PORT



# Cleanup intensity modes (``dictation`` = plain text, no title, small fast model).
MODES = ("light", "edit", "summary", "dictation")
DEFAULT_MODE = "edit"


def vocab_file() -> Path:
    """The custom-vocabulary file: ``$VNOTE_VOCAB`` or ``<config dir>/vocab.txt``."""
    env = os.environ.get("VNOTE_VOCAB")
    return Path(env).expanduser() if env else config_dir() / "vocab.txt"


# --- the settings registry ---------------------------------------------------


@dataclass(frozen=True)
class Setting:
    key: str
    env: str
    default: object
    description: str
    kind: str = "str"  # choice | str | path | int
    choices: tuple[str, ...] = ()
    editable: bool = True  # False: a startup constant — set the env var and restart


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "backend", "VNOTE_BACKEND", BUILTIN_BACKEND,
        "Which LLM cleans up transcripts: ollama (local, offline, free), claude-code (your Claude "
        "subscription through the Claude Code CLI, no API key), or claude (the Anthropic API, billed per token).",
        "choice", ("ollama", "claude-code", "claude"),
    ),
    Setting(
        "default_mode", "VNOTE_MODE", DEFAULT_MODE,
        "Cleanup mode used when none is picked: light (fix fillers and grammar only), edit (reorganize into "
        "headings and lists), summary (condense), dictation (plain text on a small fast model).",
        "choice", MODES,
    ),
    Setting(
        "language", "VNOTE_LANGUAGE", "",
        "Transcription language code such as en or de. Blank = auto-detect on every recording.",
    ),
    Setting(
        "ollama_model", "VNOTE_OLLAMA_MODEL", BUILTIN_OLLAMA_MODEL,
        "Ollama model for note cleanup (light / edit / summary). Pull it once: ollama pull <model>.",
    ),
    Setting(
        "dictation_model", "VNOTE_DICTATION_MODEL", "",
        "Ollama model for dictation mode — ideally small and fast, e.g. llama3.2:3b. Blank = same as ollama_model.",
    ),
    Setting("ollama_host", "OLLAMA_HOST", "http://127.0.0.1:11434", "Where Ollama listens."),
    Setting(
        "claude_model", "VNOTE_CLAUDE_MODEL", "claude-sonnet-5",
        "Model for the claude (API) backend. The claude-code backend uses the CLI's own model choice instead.",
    ),
    Setting(
        "claude_code_bin", "VNOTE_CLAUDE_CODE_BIN", "claude",
        "Name or path of the Claude Code CLI used by the claude-code backend.",
    ),
    # --- bound at startup: shown read-only; set the env var and restart the daemon ---
    Setting(
        "whisper_model", "VNOTE_WHISPER_MODEL", "large-v3-turbo",
        "faster-whisper model loaded when the daemon starts (about 1.6 GB, downloaded on first use).",
        editable=False,
    ),
    Setting(
        "notes_dir", "VNOTE_DIR", str(Path(__file__).resolve().parent.parent / "voice-notes"),
        "Where note folders are written.", "path", editable=False,
    ),
    Setting(
        "daemon_host", "VNOTE_DAEMON_HOST", "127.0.0.1",
        "Address the daemon binds. Keep it on localhost — there is no authentication.", editable=False,
    ),
    Setting("daemon_port", "VNOTE_DAEMON_PORT", 8760, "Port the daemon listens on.", "int", editable=False),
    Setting(
        "vocab", "VNOTE_VOCAB", str(Path.home() / ".config" / "vnote" / "vocab.txt"),
        "Custom-vocabulary file: hotwords to bias transcription and whole-word corrections. Edit its contents "
        "in the web UI or any editor; changes apply to the next recording.",
        "path", editable=False,
    ),
)

_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

# What the running process actually uses for the startup settings.
_STARTUP_VALUES = {
    "whisper_model": lambda: WHISPER_MODEL,
    "notes_dir": lambda: str(NOTES_DIR),
    "daemon_host": lambda: DAEMON_HOST,
    "daemon_port": lambda: DAEMON_PORT,
    "vocab": lambda: str(vocab_file()),
}


def setting(key: str) -> Setting:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise ValueError(f"unknown setting: {key}") from None


def _coerce(s: Setting, raw: object) -> object:
    if s.kind == "int":
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"{s.key} must be an integer, got {raw!r}") from None
    value = str(raw).strip()
    if s.kind == "choice" and value not in s.choices:
        raise ValueError(f"{s.key} must be one of {', '.join(s.choices)}; got {value!r}")
    if s.key == "ollama_host" and not value.startswith(("http://", "https://")):
        raise ValueError(f"ollama_host must start with http:// or https://; got {value!r}")
    return value


def source(key: str) -> str:
    """Where the effective value comes from: ``env`` | ``file`` | ``default``."""
    s = setting(key)
    if os.environ.get(s.env, "") != "":
        return "env"
    if s.editable and load_config().get(key) not in (None, ""):
        return "file"
    return "default"


def get(key: str) -> object:
    """The effective value of a setting: env > config file > default (startup keys: the live constant)."""
    s = setting(key)
    if not s.editable:
        return _STARTUP_VALUES[key]()
    env = os.environ.get(s.env, "")
    if env != "":
        return _coerce(s, env) if s.kind == "int" else env
    file = load_config().get(key)
    if file not in (None, ""):
        if s.kind == "int":
            try:
                return int(file)
            except (TypeError, ValueError):
                return s.default
        return str(file)
    return s.default


def describe() -> list[dict]:
    """Every setting with its effective value — the payload behind the web UI and ``vnote --config``."""
    out = []
    for s in SETTINGS:
        row = {
            "key": s.key,
            "env": s.env,
            "value": get(s.key),
            "default": str(config_dir() / "vocab.txt") if s.key == "vocab" else s.default,
            "description": s.description,
            "kind": s.kind,
            "source": source(s.key),
            "editable": s.editable,
        }
        if s.choices:
            row["choices"] = list(s.choices)
        out.append(row)
    return out


def update(changes: dict) -> list[str]:
    """Validate and persist editable settings to the config file. Returns the keys written.

    Raises ``ValueError`` (with a user-facing message) on an unknown key, a startup
    key, a key currently overridden by its environment variable, or a bad value. A
    blank value removes the key so the built-in default applies again.
    """
    cfg = load_config()
    saved: list[str] = []
    for key, raw in changes.items():
        s = setting(key)
        if not s.editable:
            raise ValueError(f"{key} is set when the daemon starts — set {s.env} and restart")
        if os.environ.get(s.env, "") != "":
            raise ValueError(f"{key} is overridden by {s.env} in the environment; unset it to change it here")
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            cfg.pop(key, None)  # blank / null = back to the built-in default
        else:
            cfg[key] = _coerce(s, raw)
        saved.append(key)
    save_config(cfg)
    return saved


# --- resolvers (env > file > built-in), evaluated on every call ----------------


def backend() -> str:
    """Resolve the cleanup backend."""
    return str(get("backend"))


def ollama_model() -> str:
    """Resolve the Ollama cleanup model."""
    return str(get("ollama_model"))


def dictation_model() -> str:
    """Resolve the model for `dictation` cleanup — ideally small and fast (a warm 3B).

    Falls back to the regular note-cleanup model so dictation works with no extra setup.
    """
    return str(get("dictation_model")) or ollama_model()


def default_mode() -> str:
    return str(get("default_mode"))


def language() -> str | None:
    """Forced transcription language, or None for auto-detect."""
    return str(get("language")) or None
