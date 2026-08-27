"""Tests for the .env loader, the config file, and setting resolution order."""

import os
from pathlib import Path

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


# --- the settings registry ------------------------------------------------------


def test_registry_keys_and_env_names_are_unique():
    keys = [s.key for s in config.SETTINGS]
    envs = [s.env for s in config.SETTINGS]
    assert len(set(keys)) == len(keys)
    assert len(set(envs)) == len(envs)


def test_get_and_source_follow_env_over_file_over_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_STYLE", raising=False)
    monkeypatch.delenv("VNOTE_MODE", raising=False)  # the retired name resolves too (see get())
    assert config.get("default_style") == "edit"
    assert config.source("default_style") == "default"
    config.save_config({"default_style": "summary"})
    assert config.get("default_style") == "summary"
    assert config.source("default_style") == "file"
    monkeypatch.setenv("VNOTE_STYLE", "light")
    assert config.get("default_style") == "light"
    assert config.source("default_style") == "env"
    assert config.default_style() == "light"


def test_the_retired_mode_names_still_select_the_style(tmp_path, monkeypatch):
    """0.6.x wrote default_mode / VNOTE_MODE; both still pick the style, and are never written back."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_STYLE", raising=False)
    monkeypatch.delenv("VNOTE_MODE", raising=False)
    config.save_config({"default_mode": "summary"})
    assert config.default_style() == "summary" and config.source("default_style") == "file"
    config.save_config({"default_mode": "summary", "default_style": "light"})
    assert config.default_style() == "light"  # the current name wins
    monkeypatch.setenv("VNOTE_MODE", "dictation")
    assert config.default_style() == "dictation" and config.source("default_style") == "env"
    monkeypatch.setenv("VNOTE_STYLE", "email")
    assert config.default_style() == "email"


def test_update_clears_the_retired_key_so_blank_really_means_default(tmp_path, monkeypatch):
    """An upgraded config.json still holds default_mode; leaving it there would keep
    winning after a write, and 'back to default' would silently do nothing."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_MODE", raising=False)
    config.save_config({"default_mode": "summary", "language": "en"})

    config.update({"default_style": "light"})
    assert config.load_config() == {"default_style": "light", "language": "en"}
    assert config.default_style() == "light"

    config.save_config({"default_mode": "summary", "default_style": "light"})
    config.update({"default_style": ""})  # blank = back to the built-in default
    assert config.load_config() == {}
    assert config.default_style() == config.DEFAULT_STYLE
    assert config.source("default_style") == "default"


def test_update_refuses_a_write_the_retired_env_var_would_swallow(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VNOTE_MODE", "dictation")
    assert config.source("default_style") == "env"
    with pytest.raises(ValueError, match="overridden by VNOTE_MODE"):
        config.update({"default_style": "light"})
    assert not config.config_file().exists()


def test_update_validates_the_style_against_the_registry(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.update({"default_style": "summary"}) == ["default_style"]
    with pytest.raises(ValueError, match="default_style must be one of"):
        config.update({"default_style": "no-such-style"})


def test_language_blank_means_auto(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_LANGUAGE", raising=False)
    assert config.language() is None
    config.save_config({"language": "en"})
    assert config.language() == "en"


def test_update_persists_editable_keys_and_blank_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for env in ("VNOTE_OLLAMA_MODEL", "VNOTE_LANGUAGE"):
        monkeypatch.delenv(env, raising=False)
    assert config.update({"ollama_model": "llama3.2:3b", "language": "en"}) == ["ollama_model", "language"]
    assert config.load_config() == {"ollama_model": "llama3.2:3b", "language": "en"}
    assert config.ollama_model() == "llama3.2:3b"
    config.update({"language": ""})
    assert config.load_config() == {"ollama_model": "llama3.2:3b"}


def test_update_rejects_bad_requests(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="unknown setting"):
        config.update({"nope": "x"})
    with pytest.raises(ValueError, match="restart"):
        config.update({"whisper_model": "tiny"})
    with pytest.raises(ValueError, match="must be one of"):
        config.update({"backend": "gpt"})
    monkeypatch.setenv("VNOTE_BACKEND", "claude")
    with pytest.raises(ValueError, match="overridden by VNOTE_BACKEND"):
        config.update({"backend": "ollama"})
    assert not config.config_file().exists()  # nothing was written on any error path


def test_describe_rows_carry_the_contract_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rows = {row["key"]: row for row in config.describe()}
    assert set(rows) == {s.key for s in config.SETTINGS}
    for row in rows.values():
        assert {"key", "env", "value", "default", "description", "kind", "source", "editable"} <= set(row)
        assert row["source"] in ("env", "file", "default")
    assert rows["backend"]["choices"] == ["ollama", "claude-code", "claude"]
    # default_style has no fixed list: the choices are whatever the style files say
    from vnote import styles

    assert rows["default_style"]["choices"] == styles.names()
    assert "edit" in rows["default_style"]["choices"]
    assert rows["whisper_model"]["editable"] is False
    assert rows["whisper_model"]["value"] == config.WHISPER_MODEL  # the live constant, not a re-read
    assert rows["daemon_port"]["value"] == config.DAEMON_PORT


def test_every_setting_env_var_is_documented():
    root = Path(__file__).parent.parent
    guide = (root / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    for s in config.SETTINGS:
        assert f"`{s.env}`" in guide, f"{s.env} missing from the User Guide env table"
        assert s.env in example, f"{s.env} missing from .env.example"



def test_update_blank_or_null_restores_the_default_for_any_kind():
    config.save_config({"backend": "claude", "default_style": "summary", "language": "en"})
    config.update({"backend": "", "default_style": None})
    assert config.load_config() == {"language": "en"}
    assert config.backend() == "ollama" and config.source("backend") == "default"


def test_update_rejects_ollama_host_without_a_scheme():
    import pytest

    with pytest.raises(ValueError, match="http://"):
        config.update({"ollama_host": "localhost:11434"})
    assert config.update({"ollama_host": "http://gpu-box:11434"}) == ["ollama_host"]


def test_update_checks_the_keep_alive_duration(tmp_path, monkeypatch):
    """Ollama 400s on a unit-less duration string; a bare number is seconds (-1 = forever)."""
    import pytest

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_OLLAMA_KEEP_ALIVE", raising=False)
    for good in ("-1", "30m", "1h30m", "300s", "0"):
        assert config.update({"ollama_keep_alive": good}) == ["ollama_keep_alive"]
    for bad in ("forever", "30 minutes", "m30"):
        with pytest.raises(ValueError, match="ollama_keep_alive must be a duration"):
            config.update({"ollama_keep_alive": bad})


def test_file_values_are_coerced_not_trusted():
    config.save_config({"default_style": 5, "language": 7})
    assert config.get("default_style") == "5"  # a string, so consumers can validate and name it
    assert config.language() == "7"


def test_daemon_port_env_garbage_does_not_kill_import(monkeypatch, capsys):
    monkeypatch.setenv("VNOTE_DAEMON_PORT", "abc")
    assert config._int_env("VNOTE_DAEMON_PORT", 8760) == 8760
    assert "VNOTE_DAEMON_PORT" in capsys.readouterr().err


def test_vocab_default_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    row = next(r for r in config.describe() if r["key"] == "vocab")
    assert row["default"] == row["value"] == str(tmp_path / "elsewhere" / "vnote" / "vocab.txt")
