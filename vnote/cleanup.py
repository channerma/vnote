"""LLM cleanup: turn a raw transcript into a tidy, well-organized note.

Pluggable backends, all sharing one prompt and one response parser:

- ``ollama``      local, offline, no account (the zero-setup default).
- ``claude-code`` the Claude Code CLI — uses your Claude subscription, no API key.
- ``claude``      the Anthropic API — needs the ``claude`` extra and a metered
                  ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config, styles
from .config import ollama_model
from .styles import Style

# --- prompt construction -----------------------------------------------------

# The two output contracts a style picks between (`output:` in its front matter).
_PLAIN_SYSTEM = (
    "You rework a spoken, dictated transcript into text the speaker can paste straight into "
    "whatever they are writing. Follow the instructions below exactly. "
    "Respond with the text and nothing else — no title line, no preamble, no code fences."
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


_REVISE_SYSTEM = (
    "You revise an existing Markdown note that was written from a dictated transcript. "
    "Apply the author's instruction to the note. Keep everything the instruction does not "
    "touch exactly as it is — the author's voice, the content, the structure, the wording. "
    "Output GitHub-flavored Markdown.\n\n"
    "Respond in exactly this format and nothing else:\n"
    "TITLE: <keep the existing title unless the instruction changes it>\n"
    "---\n"
    "<the revised note>"
)


def _build_user_prompt(
    transcript: str, style: Style, tone: str | None = None, instructions: str | None = None
) -> str:
    instruction = style.body
    if tone:
        instruction += f" Write in a {tone} tone."
    prompt = f"{instruction}\n\nTRANSCRIPT:\n\"\"\"\n{transcript}\n\"\"\""
    if instructions and instructions.strip():
        prompt += (
            "\n\nAdditional instructions from the author (follow them; they take precedence "
            f"over the defaults above): {instructions.strip()}"
        )
    return prompt


def _build_revise_prompt(note_text: str, instructions: str) -> str:
    return f"INSTRUCTION:\n{instructions}\n\nNOTE:\n\"\"\"\n{note_text}\n\"\"\""


def _split_heading(note_text: str) -> tuple[str, str]:
    """Split a leading '# Title' off a note: (title, note without that heading)."""
    m = re.match(r"\s*#[ \t]+(.+?)[ \t]*(?:\n(.*))?$", note_text, re.DOTALL)
    if m:
        return m.group(1).strip(), (m.group(2) or "").strip()
    return "", note_text.strip()


def _system_for(style: Style) -> str:
    return _PLAIN_SYSTEM if style.output == "plain" else _SYSTEM


def _fallback_title(transcript: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", transcript)
    return " ".join(words[:6]) if words else "voice note"


def _finish(raw: str, transcript: str, style: Style) -> CleanResult:
    """Turn a backend response into a CleanResult, per the style's output contract."""
    if style.output == "plain":  # no TITLE/--- framing to parse; the title is derived
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
    mode: str = config.DEFAULT_STYLE,
    backend: str | None = None,
    model: str | None = None,
    tone: str | None = None,
    instructions: str | None = None,
) -> CleanResult:
    """Clean ``transcript`` with the named style.

    ``mode`` keeps its name — every caller passes ``mode=`` — but it holds a *style*
    name now (styles.py). ``backend``/``model`` are the explicit picks: leave them
    None and the style's own lines apply, then the settings.
    """
    style = styles.get(mode)
    if style is None:
        raise ValueError(f"unknown style: {mode!r} (expected one of {', '.join(styles.names())})")
    raw = _complete(
        backend or style.backend or config.backend(),
        _system_for(style),
        _build_user_prompt(transcript, style, tone, instructions),
        model or style.model,
    )
    return _finish(raw, transcript, style)


def revise(
    note_text: str,
    instructions: str,
    *,
    backend: str | None = None,
    model: str | None = None,
) -> CleanResult:
    """Rework an existing note per a free-text instruction ("make it shorter").

    Unlike clean(), the input is the finished Markdown note rather than the
    transcript. A leading '# Title' heading is split off and handed back as the
    title, so revising a note round-trips through the same CleanResult shape.
    """
    if not instructions or not instructions.strip():
        raise ValueError("revise needs a non-empty instruction")
    heading, body = _split_heading(note_text)
    raw = _complete(
        backend or config.backend(),
        _REVISE_SYSTEM,
        _build_revise_prompt(body, instructions.strip()),
        model,
    )
    result = _parse_response(raw, heading or note_text)
    if heading and result.title == _fallback_title(heading):
        # The model dropped the TITLE: line — keep the note's own heading verbatim.
        result = CleanResult(title=heading, body=result.body)
    return result


