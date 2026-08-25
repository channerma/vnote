"""Write a session folder and (best-effort) copy the note to the Windows clipboard."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .config import NOTES_DIR


def _slugify(text: str, max_words: int = 8) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    slug = "-".join(words[:max_words])
    return slug or "note"


def make_session_dir(title_hint: str, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    name = f"{when:%Y-%m-%d-%H%M}-{_slugify(title_hint)}"
    path = NOTES_DIR / name
    suffix = 1
    while path.exists():
        suffix += 1
        path = NOTES_DIR / f"{name}-{suffix}"
    path.mkdir(parents=True)
    return path


def write_session(
    session_dir: Path,
    *,
    audio_src: Path | None,
    transcript: str,
    note_md: str | None,
    title: str,
    meta: dict,
    heading: bool = True,
) -> dict[str, Path]:
    """Populate ``session_dir``. Returns a map of artifact name -> path."""
    written: dict[str, Path] = {}

    if audio_src is not None:
        audio_dst = session_dir / ("audio" + audio_src.suffix.lower())
        if audio_src.resolve() != audio_dst.resolve():
            shutil.copy2(audio_src, audio_dst)
        written["audio"] = audio_dst

    transcript_path = session_dir / "transcript.txt"
    transcript_path.write_text(transcript.strip() + "\n", encoding="utf-8")
    written["transcript"] = transcript_path

    if note_md is not None:
        note_path = session_dir / "note.md"
        head = f"# {title}\n\n" if heading else ""  # a plain-output style: no title line
        note_path.write_text(f"{head}{note_md.strip()}\n", encoding="utf-8")
        written["note"] = note_path

    meta_path = session_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    written["meta"] = meta_path

    return written


def _clipboard_commands() -> list[list[str]]:
    """Clipboard writers to try, in order, for whatever platform we're on.

    WSL is the primary, tested path: ``clip.exe`` puts text on the *Windows*
    clipboard (the one the user actually pastes from). On native Linux we fall
    back to Wayland/X11 tools, and on macOS to ``pbcopy``. Each command reads the
    text from stdin.
    """
    cmds: list[list[str]] = []
    # WSL → Windows clipboard (tested path). clip.exe reads stdin in the console's
    # active code page; UTF-8 is the safe default and round-trips English cleanly.
    clip_exe = shutil.which("clip.exe")
    if clip_exe or Path("/mnt/c/Windows/System32/clip.exe").exists():
        cmds.append([clip_exe or "/mnt/c/Windows/System32/clip.exe"])
    # Linux: Wayland first, then X11.
    if shutil.which("wl-copy"):
        cmds.append(["wl-copy"])
    if shutil.which("xclip"):
        cmds.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        cmds.append(["xsel", "--clipboard", "--input"])
    # macOS.
    if shutil.which("pbcopy"):
        cmds.append(["pbcopy"])
    return cmds


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard. Returns True on the first writer that works.

    Tries platform-appropriate tools (clip.exe on WSL, wl-copy/xclip/xsel on Linux,
    pbcopy on macOS). Best-effort: returns False if none are present or all fail.
    """
    for cmd in _clipboard_commands():
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False
