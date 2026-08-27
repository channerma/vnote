"""Silence endpointing on raw PCM, via the Silero VAD bundled with faster-whisper.

Used by the daemon's live sessions (``stream.py``) to find the pause that ends a
tail, so a partial commits at a real speech boundary — no Whisper model is loaded.
faster-whisper is a core dependency, but its import drags in ctranslate2/onnxruntime,
so it happens lazily; the daemon warms it with the model before any recording.
"""

from __future__ import annotations

from .config import SAMPLE_RATE


def speech_spans(pcm_s16le: bytes) -> list[tuple[float, float]]:
    """(start_s, end_s) speech segments detected in raw 16 kHz mono s16le PCM."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    audio = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
    if not len(audio):
        return []
    # Close segments after a short pause and skip end-padding, so the trailing gap
    # the caller measures against its silence threshold tracks real silence promptly.
    opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=0)
    spans = get_speech_timestamps(audio, opts, sampling_rate=SAMPLE_RATE)
    return [(s["start"] / SAMPLE_RATE, s["end"] / SAMPLE_RATE) for s in spans]
