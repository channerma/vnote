"""Tests for the transcript-cleanup prompts, parser and backend dispatch (no network)."""

import json
import subprocess
from pathlib import Path

import pytest

from vnote import cleanup, config
from vnote.cleanup import _build_user_prompt, _finish, _parse_response, clean


def test_parse_full_title_and_body():
    raw = "TITLE: My Great Note\n---\nFirst line.\n\nSecond paragraph."
    r = _parse_response(raw, "ignored transcript")
    assert r.title == "My Great Note"
    assert r.body == "First line.\n\nSecond paragraph."


def test_parse_strips_quotes_around_title():
    r = _parse_response('TITLE: "Quoted Title"\n---\nbody', "x")
    assert r.title == "Quoted Title"


def test_parse_fallback_first_line_without_separator():
    raw = "TITLE: No Separator Here\nbut there is a body"
    r = _parse_response(raw, "x")
    assert r.title == "No Separator Here"
    assert r.body == "but there is a body"


def test_parse_no_title_falls_back_to_transcript_words():
    r = _parse_response("just some body with no title marker", "alpha beta gamma delta")
    assert r.title == "alpha beta gamma delta"
    # body is the raw response when no TITLE present
    assert "just some body" in r.body


def test_parse_empty_everything_yields_placeholder_title():
    r = _parse_response("", "")
    assert r.title == "voice note"
    # body falls back to the (empty) transcript
    assert r.body == ""


def test_build_user_prompt_includes_mode_instruction_and_transcript():
    prompt = _build_user_prompt("hello there", "light")
    assert "hello there" in prompt
    assert "filler" in prompt.lower()  # the 'light' instruction mentions filler words


# --- dictation mode (flow client) --------------------------------------------


def test_dictation_finish_is_plain_text_not_title_framed():
    r = _finish("TITLE: looks like a title\n---\nbut dictation takes it verbatim", "orig words here", "dictation")
    assert r.body == "TITLE: looks like a title\n---\nbut dictation takes it verbatim"
    assert r.title == "orig words here"  # fallback title from the transcript; flow ignores it


def test_note_modes_still_parse_title_framing():
    r = _finish("TITLE: A Note\n---\nbody", "x", "edit")
    assert (r.title, r.body) == ("A Note", "body")


def test_dictation_prompt_mentions_spoken_commands():
    prompt = _build_user_prompt("x", "dictation")
    assert "scratch that" in prompt


def test_tone_lands_in_the_prompt():
    assert "Write in a casual tone." in _build_user_prompt("x", "dictation", tone="casual")
    assert "tone" not in _build_user_prompt("x", "light")  # no tone -> no tone sentence


def test_clean_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown mode"):
        clean("x", mode="bogus")


def test_dictation_model_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_DICTATION_MODEL", raising=False)
    monkeypatch.delenv("VNOTE_OLLAMA_MODEL", raising=False)

    assert config.dictation_model() == config.ollama_model()  # falls back to the note model
    config.save_config({"dictation_model": "qwen2.5:3b-instruct"})
    assert config.dictation_model() == "qwen2.5:3b-instruct"
    monkeypatch.setenv("VNOTE_DICTATION_MODEL", "llama3.2:3b")
    assert config.dictation_model() == "llama3.2:3b"


# --- claude-code backend (subscription CLI; no network, no API key) -----------


def test_clean_rejects_unknown_backend_and_names_them_all():
    with pytest.raises(ValueError, match="unknown backend") as exc:
        clean("x", backend="bogus")
    for name in ("ollama", "claude-code", "opencode", "claude"):
        assert name in str(exc.value)


def test_claude_code_missing_cli_explains_how_to_fix(monkeypatch):
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: None)
    with pytest.raises(RuntimeError, match="Claude Code CLI") as exc:
        clean("hello", backend="claude-code")
    assert "VNOTE_CLAUDE_CODE_BIN" in str(exc.value)


