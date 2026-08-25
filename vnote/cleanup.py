"""LLM cleanup: turn a raw transcript into a tidy, well-organized note.

Pluggable backends, all sharing one prompt and one response parser:

- ``ollama``      local, offline, no account (the zero-setup default).
- ``claude-code`` the Claude Code CLI — uses your Claude subscription, no API key.
- ``opencode``    the opencode CLI — whatever provider/model opencode is already
                  configured with (local MLX/llama.cpp, or a hosted provider).
- ``claude``      the Anthropic API — needs the ``claude`` extra and a metered
                  ``ANTHROPIC_API_KEY``.

The two CLI backends (``claude-code``, ``opencode``) shell out to an *agent* CLI
for what is really a pure text transform, so both take care to disable tools: the
model gets no filesystem, shell or network access, and cannot wander off editing
the user's files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import (
    CLAUDE_CODE_BIN,
    CLAUDE_MODEL,
    OLLAMA_HOST,
    OPENCODE_BIN,
    dictation_model,
    ollama_model,
    opencode_model,
)

# --- prompt construction -----------------------------------------------------

_MODE_INSTRUCTIONS = {
    "light": (
        "Lightly clean the transcript: remove filler words (um, uh, you know, like), "
        "false starts and accidental repetitions; fix grammar, punctuation and capitalization; "
        "correct obvious mis-transcriptions using context. Keep the speaker's wording, content "
        "and order intact. Do not reorganize."
    ),
    "edit": (
        "Edit the transcript into a clean, well-organized note: remove filler words, false starts "
        "and repetitions; fix grammar and punctuation; correct obvious mis-transcriptions; group "
        "related thoughts into paragraphs; add headings or bullet lists where the content naturally "
        "calls for it; smooth transitions. Preserve all of the speaker's points and detail — do not "
        "summarize anything away and do not invent content."
    ),
    "summary": (
        "Rewrite the transcript as a tight, well-organized note: everything in 'edit' mode, plus "
        "cut tangents and trim verbose passages so the result is noticeably more concise than the "
        "original while keeping every substantive point. Use headings and bullets freely."
    ),
    # Flow-mode dictation: the output is typed straight into whatever app has focus,
    # so it must be fast (small model), faithful, and plain — no title, no structure.
    "dictation": (
        "Clean the dictated fragment: remove filler words (um, uh, you know) and false starts; fix "
        "punctuation, capitalization and obvious mis-transcriptions. Apply any spoken formatting or "
        "editing commands ('period', 'comma', 'quote ... unquote', 'scratch that') instead of writing "
        "them out literally. Do not reorganize, summarize, or add anything; keep the speaker's wording."
    ),
}

_DICTATION_SYSTEM = (
    "You clean up dictated text so it can be typed directly into the app the speaker is using. "
    "Follow the user's editing instructions exactly. "
    "Respond with the cleaned text and nothing else — no title line, no preamble, no code fences."
)

_SYSTEM = (
    "You are an editor that turns spoken, dictated transcripts into clean written notes. "
    "The transcript may contain spoken meta-instructions from the speaker about formatting or edits "
    "(e.g. 'make that a bulleted list', 'scratch that last bit', 'put a heading here'). Follow such "
    "instructions and do not include them as literal text in the output. Write in the speaker's own "
    "voice. Output GitHub-flavored Markdown.\n\n"
    "Respond in exactly this format and nothing else:\n"
    "TITLE: <a short 3-7 word title>\n"
    "---\n"
    "<the cleaned note in Markdown>"
)


def _build_user_prompt(transcript: str, mode: str, tone: str | None = None) -> str:
    instruction = _MODE_INSTRUCTIONS[mode]
    if tone:
        instruction += f" Write in a {tone} tone."
    return f"{instruction}\n\nTRANSCRIPT:\n\"\"\"\n{transcript}\n\"\"\""


def _system_for(mode: str) -> str:
    return _DICTATION_SYSTEM if mode == "dictation" else _SYSTEM


def _fallback_title(transcript: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", transcript)
    return " ".join(words[:6]) if words else "voice note"


def _finish(raw: str, transcript: str, mode: str) -> CleanResult:
    """Turn a backend response into a CleanResult, per mode's output contract."""
    if mode == "dictation":  # plain text out, no TITLE/--- framing to parse
        return CleanResult(title=_fallback_title(transcript), body=raw.strip() or transcript)
    return _parse_response(raw, transcript)


