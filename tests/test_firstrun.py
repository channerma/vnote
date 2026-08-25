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


def _clis(monkeypatch, *, claude_code: bool, opencode: bool) -> None:
    """Pin which agent CLIs "exist".

    Both must be stubbed in every chooser test: unstubbed, the suggested default
    would depend on what happens to be installed on the machine running the
    suite, so the same test would pass on a laptop and fail in CI.
    """
    monkeypatch.setattr(firstrun, "claude_code_available", lambda: claude_code)
    monkeypatch.setattr(firstrun, "opencode_available", lambda: opencode)
    monkeypatch.setattr(firstrun, "opencode_models", lambda: [])


def test_setup_offers_all_four_backends(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    _clis(monkeypatch, claude_code=True, opencode=True)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    labels = " ".join(seen["calls"][0]["options"]).lower()
    for name in ("claude code", "opencode", "ollama", "anthropic api"):
        assert name in labels


def test_setup_defaults_to_claude_code_when_cli_is_installed(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    _clis(monkeypatch, claude_code=True, opencode=True)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    # Claude Code outranks opencode when both are present.
    assert seen["calls"][0]["default"] == 0
    assert config.load_config()["backend"] == "claude-code"


def test_setup_suggests_opencode_when_it_is_the_only_cli(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    _clis(monkeypatch, claude_code=False, opencode=True)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    assert seen["calls"][0]["default"] == 1
    cfg = config.load_config()
    # No model pinned by default: vnote follows opencode's own configured choice.
    assert cfg["backend"] == "opencode" and "opencode_model" not in cfg


def test_setup_pins_an_opencode_model_only_when_one_is_chosen(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    _clis(monkeypatch, claude_code=False, opencode=True)
    monkeypatch.setattr(firstrun, "opencode_models", lambda: ["local/a", "local/b"])
    # Answer the backend menu with its default, then pick the 2nd listed model.
    answers = iter([1, 2])
    monkeypatch.setattr(firstrun, "_ask", lambda prompt, options, default: next(answers))
    firstrun.run(None, force=True)
    assert config.load_config()["opencode_model"] == "local/b"


def test_setup_falls_back_to_ollama_when_no_cli_is_installed(tmp_path, monkeypatch):
    _interactive(monkeypatch, tmp_path)
    _clis(monkeypatch, claude_code=False, opencode=False)
    seen: dict = {}
    _capture_ask(monkeypatch, seen)
    firstrun.run(None, force=True)
    # Never steer a machine toward a CLI it doesn't have.
    assert seen["calls"][0]["default"] == 2
    cfg = config.load_config()
    assert cfg["backend"] == "ollama" and cfg["ollama_model"]
