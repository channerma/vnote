"""Speech-to-text via faster-whisper. Uses CUDA when available, falls back to CPU."""

from __future__ import annotations

import ctypes
import glob
import os
import sys
from pathlib import Path

from . import config

_model = None  # lazily loaded, cached for the process
_device = None
_model_name = None  # which model _model actually is (the device decides by default)


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


def _cuda_plausible() -> bool:
    """Whether trying CUDA here can possibly succeed.

    CTranslate2 publishes no CUDA-enabled macOS wheel (and no Metal/MPS backend),
    so on darwin the attempt fails 100% of the time — it printed "GPU init failed:
    This CTranslate2 package was not compiled with CUDA support" on every single
    run. That is noise, not a diagnosis. Everywhere else the attempt is still worth
    making and its failure still worth printing: a Linux box with a half-installed
    CUDA stack genuinely needs to be told. `--doctor` is where macOS users are told
    they are on CPU, once, instead of on every transcription.
    """
    return sys.platform != "darwin"


def _load_model():
    global _model, _device
    if _model is not None:
        return _model
    if not _cuda_plausible():
        _model = _build("cpu")
        _device = "cpu"
        return _model
    _preload_cuda_libs()
    try:
        _model = _build("cuda")
        _device = "cuda"
    except Exception as exc:  # noqa: BLE001
        print(f"  (GPU init failed: {exc}; using CPU)", file=sys.stderr)
        _model = _build("cpu")
        _device = "cpu"
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