def _parse_response(raw: str, transcript: str) -> CleanResult:
    raw = raw.strip()
    title = None
    body = raw
    m = re.match(r"\s*TITLE:\s*(.+?)\s*\n\s*-{3,}\s*\n(.*)", raw, re.DOTALL)
    if m:
        title = m.group(1).strip().strip("\"'")
        body = m.group(2).strip()
    else:
        # Fallback: maybe just "TITLE: ..." on line 1, rest is body.
        first, _, rest = raw.partition("\n")
        fm = re.match(r"\s*TITLE:\s*(.+)", first)
        if fm and rest.strip():
            title = fm.group(1).strip().strip("\"'")
            body = rest.strip()
    return CleanResult(title=title or _fallback_title(transcript), body=body or transcript)


@dataclass
class CleanResult:
    title: str
    body: str


# --- backends ----------------------------------------------------------------


def clean(
    transcript: str,
    mode: str = "edit",
    backend: str = "ollama",
    model: str | None = None,
    tone: str | None = None,
) -> CleanResult:
    if mode not in _MODE_INSTRUCTIONS:
        raise ValueError(f"unknown mode: {mode!r} (expected one of {', '.join(_MODE_INSTRUCTIONS)})")
    if backend == "ollama":
        default = dictation_model() if mode == "dictation" else ollama_model()
        return _clean_ollama(transcript, mode, model or default, tone)
    if backend == "claude-code":
        # No default model: let the Claude Code CLI use whatever the user's own
        # setup selects, so vnote never pins their subscription to one model.
        return _clean_claude_code(transcript, mode, model, tone)
    if backend == "opencode":
        # Same reasoning as claude-code: no default model, so opencode keeps
        # using whichever provider/model the user already selected.
        return _clean_opencode(transcript, mode, model or opencode_model(), tone)
    if backend == "claude":
        return _clean_claude(transcript, mode, model or CLAUDE_MODEL, tone)
    raise ValueError(
        f"unknown backend: {backend!r} (expected 'ollama', 'claude-code', 'opencode' or 'claude')"
    )


# --- Ollama ---


def _ollama_get(path: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None


def _ensure_ollama_running() -> None:
    if _ollama_get("/api/version") is not None:
        return
    print("  starting ollama serve ...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ollama is not installed or not on PATH (see https://ollama.com)") from exc
    for _ in range(40):  # up to ~10s
        time.sleep(0.25)
        if _ollama_get("/api/version") is not None:
            return
    raise RuntimeError("ollama serve did not come up; try running 'ollama serve' manually")


def _ensure_model_present(model: str) -> None:
    tags = _ollama_get("/api/tags", timeout=5.0) or {}
    names = {m.get("name", "") for m in tags.get("models", [])}
    # Ollama lists e.g. "qwen2.5:14b-instruct"; also accept the bare base name.
    if model in names or any(n == model or n.startswith(model + ":") for n in names):
        return
    raise RuntimeError(
        f"Ollama model {model!r} is not pulled yet.\n"
        f"    Run once:  ollama pull {model}"
    )


def _clean_ollama(transcript: str, mode: str, model: str, tone: str | None = None) -> CleanResult:
    _ensure_ollama_running()
    _ensure_model_present(model)
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.3},
        "messages": [
            {"role": "system", "content": _system_for(mode)},
            {"role": "user", "content": _build_user_prompt(transcript, mode, tone)},
        ],
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read())
    content = (data.get("message") or {}).get("content", "")
    if not content.strip():
        raise RuntimeError(f"empty response from Ollama model {model!r}")
    return _finish(content, transcript, mode)