def _fake_run(recorder, *, stdout="TITLE: T\n---\nbody", returncode=0, stderr=""):
    def run(cmd, **kwargs):
        recorder["cmd"] = cmd
        recorder["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run


def test_claude_code_disables_tools_and_pipes_prompt_on_stdin(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec))

    result = clean("um so the parser broke", mode="edit", backend="claude-code")

    assert rec["cmd"][:2] == ["/usr/bin/claude", "-p"]
    # Tools off: a pure text transform needs no filesystem/network access.
    assert rec["cmd"][rec["cmd"].index("--allowed-tools") + 1] == ""
    assert "--system-prompt" in rec["cmd"]
    # No --model unless asked: don't pin the user's subscription to one model.
    assert "--model" not in rec["cmd"]
    # Transcript travels on stdin, not argv (argv caps out on long notes).
    assert "um so the parser broke" in rec["input"]
    assert not any("um so the parser broke" in part for part in rec["cmd"])
    assert (result.title, result.body) == ("T", "body")


def test_claude_code_passes_model_only_when_given(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec))
    clean("x", backend="claude-code", model="claude-opus-5")
    assert rec["cmd"][rec["cmd"].index("--model") + 1] == "claude-opus-5"


def test_claude_code_dictation_mode_returns_plain_text(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec, stdout="cleaned words\n"))
    result = clean("raw words", mode="dictation", backend="claude-code")
    assert result.body == "cleaned words"  # no TITLE framing parsed in dictation mode


def test_claude_code_nonzero_exit_surfaces_stderr(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(
        cleanup.subprocess, "run", _fake_run(rec, returncode=1, stdout="", stderr="not logged in")
    )
    with pytest.raises(RuntimeError, match="not logged in"):
        clean("x", backend="claude-code")


def test_claude_code_empty_output_is_an_error(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec, stdout="   \n"))
    with pytest.raises(RuntimeError, match="empty response"):
        clean("x", backend="claude-code")


def test_claude_code_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr(cleanup.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        clean("x", backend="claude-code")


# --- opencode backend (whatever provider/model opencode is configured with) ---


def _oc_stream(*events: dict) -> str:
    """opencode's `--format json` output: one JSON event per line."""
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _oc_text(text: str) -> dict:
    return {"type": "text", "part": {"type": "text", "text": text}}


def _fake_oc_run(recorder, *, stdout=None, returncode=0, stderr=""):
    """Stand in for the opencode CLI, capturing the sandbox it was pointed at.

    The agent file is read *during* the call because the sandbox is a
    TemporaryDirectory — by the time the test body resumes it is gone.
    """
    if stdout is None:
        stdout = _oc_stream(_oc_text("TITLE: T\n---\nbody"))

    def run(cmd, **kwargs):
        recorder["cmd"] = cmd
        recorder["input"] = kwargs.get("input")
        sandbox = Path(cmd[cmd.index("--dir") + 1])
        recorder["agent"] = (sandbox / ".opencode" / "agent" / "vnote.md").read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run


def test_opencode_missing_cli_explains_how_to_fix(monkeypatch):
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: None)
    with pytest.raises(RuntimeError, match="opencode CLI") as exc:
        clean("hello", backend="opencode")
    assert "VNOTE_OPENCODE_BIN" in str(exc.value)


def test_opencode_runs_a_tool_free_agent_in_a_sandbox(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_oc_run(rec))

    result = clean("um so the parser broke", mode="edit", backend="opencode")

    assert rec["cmd"][0] == "/usr/bin/opencode"
    assert "run" in rec["cmd"]
    assert rec["cmd"][rec["cmd"].index("--agent") + 1] == "vnote"
    # JSON, not the pretty stream: it is parseable and it separates `text` parts
    # from a thinking model's `reasoning` parts.
    assert rec["cmd"][rec["cmd"].index("--format") + 1] == "json"
    # Tools off: a pure text transform needs no filesystem/network access.
    for tool in ("write", "edit", "bash", "read"):
        assert f"{tool}: false" in rec["agent"]
    # The agent body carries vnote's own system prompt (opencode has no flag for it).
    assert "TITLE:" in rec["agent"]
    # No --model unless asked: don't override the user's opencode config.
    assert "--model" not in rec["cmd"]
    # Transcript travels on stdin, not argv (argv caps out on long notes).
    assert "um so the parser broke" in rec["input"]
    assert not any("um so the parser broke" in part for part in rec["cmd"])
    assert (result.title, result.body) == ("T", "body")


