"""Guardrails for the files in scripts/.

Windows PowerShell 5.1 reads BOM-less .ps1 files as ANSI, so any non-ASCII
byte can mangle into a curly quote and break parsing (an em-dash inside a
string did exactly that). Rule of thumb, kept after the tray client was retired:
everything consumed by Windows-side tooling stays pure ASCII.
"""

from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"


def test_scripts_are_pure_ascii():
    for script in sorted(SCRIPTS.iterdir()):
        if script.is_file():
            script.read_bytes().decode("ascii")  # raises on any non-ASCII byte
