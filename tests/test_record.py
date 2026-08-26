"""Tests for capture-backend selection and ffmpeg failure reporting."""

import pytest

from vnote import record


def test_macos_never_selects_the_pulse_ffmpeg_backend(monkeypatch):
    """ffmpeg's `-f pulse` is Linux-only. With Homebrew ffmpeg on PATH and no
    parec/pw-record, macOS must fall through to sounddevice — selecting ffmpeg
    makes every recording abort at 0.2s with 'Nothing recorded (too short)'."""
    monkeypatch.setattr(record.sys, "platform", "darwin")
    monkeypatch.setattr(record.shutil, "which", lambda tool: "/opt/homebrew/bin/ffmpeg" if tool == "ffmpeg" else None)

    assert record.selected_backend() == "sounddevice"


def test_linux_still_uses_ffmpeg_when_no_pulse_cli_is_present(monkeypatch):
    monkeypatch.setattr(record.sys, "platform", "linux")
    monkeypatch.setattr(record.shutil, "which", lambda tool: "/usr/bin/ffmpeg" if tool == "ffmpeg" else None)

    assert record.selected_backend() == "ffmpeg"


def test_parec_still_wins_on_wsl(monkeypatch):
    monkeypatch.setattr(record.sys, "platform", "linux")
    monkeypatch.setattr(record.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    assert record.selected_backend() == "parec"


def test_ffmpeg_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A dead ffmpeg used to surface as a bare 'nothing recorded'; the stderr
    line ('Unknown input format: pulse') is the entire diagnosis."""
    # Never set `stop`: the real path is the user still holding Enter while ffmpeg
    # dies on its own, so the timer loop exits via proc.poll(), not a SIGINT.
    monkeypatch.setattr(record, "_wait_for_enter", lambda stop: None)
    monkeypatch.setattr(
        record,
        "_ffmpeg_cmd",
        lambda dest: ["sh", "-c", "echo \"Unknown input format: 'pulse'\" >&2; exit 234"],
    )

    with pytest.raises(RuntimeError, match="Unknown input format"):
        record._record_via_ffmpeg(tmp_path / "out.wav")
