"""Speech-to-text via faster-whisper. Uses CUDA when available, falls back to CPU."""

from __future__ import annotations

import ctypes
import glob
import os
import sys
import threading
from pathlib import Path

from . import config

_model = None  # lazily loaded, cached for the process
_device = None
_load_lock = threading.Lock()  # the daemon warms on a background thread while requests arrive


def _preload_cuda_libs() -> None:
    """dlopen the pip-installed NVIDIA libs (cuBLAS/cuDNN) so CTranslate2 finds them.

    faster-whisper's backend looks these up by SONAME at inference time; when they
    come from the ``nvidia-*-cu12`` wheels they live under ``site-packages/nvidia``
    and aren't on the loader path. Loading them RTLD_GLOBAL here fixes that.
    """
    try:
        import nvidia  # noqa: F401  (the namespace package from the wheels)
    except ImportError:
        return
    for base in nvidia.__path__:  # type: ignore[attr-defined]
        for so in sorted(glob.glob(os.path.join(base, "*", "lib", "*.so*"))):
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def _build(device: str):
    global _model_name
    from faster_whisper import WhisperModel

    _model_name = config.whisper_model(device)
    if device == "cuda":
        return WhisperModel(_model_name, device="cuda", compute_type="float16")
    return WhisperModel(_model_name, device="cpu", compute_type="int8")


def _is_cuda_problem(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(t in text for t in ("cuda", "cublas", "cudnn", "cudart", "nvrtc", "gpu", "device"))


def is_warm() -> bool:
    """True once the model is loaded — what the daemon's /health reports while it warms."""
    return _model is not None


def _load_model():
    """Load the model once, even when several threads ask at the same time.

    The daemon warms on a background thread while requests are already being
    served, so a bare ``if _model is None`` would let two threads each build a
    model (two copies in VRAM). Fast path stays lock-free once warm.
    """
    global _model, _device
    if _model is not None:
        return _model
    with _load_lock:
        if _model is not None:  # another thread built it while we waited
            return _model
        _preload_cuda_libs()
        try:
            model, device = _build("cuda"), "cuda"
        except Exception as exc:  # noqa: BLE001
            print(f"  (GPU init failed: {exc}; using CPU)", file=sys.stderr)
            model, device = _build("cpu"), "cpu"
        _device = device
        _model = model  # published last: is_warm() must not go true before _device is set
        return _model


def _run(model, audio_path: Path, language: str | None, hotwords: str | None = None):
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        beam_size=5,
        hotwords=hotwords,
    )
    text = "".join(seg.text for seg in segments).strip()  # consuming the generator runs inference
    return text, info


def transcribe(audio_path: Path, language: str | None = None) -> tuple[str, dict]:
    """Transcribe ``audio_path``. Returns (text, info_dict)."""
    global _model, _device
    from . import vocab

    hotwords = vocab.hotwords_string()
    model = _load_model()
    try:
        text, info = _run(model, audio_path, language, hotwords=hotwords)
    except Exception as exc:  # noqa: BLE001
        if _device == "cuda" and _is_cuda_problem(exc):
            print(f"  (GPU inference failed: {exc}; retrying on CPU)", file=sys.stderr)
            _model = _build("cpu")
            _device = "cpu"
            text, info = _run(_model, audio_path, language, hotwords=hotwords)
        else:
            raise
    meta = {
        "whisper_model": _model_name,
        "device": _device,
        "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "audio_duration_s": round(float(info.duration), 2),
    }
    return vocab.apply_corrections(text), meta
