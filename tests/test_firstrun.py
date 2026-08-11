"""Tests for first-run setup gating and VRAM-based model suggestion."""

from vnote import config, firstrun


def test_suggest_tier_by_vram():
    assert firstrun._suggest_tier(None) == 1  # unknown GPU -> middle tier
    assert firstrun._suggest_tier(24) == 0    # plenty -> 14b
    assert firstrun._suggest_tier(8) == 1     # mid -> 7b
    assert firstrun._suggest_tier(4) == 2     # small -> 3b
    assert firstrun._suggest_tier(1) == 2     # tiny -> still the smallest tier


def test_should_run_false_when_backend_flag_given(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert firstrun.should_run("ollama") is False


def test_should_run_false_when_env_forces_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VNOTE_BACKEND", "ollama")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert firstrun.should_run(None) is False


def test_should_run_false_when_config_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_BACKEND", raising=False)
    config.save_config({"backend": "ollama"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert firstrun.should_run(None) is False


def test_should_run_false_when_not_a_tty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_BACKEND", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert firstrun.should_run(None) is False


def test_should_run_true_when_interactive_and_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_BACKEND", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert firstrun.should_run(None) is True


# --- backend picker ----------------------------------------------------------


def _capture_ask(monkeypatch, seen: dict):
    """Replace the menu with one that records its args and takes the default."""

    def fake_ask(prompt, options, default):
        seen.setdefault("calls", []).append({"options": options, "default": default})
        return default

    monkeypatch.setattr(firstrun, "_ask", fake_ask)


def _interactive(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_BACKEND", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(firstrun, "_detect_vram_gb", lambda: 8.0)


def test_setup_offers_all_three_backends(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    monkeypatch.setattr(firstrun, "claude_code_available", lambda: True)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    labels = " ".join(seen["calls"][0]["options"]).lower()
    assert "claude code" in labels and "ollama" in labels and "anthropic api" in labels


def test_setup_defaults_to_claude_code_when_cli_is_installed(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    monkeypatch.setattr(firstrun, "claude_code_available", lambda: True)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    assert seen["calls"][0]["default"] == 0
    assert config.load_config()["backend"] == "claude-code"


def test_setup_falls_back_to_ollama_when_cli_is_missing(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    monkeypatch.setattr(firstrun, "claude_code_available", lambda: False)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    # Never steer a machine toward a CLI it doesn't have.
    assert seen["calls"][0]["default"] == 1
    cfg = config.load_config()
    assert cfg["backend"] == "ollama" and cfg["ollama_model"]
