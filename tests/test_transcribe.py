"""Model loading: the daemon warms on a background thread while requests arrive."""

import threading
import time

from vnote import transcribe


def test_load_model_builds_once_under_concurrent_callers(monkeypatch):
    """A warm thread and a request must share one model, not build two (two copies in VRAM)."""
    builds: list[str] = []

    def slow_build(device: str):
        builds.append(device)
        time.sleep(0.05)  # long enough for the other thread to reach the lock
        return object()

    monkeypatch.setattr(transcribe, "_model", None)  # restored after the test
    monkeypatch.setattr(transcribe, "_device", None)
    monkeypatch.setattr(transcribe, "_preload_cuda_libs", lambda: None)
    monkeypatch.setattr(transcribe, "_build", slow_build)
    assert transcribe.is_warm() is False

    got: list[object] = []
    threads = [threading.Thread(target=lambda: got.append(transcribe._load_model())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert len(builds) == 1  # exactly one build
    # On a CUDA-less box (macOS) the load is deliberately CPU-only from the start.
    assert builds[0] == ("cpu" if not transcribe._cuda_plausible() else "cuda")
    assert got[0] is got[1] is transcribe._model
    assert transcribe.is_warm() is True
    assert transcribe._device == builds[0]


def test_cuda_is_not_attempted_on_macos(monkeypatch):
    """CTranslate2 publishes no CUDA-enabled macOS wheel, so the probe can only
    ever fail — and it printed "GPU init failed" on stderr for every transcription."""
    monkeypatch.setattr(transcribe.sys, "platform", "darwin")
    assert transcribe._cuda_plausible() is False


def test_cuda_is_still_attempted_off_macos(monkeypatch):
    """A Linux box with a broken CUDA stack should still be told about it."""
    monkeypatch.setattr(transcribe.sys, "platform", "linux")
    assert transcribe._cuda_plausible() is True
