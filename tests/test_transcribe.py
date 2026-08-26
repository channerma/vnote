"""Tests for device selection in the transcription path."""

import sys

from vnote import transcribe


def test_cuda_is_not_attempted_on_macos(monkeypatch):
    """CTranslate2 publishes no CUDA-enabled macOS wheel, so the attempt can only
    ever fail — and it printed "GPU init failed" on stderr for every transcription."""
    monkeypatch.setattr(sys, "platform", "darwin")

    assert transcribe._cuda_plausible() is False


def test_cuda_is_still_attempted_off_macos(monkeypatch):
    """A Linux box with a broken CUDA stack should still be told about it."""
    monkeypatch.setattr(sys, "platform", "linux")

    assert transcribe._cuda_plausible() is True
