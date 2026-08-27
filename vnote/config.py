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
import re
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


def _default_notes_dir() -> Path:
    """Where session folders go when ``VNOTE_DIR`` is unset.

    From a source checkout this is ``<repo>/voice-notes`` — where a developer
    expects them, and what .gitignore already covers. Installed as a tool the
    *same* relative path lands inside site-packages, so anchor to ``~`` instead.
    Verified 2026-08-26: `uv tool install` wrote notes to
    ``~/.local/share/uv/tools/vnote/lib/python3.14/site-packages/voice-notes/``.
    The marker is pyproject.toml beside the package, not the presence of .git —
    an sdist unpacked for a `pip install -e .` has one and no other.
    """
    root = Path(__file__).resolve().parent.parent
    return root / "voice-notes" if (root / "pyproject.toml").is_file() else Path.home() / "voice-notes"


# Where session folders are written. Override with VNOTE_DIR.
NOTES_DIR = Path(os.environ.get("VNOTE_DIR") or _default_notes_dir())

# --- Whisper ---
# No single default is right for both machines this runs on, so the default follows
# the device. On CPU (macOS: CTranslate2 has no Metal build) large-v3-turbo runs
# ~1x realtime — a 22s note took 24.9s — which is fine for a memo but unusable for
# flow dictation, where you wait on every utterance; `small` is ~3x faster. On CUDA
# large-v3-turbo is also ~realtime, so there accuracy is free. VNOTE_WHISPER_MODEL
# (or `whisper_model` in config.json) overrides both.
WHISPER_MODEL_BY_DEVICE = {"cuda": "large-v3-turbo", "cpu": "small"}


def whisper_model(device: str = "cpu") -> str:
    """The Whisper model to load on ``device``: explicit override, else per-device.

    Resolved at load time rather than import time because the device is not known
    until CUDA has been tried.
    """
    override = os.environ.get("VNOTE_WHISPER_MODEL") or load_config().get("whisper_model")
    return override or WHISPER_MODEL_BY_DEVICE.get(device, "small")


def whisper_model_override() -> str | None:
    """The pinned model, or ``None`` when the device decides. For status output."""
    return os.environ.get("VNOTE_WHISPER_MODEL") or load_config().get("whisper_model") or None
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



# The style used when nothing picks one. Styles themselves live in Markdown files
# (see styles.py); this is only the name of one of them.
DEFAULT_STYLE = "edit"


def vocab_file() -> Path:
    """The custom-vocabulary file: ``$VNOTE_VOCAB`` or ``<config dir>/vocab.txt``."""
    env = os.environ.get("VNOTE_VOCAB")
    return Path(env).expanduser() if env else config_dir() / "vocab.txt"


def styles_dirs() -> list[Path]:
    """Extra style folders from ``$VNOTE_STYLES_DIRS`` (an ``os.pathsep`` list), in order.

    Each one overrides the built-ins and the ones before it; Mine
    (``<config dir>/styles``) is always searched and is where the editor writes.
    """
    raw = os.environ.get("VNOTE_STYLES_DIRS", "")
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]


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
        "default_style", "VNOTE_STYLE", DEFAULT_STYLE,
        "Cleanup style used when none is picked. A style is a Markdown file holding the instruction the "
        "model gets; the choices are whatever the style folders hold (edit them in Settings).",
        "choice",  # choices are dynamic — see choices_for()
    ),
    Setting(
        "language", "VNOTE_LANGUAGE", "",
        "Transcription language code such as en or de. Blank = auto-detect on every recording.",
    ),
    Setting(
        "ollama_model", "VNOTE_OLLAMA_MODEL", BUILTIN_OLLAMA_MODEL,
        "Ollama model for note cleanup. Pull it once: ollama pull <model>. A style can name its own model "
        "instead (a model: line in the style file).",
    ),
    Setting("ollama_host", "OLLAMA_HOST", "http://127.0.0.1:11434", "Where Ollama listens."),
    Setting(
        "ollama_keep_alive", "VNOTE_OLLAMA_KEEP_ALIVE", "30m",
        "How long Ollama keeps the model loaded after a request — e.g. 30m, 1h, -1 = until Ollama exits "
        "(Ollama's own default is 5m).",
    ),
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
        "styles_dirs", "VNOTE_STYLES_DIRS", "",
        "Extra folders of style files, separated by the platform's path separator. Each one overrides the "
        "built-in styles and the folders before it. Your own styles live in <config dir>/styles and are "
        "edited below.",
        "path", editable=False,
    ),
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
    "styles_dirs": lambda: os.pathsep.join(str(p) for p in styles_dirs()),
    "vocab": lambda: str(vocab_file()),
}

