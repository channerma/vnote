"""Per-note file names: ``<folder>_note.md``, ``<folder>_minutes.md``, ``<folder>_audio.<ext>``.

Notes written before 2026-09-29 use the bare ``note.md`` / ``minutes.md`` / ``audio.<ext>``.
Resolution order (decision recorded here because it is counter-intuitive):

1. The BARE file, if it exists, wins. A folder that has one is a legacy folder, and there
   ``<folder>_note.md`` may be the frozen double_clean baseline, which must never be read
   as the live note (that would orphan every later edit). New folders never create bare names.
2. Else ``<folder>_<kind>``.
3. Else the one ``*_<kind>`` file (a folder renamed after creation keeps its old prefix).
4. Else the prefixed name, as a new file.

Take folders (``takes/<n>/audio.*``) keep the bare name. ``tests/test_names.py`` pins this.
"""

from pathlib import Path


def _resolve(session_dir: Path, kind: str, legacy: str) -> Path:
    d = Path(session_dir)
    bare = d / legacy
    if bare.is_file():
        return bare
    prefixed = d / f"{d.name}_{kind}"
    if prefixed.is_file():
        return prefixed
    if d.is_dir():
        strays = [p for p in d.glob(f"*_{kind}") if p.is_file()]
        if len(strays) == 1:
            return strays[0]
    return prefixed


def note_path(session_dir: Path) -> Path:
    return _resolve(session_dir, "note.md", "note.md")


def minutes_path(session_dir: Path) -> Path:
    return _resolve(session_dir, "minutes.md", "minutes.md")


def audio_stem(session_dir: Path) -> str:
    """Stem for NEW audio written at a note root (``<folder>_audio``); add the suffix yourself."""
    return f"{Path(session_dir).name}_audio"
