"""Tests for the .env loader, the config file, and setting resolution order."""

import os

from vnote import config


def test_dotenv_loads_new_but_never_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("VNOTE_DOTENV_EXISTING", "keep-me")
    env_file = tmp_path / ".env"
    env_file.write_text(
        'VNOTE_DOTENV_NEW="from-file"\n'
        "VNOTE_DOTENV_EXISTING=should-not-win\n"
        "# a comment\n"
        "BARE_LINE_NO_EQUALS\n"
    )
    config._load_dotenv(env_file)

    assert os.environ["VNOTE_DOTENV_NEW"] == "from-file"  # quotes stripped
    assert os.environ["VNOTE_DOTENV_EXISTING"] == "keep-me"  # real env wins

    monkeypatch.delenv("VNOTE_DOTENV_NEW", raising=False)  # clean up the manual os.environ write


def test_load_config_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.load_config() == {}


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save_config({"backend": "claude", "ollama_model": "x"})
    assert config.load_config() == {"backend": "claude", "ollama_model": "x"}
    assert config.config_file().parent.name == "vnote"


def test_backend_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_BACKEND", raising=False)

    # 1. nothing set -> built-in
    assert config.backend() == config.BUILTIN_BACKEND

    # 2. config file overrides built-in
    config.save_config({"backend": "claude"})
    assert config.backend() == "claude"

    # 3. env var beats the file
    monkeypatch.setenv("VNOTE_BACKEND", "ollama")
    assert config.backend() == "ollama"


def test_ollama_model_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_OLLAMA_MODEL", raising=False)

    assert config.ollama_model() == config.BUILTIN_OLLAMA_MODEL
    config.save_config({"ollama_model": "qwen2.5:7b-instruct"})
    assert config.ollama_model() == "qwen2.5:7b-instruct"
    monkeypatch.setenv("VNOTE_OLLAMA_MODEL", "llama3.2:3b")
    assert config.ollama_model() == "llama3.2:3b"


def test_app_tone_matching(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.app_tones() == {}  # no config file -> empty map
    assert config.app_tone_for("anything") is None

    config.save_config({"app_tones": {"slack": "casual", "outlook": "formal"}})
    assert config.app_tone_for("#eng-general - Slack") == "casual"
    assert config.app_tone_for("Inbox - Outlook") == "formal"
    assert config.app_tone_for("Notepad") is None


def test_env_on_defaults_on_and_honors_off_values(monkeypatch):
    from vnote import config as cfg

    monkeypatch.delenv("VNOTE_HISTORY_AUDIO", raising=False)
    assert cfg._env_on("VNOTE_HISTORY_AUDIO") is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("VNOTE_HISTORY_AUDIO", off)
        assert cfg._env_on("VNOTE_HISTORY_AUDIO") is False
    monkeypatch.setenv("VNOTE_HISTORY_AUDIO", "1")
    assert cfg._env_on("VNOTE_HISTORY_AUDIO") is True


def test_default_notes_dir_uses_repo_root_in_a_source_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "vnote").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(config, "__file__", str(repo / "vnote" / "config.py"))

    assert config._default_notes_dir() == (repo / "voice-notes").resolve()


def test_default_notes_dir_never_lands_in_site_packages(tmp_path, monkeypatch):
    """Installed as a tool there is no pyproject.toml beside the package, and the
    repo-relative path would resolve *inside* site-packages. Anchor to ~ instead."""
    site = tmp_path / "site-packages"
    (site / "vnote").mkdir(parents=True)  # deliberately no pyproject.toml
    home = tmp_path / "home"
    monkeypatch.setattr(config, "__file__", str(site / "vnote" / "config.py"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    notes = config._default_notes_dir()
    assert notes == home / "voice-notes"
    assert site.resolve() not in notes.parents
