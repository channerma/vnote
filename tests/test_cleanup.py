"""Tests for the transcript-cleanup prompts, parser and backend dispatch (no network)."""

import json
import subprocess
from pathlib import Path

import pytest

from vnote import cleanup, config, styles
from vnote.cleanup import _build_user_prompt, _finish, _parse_response, clean, revise


def _style(name: str):
    """The built-in style of that name — the prompt helpers take a Style, not a name."""
    return styles.get(name)


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


def test_build_user_prompt_includes_the_style_body_and_transcript():
    prompt = _build_user_prompt("hello there", _style("light"))
    assert "hello there" in prompt
    assert "filler" in prompt.lower()  # the 'light' instruction mentions filler words


# --- output: plain (no title framing) ---------------------------------------


def test_plain_output_finish_is_not_title_framed():
    r = _finish("TITLE: looks like a title\n---\nbut dictation takes it verbatim", "orig words here",
                _style("dictation"))
    assert r.body == "TITLE: looks like a title\n---\nbut dictation takes it verbatim"
    assert r.title == "orig words here"  # fallback title from the transcript; flow ignores it


def test_note_output_still_parses_title_framing():
    r = _finish("TITLE: A Note\n---\nbody", "x", _style("edit"))
    assert (r.title, r.body) == ("A Note", "body")


def test_dictation_prompt_mentions_spoken_commands():
    prompt = _build_user_prompt("x", _style("dictation"))
    assert "scratch that" in prompt


def test_tone_lands_in_the_prompt():
    assert "Write in a casual tone." in _build_user_prompt("x", _style("dictation"), tone="casual")
    assert "tone" not in _build_user_prompt("x", _style("light"))  # no tone -> no tone sentence


def test_clean_rejects_an_unknown_style():
    with pytest.raises(ValueError, match="unknown style"):
        clean("x", mode="bogus")


# --- the style decides the prompt, the contract and the model ------------------


def _record_complete(monkeypatch):
    """Capture what clean() hands the backend, without running one."""
    rec: dict = {}

    def fake(backend, system, user, model):
        rec.update(backend=backend, system=system, user=user, model=model)
        return "TITLE: T\n---\nbody"

    monkeypatch.setattr(cleanup, "_complete", fake)
    return rec


def test_clean_uses_the_style_body_and_the_note_contract(monkeypatch):
    rec = _record_complete(monkeypatch)
    clean("transcript here", mode="summary")
    assert styles.get("summary").body in rec["user"]
    assert "TITLE:" in rec["system"]  # output: note keeps the title contract


def test_a_plain_style_gets_the_plain_preamble_and_no_title_parsing(monkeypatch):
    rec = _record_complete(monkeypatch)
    result = clean("orig words here", mode="dictation")
    assert "no title line" in rec["system"] and "TITLE:" not in rec["system"]
    assert result.body == "TITLE: T\n---\nbody"  # taken verbatim, not parsed
    assert result.title == "orig words here"      # derived from the transcript instead


def test_backend_and_model_precedence_is_explicit_then_style_then_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    styles.write("housestyle", "---\nbackend: claude-code\nmodel: tiny:1b\n---\nDo it.")
    rec = _record_complete(monkeypatch)

    clean("x", mode="housestyle")
    assert (rec["backend"], rec["model"]) == ("claude-code", "tiny:1b")  # the style's own lines

    clean("x", mode="housestyle", backend="ollama", model="big:14b")
    assert (rec["backend"], rec["model"]) == ("ollama", "big:14b")  # an explicit pick wins

    clean("x", mode="edit")  # a style with neither: the settings decide
    assert (rec["backend"], rec["model"]) == (config.backend(), None)
    styles._invalidate()


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


def test_claude_code_plain_style_returns_plain_text(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec, stdout="cleaned words\n"))
    result = clean("raw words", mode="dictation", backend="claude-code")
    assert result.body == "cleaned words"  # no TITLE framing parsed for output: plain


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


# --- free-text instructions on cleanup ---------------------------------------


def test_instructions_land_at_the_end_of_the_user_prompt():
    prompt = _build_user_prompt("x", _style("edit"), instructions="keep the numbers exact")
    assert prompt.rstrip().endswith("keep the numbers exact")
    assert "take precedence" in prompt


