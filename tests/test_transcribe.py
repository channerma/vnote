"""Tests for device selection in the transcription path."""

import sys

from vnote import config, transcribe


def test_cuda_is_not_attempted_on_macos(monkeypatch):
    """CTranslate2 publishes no CUDA-enabled macOS wheel, so the attempt can only
    ever fail — and it printed "GPU init failed" on stderr for every transcription."""
    monkeypatch.setattr(sys, "platform", "darwin")

    assert transcribe._cuda_plausible() is False


def test_cuda_is_still_attempted_off_macos(monkeypatch):
    """A Linux box with a broken CUDA stack should still be told about it."""
    monkeypatch.setattr(sys, "platform", "linux")

    assert transcribe._cuda_plausible() is True


def test_model_default_follows_the_device(monkeypatch, tmp_path):
    """No single default suits both machines: CPU wants speed, CUDA gets accuracy free."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no pinned model anywhere
    monkeypatch.delenv("VNOTE_WHISPER_MODEL", raising=False)

    assert config.whisper_model("cpu") == "small"
    assert config.whisper_model("cuda") == "large-v3-turbo"
    assert config.whisper_model_override() is None


def test_an_explicit_model_overrides_both_devices(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VNOTE_WHISPER_MODEL", "base")

    assert config.whisper_model("cpu") == "base"
    assert config.whisper_model("cuda") == "base"
    assert config.whisper_model_override() == "base"


def test_config_file_can_pin_the_model(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VNOTE_WHISPER_MODEL", raising=False)
    config.save_config({"whisper_model": "medium"})

    assert config.whisper_model("cuda") == "medium"
