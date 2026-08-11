"""Tests for argument parsing (no audio, no models touched)."""

from vnote import cli, config
from vnote.cli import _parse_args


def test_defaults():
    a = _parse_args([])
    assert a.mode == "edit"
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


def test_backend_and_audio_and_flags():
    a = _parse_args(["memo.m4a", "--backend", "claude", "--raw", "--no-clipboard"])
    assert a.audio == "memo.m4a"
    assert a.backend == "claude"
    assert a.raw is True
    assert a.no_clipboard is True


# --- --promote -------------------------------------------------------------------


def test_promote_flag_in_process(monkeypatch, tmp_path, capsys):
    from datetime import datetime

    from vnote import cli, daemon, history, output

    monkeypatch.setattr(daemon, "is_up", lambda timeout=0.3: False)  # force in-process path
    monkeypatch.setattr(history, "NOTES_DIR", tmp_path)
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    history.append_take(raw="promote me please and thanks", clean=None, wav=None,
                        seconds=1.0, mode=None, tone=None, when=datetime(2026, 7, 6, 9, 0, 0))
    assert cli.main(["--promote"]) == 0
    assert "promoted" in capsys.readouterr().err
    assert any(p.name.startswith("2026-07-06-0900-") for p in tmp_path.iterdir())


def test_promote_flag_error_exits_nonzero(monkeypatch, tmp_path, capsys):
    from vnote import cli, daemon, history, output

    monkeypatch.setattr(daemon, "is_up", lambda timeout=0.3: False)
    monkeypatch.setattr(history, "NOTES_DIR", tmp_path)
    monkeypatch.setattr(output, "NOTES_DIR", tmp_path)
    assert cli.main(["--promote"]) == 1
    assert "error:" in capsys.readouterr().err


def test_resolved_model_per_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_OLLAMA_MODEL", raising=False)
    # An explicit --model always wins.
    assert cli._resolved_model("claude-code", "claude-opus-5") == "claude-opus-5"
    assert cli._resolved_model("ollama", None) == config.ollama_model()
    assert cli._resolved_model("claude", None) == config.CLAUDE_MODEL
    # claude-code is deliberately unpinned — the CLI picks the model.
    assert "claude-code" in cli._resolved_model("claude-code", None)