def test_instructions_work_for_a_plain_style_too():
    prompt = _build_user_prompt("x", _style("dictation"), instructions="british spelling")
    assert "british spelling" in prompt


def test_no_instructions_paragraph_when_none_or_blank():
    assert "Additional instructions" not in _build_user_prompt("x", _style("edit"))
    assert "Additional instructions" not in _build_user_prompt("x", _style("edit"), instructions="   ")


def test_instructions_reach_the_backend_prompt(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec))
    clean("raw words", backend="claude-code", instructions="cut the preamble")
    assert "cut the preamble" in rec["input"]


# --- revise (instruction applied to an existing note) -------------------------


def test_revise_builds_revise_prompts_and_strips_the_heading(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec))

    result = revise("# My Great Note\n\nFirst line.", "make it shorter", backend="claude-code")

    system = rec["cmd"][rec["cmd"].index("--system-prompt") + 1]
    assert "revise" in system.lower()
    assert "INSTRUCTION:\nmake it shorter" in rec["input"]
    assert "First line." in rec["input"]
    assert "# My Great Note" not in rec["input"]  # heading stripped; it round-trips as the title
    assert (result.title, result.body) == ("T", "body")


def test_revise_falls_back_to_the_existing_heading_as_title(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(cleanup, "claude_code_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cleanup.subprocess, "run", _fake_run(rec, stdout="just a revised body"))

    result = revise("# Parser Notes\n\nold body", "make it shorter", backend="claude-code")
    assert result.title == "Parser Notes"
    assert result.body == "just a revised body"


def test_revise_rejects_blank_instruction_and_unknown_backend():
    with pytest.raises(ValueError):
        revise("# T\n\nbody", "   ")
    with pytest.raises(ValueError, match="unknown backend"):
        revise("# T\n\nbody", "shorter", backend="bogus")


def test_revise_ollama_uses_the_note_model(monkeypatch):
    """Revise is style-agnostic: no style's model: line can pull it onto a small model."""
    rec: dict = {}

    def fake(system, user, model):
        rec["model"] = model
        return "TITLE: T\n---\nbody"

    monkeypatch.setattr(cleanup, "_ollama_complete", fake)
    monkeypatch.setattr(cleanup, "ollama_model", lambda: "big:14b")

    assert revise("# T\n\nbody", "shorter").title == "T"
    assert rec["model"] == "big:14b"


# --- continue / merge (Phase 10 F: a new take against an existing note) --------


def test_continue_note_forbids_the_title_line_and_shows_the_note_as_context(monkeypatch):
    rec = _record_complete(monkeypatch)

    body = cleanup.continue_note("# Deploy Notes\n\nStep one.", "and then step two",
                                 mode="summary", backend="ollama")

    assert "no title line" in rec["system"] and "TITLE:" not in rec["system"]
    assert "never repeat it" in rec["system"].lower()
    assert styles.get("summary").body in rec["user"]  # the style is still the editing instruction
    assert "# Deploy Notes" in rec["user"] and "and then step two" in rec["user"]
    assert "context only" in rec["user"]
    assert body == "body"  # the reply is the continuation itself, not a titled note


def test_continue_note_drops_the_framings_a_model_reaches_for(monkeypatch):
    """The prompt forbids all three; appending any of them verbatim breaks the note."""
    replies = iter([
        "TITLE: A New Title\n---\nthe continuation",       # the ordinary cleanup contract
        "```markdown\nthe continuation\n```",               # a fence around the whole answer
        "# A New Heading\n\nthe continuation",              # a heading of its own
        "```\n# A New Heading\n\nthe continuation\n```",   # both at once
        "   \n",                                            # nothing usable at all
    ])

    def fake(backend, system, user, model):
        return next(replies)

    monkeypatch.setattr(cleanup, "_complete", fake)
    for _ in range(4):
        assert cleanup.continue_note("# T\n\nbody", "more words", mode="edit") == "the continuation"
    assert cleanup.continue_note("# T\n\nbody", "more words", mode="edit") == "more words"


def test_continue_note_resolves_the_backend_and_model_like_clean(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    styles.write("housestyle", "---\nbackend: claude-code\nmodel: tiny:1b\n---\nDo it.")
    rec = _record_complete(monkeypatch)

    cleanup.continue_note("# T\n\nbody", "more", mode="housestyle")
    assert (rec["backend"], rec["model"]) == ("claude-code", "tiny:1b")
    cleanup.merge_note("# T\n\nbody", "more", mode="housestyle", backend="ollama", model="big:14b")
    assert (rec["backend"], rec["model"]) == ("ollama", "big:14b")  # an explicit pick still wins
    styles._invalidate()


def test_merge_note_keeps_the_title_contract_and_sees_both_texts(monkeypatch):
    rec = _record_complete(monkeypatch)

    result = cleanup.merge_note("# Deploy Notes\n\nStep one.", "and then step two",
                                mode="summary", instructions="keep it tight")

    assert "TITLE:" in rec["system"] and "merg" in rec["system"].lower()
    assert "# Deploy Notes" in rec["user"] and "and then step two" in rec["user"]
    assert "keep it tight" in rec["user"]
    assert (result.title, result.body) == ("T", "body")  # the whole note, title and all


def test_a_plain_style_keeps_its_plain_contract_in_both(monkeypatch):
    rec = _record_complete(monkeypatch)
    cleanup.continue_note("plain body", "more", mode="dictation")
    assert "no title line" in rec["system"]
    result = cleanup.merge_note("plain body", "more", mode="dictation")
    assert "no title line" in rec["system"] and result.body == "TITLE: T\n---\nbody"


def test_continue_and_merge_reject_an_unknown_style():
    with pytest.raises(ValueError, match="unknown style"):
        cleanup.continue_note("# T\n\nbody", "more", mode="gone")
    with pytest.raises(ValueError, match="unknown style"):
        cleanup.merge_note("# T\n\nbody", "more", mode="gone")


# --- the Ollama HTTP payloads (no network: urlopen is faked) --------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_ollama(monkeypatch, body: bytes) -> list[dict]:
    """Bypass the readiness probes and record what gets POSTed to /api/chat."""
    posted: list[dict] = []
    monkeypatch.setattr(cleanup, "_ensure_ollama_running", lambda: None)
    monkeypatch.setattr(cleanup, "_ensure_model_present", lambda model: None)

    def fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data))
        return _FakeResponse(body)

    monkeypatch.setattr(cleanup.urllib.request, "urlopen", fake_urlopen)
    return posted


def test_ollama_complete_sends_keep_alive(monkeypatch):
    monkeypatch.setenv("VNOTE_OLLAMA_KEEP_ALIVE", "42m")
    posted = _capture_ollama(monkeypatch, json.dumps({"message": {"content": "hello"}}).encode())

    assert cleanup._ollama_complete("sys", "user", "big:14b") == "hello"
    assert posted[0]["keep_alive"] == "42m" == str(config.get("ollama_keep_alive"))


def test_preload_ollama_loads_the_model_with_an_empty_message_list(monkeypatch):
    monkeypatch.setenv("VNOTE_OLLAMA_KEEP_ALIVE", "30m")
    posted = _capture_ollama(monkeypatch, b"{}")

    cleanup.preload_ollama("big:14b")

    assert posted == [{"model": "big:14b", "messages": [], "keep_alive": "30m"}]


def test_keep_alive_numbers_go_out_as_json_numbers(monkeypatch):
    """Ollama parses a keep_alive *string* with Go's time.ParseDuration: "-1" is a 400,
    -1 the number is "until Ollama exits" (checked against Ollama 0.23.1, 2026-08-25)."""
    monkeypatch.setenv("VNOTE_OLLAMA_KEEP_ALIVE", "-1")
    posted = _capture_ollama(monkeypatch, json.dumps({"message": {"content": "hi"}}).encode())

    cleanup.preload_ollama("big:14b")
    cleanup._ollama_complete("sys", "user", "big:14b")

    assert posted[0]["keep_alive"] == -1 and isinstance(posted[0]["keep_alive"], int)
    assert posted[1]["keep_alive"] == -1 and isinstance(posted[1]["keep_alive"], int)

    monkeypatch.setenv("VNOTE_OLLAMA_KEEP_ALIVE", "30m")
    posted.clear()
    cleanup.preload_ollama("big:14b")
    assert posted[0]["keep_alive"] == "30m"  # a unit stays a string