# --- Claude Code CLI (subscription, no API key) ---

_CLAUDE_CODE_TIMEOUT_S = 600


def claude_code_bin() -> str | None:
    """Path to the Claude Code executable, or None if it isn't installed."""
    return shutil.which(CLAUDE_CODE_BIN)


def _clean_claude_code(
    transcript: str, mode: str, model: str | None = None, tone: str | None = None
) -> CleanResult:
    """Clean up via the Claude Code CLI — bills your subscription, not an API key.

    Tools are disabled: this is a pure text transform, so the model needs no
    filesystem, shell or network access. The prompt goes in on stdin rather than
    argv, which would cap out on a long transcript.
    """
    exe = claude_code_bin()
    if exe is None:
        raise RuntimeError(
            f"The claude-code backend needs the Claude Code CLI on PATH (looked for {CLAUDE_CODE_BIN!r}).\n"
            "    Install it:              https://claude.com/product/claude-code\n"
            "    Or point vnote at it:    VNOTE_CLAUDE_CODE_BIN=/path/to/claude\n"
            "    Or use the local backend:  --backend ollama"
        )

    cmd = [exe, "-p", "--allowed-tools", "", "--system-prompt", _system_for(mode)]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            input=_build_user_prompt(transcript, mode, tone),
            capture_output=True,
            text=True,
            timeout=_CLAUDE_CODE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Claude Code timed out after {_CLAUDE_CODE_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run Claude Code ({exe}): {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500] or f"exit {proc.returncode}"
        raise RuntimeError(
            f"Claude Code failed: {detail}\n"
            "    If it needs sign-in, run `claude` once interactively first."
        )
    if not proc.stdout.strip():
        raise RuntimeError("empty response from Claude Code")
    return _finish(proc.stdout, transcript, mode)


# --- opencode CLI (whatever provider/model opencode is configured with) ---

_OPENCODE_TIMEOUT_S = 600

# Tools opencode may expose to an agent. vnote needs none of them — this is a
# pure text transform — and leaving them on invites the model to "helpfully"
# read or write files. Listed explicitly rather than relying on a deny-by-default:
# unknown keys are ignored, so naming a tool opencode drops later is harmless,
# while a tool we forget to name would stay enabled.
_OPENCODE_TOOLS_OFF = (
    "write", "edit", "patch", "bash", "read", "grep", "glob", "list",
    "webfetch", "task", "todowrite", "todoread",
)


def opencode_bin() -> str | None:
    """Path to the opencode executable, or None if it isn't installed."""
    return shutil.which(OPENCODE_BIN)


def _opencode_pure() -> bool:
    """Whether to pass ``--pure`` (skip external plugins). Default on.

    Isolation is the point of this path, but a user whose provider or auth comes
    from an opencode *plugin* needs those loaded — ``VNOTE_OPENCODE_PURE=0``.
    """
    return os.environ.get("VNOTE_OPENCODE_PURE", "").strip().lower() not in ("0", "false", "no", "off")


def _write_opencode_agent(sandbox: Path, mode: str) -> None:
    """Write a throwaway, tool-free opencode agent whose prompt is vnote's own.

    opencode has no ``--system-prompt`` flag; the supported way to set one is an
    agent definition, which it picks up from ``<dir>/.opencode/agent/*.md``. So
    the sandbox *is* the configuration: a scratch directory holding one agent and
    nothing else. Running there also means the model sees an empty project rather
    than whatever the user happened to `cd` into.
    """
    agent_dir = sandbox / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    tools = "\n".join(f"  {name}: false" for name in _OPENCODE_TOOLS_OFF)
    agent_dir.joinpath("vnote.md").write_text(
        "---\n"
        "description: vnote transcript cleanup (pure text transform)\n"
        "mode: primary\n"
        "temperature: 0.3\n"
        "tools:\n"
        f"{tools}\n"
        "---\n"
        f"{_system_for(mode)}\n",
        encoding="utf-8",
    )


def _opencode_text(stdout: str) -> str:
    """Concatenate the assistant text from opencode's ``--format json`` stream.

    The stream is JSON-lines of typed events. Only ``text`` parts are collected,
    which conveniently drops ``reasoning`` parts — several models opencode can be
    pointed at are thinking models, and their scratchpad must not land in a note.
    """
    chunks = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue  # banner or stray log line, not an event
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "text":
            text = (event.get("part") or {}).get("text")
            if text:
                chunks.append(text)
    return "".join(chunks)


def _clean_opencode(
    transcript: str, mode: str, model: str | None = None, tone: str | None = None
) -> CleanResult:
    """Clean up via the opencode CLI, using the provider/model it's configured with.

    Like the claude-code backend: tools disabled, prompt on stdin (argv would cap
    out on a long transcript), and no model pinned unless the user asked for one.
    """
    exe = opencode_bin()
    if exe is None:
        raise RuntimeError(
            f"The opencode backend needs the opencode CLI on PATH (looked for {OPENCODE_BIN!r}).\n"
            "    Install it:              https://opencode.ai\n"
            "    Or point vnote at it:    VNOTE_OPENCODE_BIN=/path/to/opencode\n"
            "    Or use the local backend:  --backend ollama"
        )

    # ignore_cleanup_errors: on Windows a file opencode still holds open would
    # otherwise turn a successful cleanup into a crash at teardown.
    with tempfile.TemporaryDirectory(prefix="vnote-opencode-", ignore_cleanup_errors=True) as tmp:
        sandbox = Path(tmp)
        _write_opencode_agent(sandbox, mode)
        cmd = [exe, "run", "--dir", str(sandbox), "--agent", "vnote", "--format", "json"]
        if _opencode_pure():
            cmd.insert(1, "--pure")
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(
                cmd,
                input=_build_user_prompt(transcript, mode, tone),
                capture_output=True,
                text=True,
                timeout=_OPENCODE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"opencode timed out after {_OPENCODE_TIMEOUT_S}s") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not run opencode ({exe}): {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500] or f"exit {proc.returncode}"
        raise RuntimeError(
            f"opencode failed: {detail}\n"
            "    Check its providers are set up:  opencode models"
        )
    content = _opencode_text(proc.stdout)
    if not content.strip():
        detail = (proc.stderr or "").strip()[:300]
        raise RuntimeError(
            "empty response from opencode" + (f": {detail}" if detail else "")
            + "\n    Is a model configured and reachable?  opencode models"
        )
    return _finish(content, transcript, mode)


# --- Claude (optional metered API backend) ---


def _clean_claude(transcript: str, mode: str, model: str, tone: str | None = None) -> CleanResult:
    """Clean up via the Anthropic API. Opt-in: needs the `claude` extra + a key.

    Reuses the same system prompt, user prompt and response parser as the local
    backend so the two produce the same TITLE/--- output shape.

    No sampling parameters are sent: `temperature`/`top_p`/`top_k` are rejected
    with a 400 on current models (Sonnet 5, Opus 5, Opus 4.8/4.7), so passing one
    would pin this backend to an older generation.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "The Claude backend needs the `anthropic` package.\n"
            "    Install the extra:  uv pip install -e '.[claude]'   (or: uv pip install anthropic)\n"
            "    Or use the default local backend:  --backend ollama"
        ) from exc

    try:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    except Exception as exc:  # noqa: BLE001 - SDK raises if no key is configured
        raise RuntimeError(
            f"Could not initialize the Anthropic client: {exc}\n"
            "    Set ANTHROPIC_API_KEY (see .env.example), or use --backend ollama."
        ) from exc

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_system_for(mode),
            messages=[{"role": "user", "content": _build_user_prompt(transcript, mode, tone)}],
        )
    except anthropic.APIError as exc:
        raise RuntimeError(f"Anthropic API error: {exc}") from exc

    content = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
    if not content.strip():
        raise RuntimeError(f"empty response from Claude model {model!r}")
    return _finish(content, transcript, mode)