# Retired names still honoured on the way in (never written back): 0.6.x called
# styles "modes", so a config file or an environment from then still picks the style.
_DEPRECATED: dict[str, tuple[str, str]] = {"default_style": ("default_mode", "VNOTE_MODE")}


def setting(key: str) -> Setting:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise ValueError(f"unknown setting: {key}") from None


def choices_for(key: str) -> tuple[str, ...]:
    """A choice setting's options. ``default_style`` has no fixed list — the style
    registry is the list, and it changes whenever a style file is added or removed."""
    if key == "default_style":
        from . import styles

        return tuple(styles.names())
    return setting(key).choices


def _coerce(s: Setting, raw: object) -> object:
    if s.kind == "int":
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"{s.key} must be an integer, got {raw!r}") from None
    value = str(raw).strip()
    if s.kind == "choice":
        choices = choices_for(s.key)
        if value not in choices:
            raise ValueError(f"{s.key} must be one of {', '.join(choices)}; got {value!r}")
    if s.key == "ollama_host" and not value.startswith(("http://", "https://")):
        raise ValueError(f"ollama_host must start with http:// or https://; got {value!r}")
    # Ollama reads a bare number as seconds (-1 = until it exits) and anything else with
    # Go's time.ParseDuration, which insists on a unit — reject what it would 400 on.
    if s.key == "ollama_keep_alive" and not re.fullmatch(r"-?\d+|(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+", value):
        raise ValueError(
            f"{s.key} must be a duration like 30m, 1h, 300s, a number of seconds, or -1; got {value!r}"
        )
    return value


def _retired(key: str) -> tuple[str, str] | None:
    """(old config key, old env var) for a setting that was renamed, or None."""
    return _DEPRECATED.get(key)


def source(key: str) -> str:
    """Where the effective value comes from: ``env`` | ``file`` | ``default``."""
    s = setting(key)
    old = _retired(key)
    if os.environ.get(s.env, "") != "" or (old and os.environ.get(old[1], "") != ""):
        return "env"
    if s.editable:
        cfg = load_config()
        if cfg.get(key) not in (None, "") or (old and cfg.get(old[0]) not in (None, "")):
            return "file"
    return "default"


def get(key: str) -> object:
    """The effective value of a setting: env > config file > default (startup keys: the live constant)."""
    s = setting(key)
    if not s.editable:
        return _STARTUP_VALUES[key]()
    old = _retired(key)
    env = os.environ.get(s.env, "") or (os.environ.get(old[1], "") if old else "")
    if env != "":
        return _coerce(s, env) if s.kind == "int" else env
    cfg = load_config()
    file = cfg.get(key)
    if file in (None, "") and old:
        file = cfg.get(old[0])
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
        choices = choices_for(s.key)
        if choices:
            row["choices"] = list(choices)
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
        old = _retired(key)
        if not s.editable:
            raise ValueError(f"{key} is set when the daemon starts — set {s.env} and restart")
        for env in (s.env, old[1] if old else None):
            # The retired env var still resolves the value (see get()), so a write here
            # would be accepted and then do nothing — say so instead.
            if env and os.environ.get(env, "") != "":
                raise ValueError(f"{key} is overridden by {env} in the environment; unset it to change it here")
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            cfg.pop(key, None)  # blank / null = back to the built-in default
        else:
            cfg[key] = _coerce(s, raw)
        if old:
            cfg.pop(old[0], None)  # ... and the retired name never outlives a deliberate write
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


def default_style() -> str:
    """The style name used when nothing picks one (may name a style that no longer exists)."""
    return str(get("default_style"))


def language() -> str | None:
    """Forced transcription language, or None for auto-detect."""
    return str(get("language")) or None