def test_opencode_runs_in_a_scratch_dir_not_the_users_project(monkeypatch, tmp_path):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_oc_run(rec))
    monkeypatch.chdir(tmp_path)
    clean("x", backend="opencode")
    sandbox = Path(rec["cmd"][rec["cmd"].index("--dir") + 1])
    assert sandbox != tmp_path and tmp_path not in sandbox.parents
    # …and it is cleaned up once the call returns.
    assert not sandbox.exists()


def test_opencode_passes_model_only_when_given(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_oc_run(rec))
    clean("x", backend="opencode", model="local/qwen")
    assert rec["cmd"][rec["cmd"].index("--model") + 1] == "local/qwen"


def test_opencode_model_falls_back_to_the_saved_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_OPENCODE_MODEL", raising=False)
    config.save_config({"backend": "opencode", "opencode_model": "local/saved"})
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_oc_run(rec))
    clean("x", backend="opencode")
    assert rec["cmd"][rec["cmd"].index("--model") + 1] == "local/saved"


def test_opencode_pure_is_on_by_default_and_can_be_turned_off(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_oc_run(rec))
    monkeypatch.delenv("VNOTE_OPENCODE_PURE", raising=False)
    clean("x", backend="opencode")
    assert "--pure" in rec["cmd"]
    # Escape hatch for users whose provider comes from an opencode plugin.
    monkeypatch.setenv("VNOTE_OPENCODE_PURE", "0")
    clean("x", backend="opencode")
    assert "--pure" not in rec["cmd"]


def test_opencode_joins_text_parts_and_ignores_reasoning_and_noise(monkeypatch):
    rec: dict = {}
    stream = (
        "opencode banner line\n"  # not an event; must not break the parse
        + _oc_stream(
            {"type": "step_start", "part": {}},
            {"type": "reasoning", "part": {"type": "reasoning", "text": "SECRET SCRATCHPAD"}},
            _oc_text("TITLE: Split\n---\nfirst "),
            _oc_text("second"),
            {"type": "step_finish", "part": {}},
        )
    )
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_oc_run(rec, stdout=stream))
    result = clean("x", backend="opencode")
    assert (result.title, result.body) == ("Split", "first second")
    assert "SECRET" not in result.body


def test_opencode_dictation_mode_returns_plain_text(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(
        cleanup.subprocess, "run", _fake_oc_run(rec, stdout=_oc_stream(_oc_text("cleaned words\n")))
    )
    result = clean("raw words", mode="dictation", backend="opencode")
    assert result.body == "cleaned words"  # no TITLE framing parsed in dictation mode


def test_opencode_nonzero_exit_surfaces_stderr(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(
        cleanup.subprocess, "run",
        _fake_oc_run(rec, returncode=1, stdout="", stderr="no provider configured"),
    )
    with pytest.raises(RuntimeError, match="no provider configured"):
        clean("x", backend="opencode")


def test_opencode_stream_without_text_parts_is_an_error(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")
    monkeypatch.setattr(
        cleanup.subprocess, "run",
        _fake_oc_run(rec, stdout=_oc_stream({"type": "step_finish", "part": {}})),
    )
    with pytest.raises(RuntimeError, match="empty response"):
        clean("x", backend="opencode")


def test_opencode_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(cleanup, "opencode_bin", lambda: "/usr/bin/opencode")

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr(cleanup.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        clean("x", backend="opencode")