def _complete(backend: str, system: str, user: str, model: str | None) -> str:
    """Run one prompt through the chosen backend; returns the raw model text."""
    if backend == "ollama":
        return _ollama_complete(system, user, model or ollama_model())
    if backend == "claude-code":
        # No default model: let the Claude Code CLI use whatever the user's own
        # setup selects, so vnote never pins their subscription to one model.
        return _claude_code_complete(system, user, model)
    if backend == "claude":
        return _claude_complete(system, user, model or str(config.get("claude_model")))
    raise ValueError(
        f"unknown backend: {backend!r} (expected 'ollama', 'claude-code' or 'claude')"
    )


# --- Ollama ---


def _ollama_get(path: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{config.get('ollama_host')}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):  # ValueError: bad URL/scheme
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


def _keep_alive() -> str | int:
    """The ``keep_alive`` value in the form Ollama actually accepts.

    A JSON *string* goes through Go's time.ParseDuration and must carry a unit
    ("30m"); only a JSON *number* means seconds, with -1 = keep it loaded until
    Ollama exits. Sending "-1" as a string is a 400 ("time: missing unit in
    duration") — checked against Ollama 0.23.1, 2026-08-25.
    """
    value = str(config.get("ollama_keep_alive")).strip()
    return int(value) if re.fullmatch(r"-?\d+", value) else value


def preload_ollama(model: str) -> None:
    """Load ``model`` into Ollama's memory so the first note doesn't pay for it.

    An empty ``messages`` array is Ollama's documented way to load a model without
    generating anything (API doc, read 2026-08-25). Raises on any failure — the
    daemon's background warm decides what a failure means.
    """
    _ensure_ollama_running()
    _ensure_model_present(model)
    payload = {
        "model": model,
        "messages": [],
        "keep_alive": _keep_alive(),
    }
    req = urllib.request.Request(
        f"{config.get('ollama_host')}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300):  # a cold pull of weights from disk is slow
        pass


def _ollama_complete(system: str, user: str, model: str) -> str:
    _ensure_ollama_running()
    _ensure_model_present(model)
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.3},
        "keep_alive": _keep_alive(),  # keep it hot for the next note
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        f"{config.get('ollama_host')}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read())
    content = (data.get("message") or {}).get("content", "")
    if not content.strip():
        raise RuntimeError(f"empty response from Ollama model {model!r}")
    return content


# --- Claude Code CLI (subscription, no API key) ---

_CLAUDE_CODE_TIMEOUT_S = 600


def claude_code_bin() -> str | None:
    """Path to the Claude Code executable, or None if it isn't installed."""
    return shutil.which(str(config.get("claude_code_bin")))


def _claude_code_complete(system: str, user: str, model: str | None = None) -> str:
    """Run a prompt through the Claude Code CLI — bills your subscription, not an API key.

    Tools are disabled: this is a pure text transform, so the model needs no
    filesystem, shell or network access. The prompt goes in on stdin rather than
    argv, which would cap out on a long transcript.
    """
    exe = claude_code_bin()
    if exe is None:
        raise RuntimeError(
            f"The claude-code backend needs the Claude Code CLI on PATH "
            f"(looked for {config.get('claude_code_bin')!r}).\n"
            "    Install it:              https://claude.com/product/claude-code\n"
            "    Or point vnote at it:    VNOTE_CLAUDE_CODE_BIN=/path/to/claude\n"
            "    Or use the local backend:  --backend ollama"
        )

    cmd = [exe, "-p", "--allowed-tools", "", "--system-prompt", system]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            input=user,
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
    return proc.stdout


# --- Claude (optional metered API backend) ---


def _claude_complete(system: str, user: str, model: str) -> str:
    """Run a prompt through the Anthropic API. Opt-in: needs the `claude` extra + a key.

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
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as exc:
        raise RuntimeError(f"Anthropic API error: {exc}") from exc

    content = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
    if not content.strip():
        raise RuntimeError(f"empty response from Claude model {model!r}")
    return content
