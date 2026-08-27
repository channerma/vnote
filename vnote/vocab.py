"""Custom vocabulary: bias ASR toward your terms, correct it afterwards.

The dictionary is a plain-text file (``~/.config/vnote/vocab.txt``, override
with ``VNOTE_VOCAB``), one entry per line:

    # bare line = hotword: bias transcription toward this spelling
    TRANSFORM
    Dymola

    # left -> right = correction applied to the transcript
    # (case-insensitive, whole-word, replacement used verbatim)
    jason -> JSON
    v note -> vnote

Loaded with an mtime cache, so edits apply on the next utterance — no daemon
restart. Corrections run inside ``transcribe()``; hotwords go to
``model.transcribe(hotwords=...)``.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config


def vocab_file() -> Path:
    return config.vocab_file()


_cache: tuple[tuple[str, float], list[str], list[tuple[re.Pattern, str]]] | None = None


def _parse(text: str) -> tuple[list[str], list[tuple[re.Pattern, str]]]:
    hotwords: list[str] = []
    rules: list[tuple[re.Pattern, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "->" in line:
            wrong, _, right = line.partition("->")
            wrong, right = wrong.strip(), right.strip()
            if wrong:
                rules.append((re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE), right))
        else:
            hotwords.append(line)
    return hotwords, rules


def _load() -> tuple[list[str], list[tuple[re.Pattern, str]]]:
    """Parsed vocab, cached on (path, mtime). Missing file -> empty vocab."""
    global _cache
    path = vocab_file()
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return [], []
    if _cache is None or _cache[0] != key:
        try:
            hotwords, rules = _parse(path.read_text(encoding="utf-8"))
        except OSError:
            return [], []
        _cache = (key, hotwords, rules)
    return _cache[1], _cache[2]


def hotwords_string() -> str | None:
    """Hotwords as the comma-joined string faster-whisper expects, or None."""
    hotwords, _ = _load()
    return ", ".join(hotwords) if hotwords else None


def apply_corrections(text: str) -> str:
    for pattern, right in _load()[1]:
        text = pattern.sub(right, text)
    return text
