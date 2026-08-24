"""Tests for the transcript-cleanup prompts, parser and backend dispatch (no network)."""

import subprocess

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


# --- dictation mode (plain-text output, no title framing) ------------------


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


def test_clean_rejects_unknown_backend_and_names_all_three():
    with pytest.raises(ValueError, match="unknown backend") as exc:
        clean("x", backend="bogus")
    for name in ("ollama", "claude-code", "claude"):
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
