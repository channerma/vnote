"""Tests for argument parsing (no audio, no models touched)."""

from vnote import cli, config
from vnote.cli import _parse_args


def test_defaults():
    a = _parse_args([])
    assert a.mode is None  # resolved in main(): flag > saved default_mode > edit
    assert a.backend is None  # resolved later from saved choice / env / built-in
    assert a.raw is False
    assert a.no_clipboard is False
    assert a.audio is None
    assert a.serve is False
    assert a.no_daemon is False


def test_mode_flags_are_mutually_exclusive_values():
    assert _parse_args(["--light"]).mode == "light"
    assert _parse_args(["--summary"]).mode == "summary"
    assert _parse_args(["--edit"]).mode == "edit"
    assert _parse_args(["--dictation"]).mode == "dictation"


def test_backend_and_audio_and_flags():
    a = _parse_args(["memo.m4a", "--backend", "claude", "--raw", "--no-clipboard"])
    assert a.audio == "memo.m4a"
    assert a.backend == "claude"
    assert a.raw is True
    assert a.no_clipboard is True


def test_resolved_model_per_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_OLLAMA_MODEL", raising=False)
    # An explicit --model always wins.
    assert cli._resolved_model("claude-code", "claude-opus-5") == "claude-opus-5"
    assert cli._resolved_model("ollama", None) == config.ollama_model()
    assert cli._resolved_model("claude", None) == config.get("claude_model")
    # claude-code is deliberately unpinned — the CLI picks the model.
    assert "claude-code" in cli._resolved_model("claude-code", None)



def test_bad_saved_mode_is_a_clear_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("VNOTE_MODE", "bogus")
    assert cli.main([]) == 2  # fails before any recording is attempted
    err = capsys.readouterr().err
    assert "unknown cleanup mode 'bogus'" in err and "VNOTE_MODE" in err
    assert cli.main(["--config"]) == 0  # utility actions still run so you can see the bad value


def test_setup_keeps_other_saved_settings(monkeypatch, capsys):
    from vnote import firstrun

    config.save_config({"default_mode": "summary", "language": "en"})
    monkeypatch.setattr(firstrun, "claude_code_available", lambda: True)
    monkeypatch.setattr(firstrun, "_ask", lambda prompt, options, default: 0)  # pick claude-code
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    firstrun.run(None, force=True)
    assert config.load_config() == {"default_mode": "summary", "language": "en", "backend": "claude-code"}


def test_instructions_flag_parses():
    assert _parse_args([]).instructions is None
    assert _parse_args(["--instructions", "bullet points only"]).instructions == "bullet points only"
