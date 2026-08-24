"""``vnote`` — record a voice note (or take an audio file), transcribe, clean up.

    vnote                      record from mic, Enter to stop, transcribe + clean
    vnote memo.m4a             process an existing audio file
    vnote --light / --summary  cleanup intensity (default: your default_mode setting, else --edit)
    vnote --dictation          plain text from a small fast model — for pasting somewhere
    vnote --raw                transcript only, skip the LLM cleanup
    vnote --backend claude-code  clean up with Claude Code (uses your subscription)
    vnote --redo DIR           re-run cleanup on a saved note (skips transcription)
    vnote --serve [--open]     the daemon + web UI at http://127.0.0.1:8760 (--open launches the browser)
    vnote --doctor             check the environment; vnote --config / --setup
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import __version__, config, firstrun, pipeline
from .config import MODES
from .pipeline import EmptyTranscriptError, TranscriptionError
from .pipeline import resolve_redo as _resolve_redo  # noqa: F401  (kept for tests/back-compat)
from .pipeline import resolved_model as _resolved_model  # noqa: F401  (kept for tests/back-compat)


def _say(*args: object) -> None:
    """Print a status/progress message to stderr (keeps stdout clean for --stdout)."""
    print(*args, file=sys.stderr)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="vnote", description="Local voice notes: record -> transcribe -> tidy up.")
    p.add_argument("audio", nargs="?", help="existing audio file to process; omit to record from the mic")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--light", action="store_const", const="light", dest="mode", help="light cleanup (faithful)")
    mode.add_argument("--edit", action="store_const", const="edit", dest="mode",
                      help="editorial cleanup (the built-in default; see the default_mode setting)")
    mode.add_argument("--summary", action="store_const", const="summary", dest="mode", help="condensed rewrite")
    mode.add_argument("--dictation", action="store_const", const="dictation", dest="mode",
                      help="plain text from a small fast model — no title, no structure")
    p.add_argument("--raw", action="store_true", help="skip the LLM cleanup; keep only the transcript")
    p.add_argument("--backend", choices=("ollama", "claude-code", "claude"), default=None,
                   help="cleanup backend: ollama (local), claude-code (your Claude "
                        "subscription), claude (metered API). Default: your saved first-run choice")
    p.add_argument("--model", help="override the cleanup model name")
    p.add_argument("--instructions", metavar="TEXT",
                   help="extra instructions for the cleanup, e.g. 'bullet points only' (also with --redo)")
    p.add_argument("--language", help="force transcription language (e.g. 'en'); default: the saved "
                                      "`language` setting, else auto-detect")
    p.add_argument("--no-clipboard", action="store_true", help="do not copy the result to the clipboard")
    p.add_argument("--stdout", action="store_true", dest="to_stdout",
                   help="also print the cleaned note to stdout (for piping)")
    p.add_argument("-o", "--open", action="store_true", dest="open_editor",
                   help="open the new note in $EDITOR after writing (with --serve: open the web UI "
                        "in your browser)")
    p.add_argument("--redo", metavar="PATH",
                   help="re-run cleanup on a saved note dir or transcript.txt (no re-transcription)")
    p.add_argument("--keep-temp-audio", action="store_true",
                   help="when recording, also keep the temp wav if writing fails")
    p.add_argument("--no-daemon", action="store_true",
                   help="ignore any running vnote daemon; load models in-process for this run")
    # Utility actions (each short-circuits the normal flow).
    p.add_argument("--serve", action="store_true",
                   help="run the warm daemon + web UI in the foreground (Ctrl-C to stop); add --open "
                        "to launch the browser")
    p.add_argument("--doctor", action="store_true", help="check the environment and exit")
    p.add_argument("--config", action="store_true", dest="show_config", help="print resolved configuration and exit")
    p.add_argument("--setup", action="store_true", help="(re-)run the interactive first-run setup and exit")
    p.add_argument("--version", action="version", version=f"vnote {__version__}")
    p.set_defaults(mode=None)  # resolved in main(): flag > saved default_mode > edit
    return p.parse_args(argv)


def _open_in_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        _say("  (set $EDITOR or $VISUAL to auto-open notes)")
        return
    try:
        subprocess.run([*shlex.split(editor), str(path)])
    except OSError as exc:
        _say(f"  (could not open editor: {exc})")


def _show_config() -> int:
    """Every setting with its effective value and where it came from (the web UI shows the same list)."""
    cf = config.config_file()
    print("vnote configuration (env VNOTE_* > config file > built-in default):")
    exists = "(exists)" if cf.exists() else "(none yet — the web UI or `vnote --setup` writes it)"
    print(f"  config file : {cf} {exists}")
    for row in config.describe():
        value = row["value"]
        if value in ("", None):
            value = {"language": "(auto)", "dictation_model": "(same as ollama_model)"}.get(row["key"], "(default)")
        if row["key"] == "vocab":
            value = f"{value} {'(exists)' if Path(str(value)).exists() else '(none yet — add hotwords in the web UI)'}"
        note = "" if row["editable"] else "  [bound at start — set the env var and restart]"
        print(f"  {row['key']:<16}: {value}  <- {row['source']}{note}")
    return 0


def _report_stage(event: str, **info: object) -> None:
    """Progress lines for make_note, printed as each stage finishes (not after the whole run)."""
    if event == "transcribed":
        _say(f"  {info['chars']} chars in {info['seconds']}s (lang={info['language']}).")
    elif event == "cleaning":
        _say(f"Cleaning up via {info['backend']} ({info['mode']}) ...")
    elif event == "cleaned":
        _say(f"  done in {info['seconds']}s.")
    elif event == "cleanup_failed":
        _say(f"\nCleanup unavailable: {info['error']}\n")
        _say("Keeping the raw transcript instead.")


def _pipeline(no_daemon: bool):
    """Return (transcribe_fn, clean_fn): daemon-backed if one is up, else in-process."""
    if not no_daemon:
        from . import daemon

        if daemon.is_up():
            _say("  (using warm daemon)")
            return daemon.transcribe, daemon.clean
    from .cleanup import clean
    from .transcribe import transcribe

    return transcribe, clean


# --- re-clean an existing note (no transcription) ---------------------------


def _do_redo(args: argparse.Namespace, backend: str) -> int:
    # Resolve first so a bad path is reported before we announce any work.
    try:
        transcript, _ = pipeline.resolve_redo(Path(args.redo))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not transcript:
        print("error: transcript is empty", file=sys.stderr)
        return 1

    _say(f"Re-cleaning via {backend} ({args.mode}) ...")
    _, clean_fn = _pipeline(args.no_daemon)

    try:
        result = pipeline.reclean(
            Path(args.redo), clean_fn=clean_fn, mode=args.mode, backend=backend, model=args.model,
            instructions=args.instructions,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"error: cleanup failed: {exc}", file=sys.stderr)
        return 1

    if result.session_dir is not None:
        _say(f"📁 updated {result.session_dir / 'note.md'}")

    if not args.no_clipboard:
        from .output import copy_to_clipboard

        if copy_to_clipboard(result.note_text):
            _say("   → copied to clipboard")
    if args.to_stdout:
        sys.stdout.write(result.note_text)
    if args.open_editor and result.session_dir is not None:
        _open_in_editor(result.session_dir / "note.md")
    return 0


# --- normal flow ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Utility actions short-circuit before any recording/transcription.
    if args.setup:
        firstrun.run(None, force=True)
        return 0
    if args.show_config:
        return _show_config()
    if args.doctor:
        from . import doctor

        return doctor.run(args.backend or config.backend())
    if args.serve:
        from . import server

        return server.serve(open_browser=args.open_editor)

    # First-run setup (interactive TTY only; a no-op otherwise), then resolve the
    # backend: explicit --backend flag > saved choice / env > built-in default.
    firstrun.run(args.backend)
    backend = args.backend or config.backend()
    args.mode = args.mode or config.default_mode()
    if args.mode not in MODES:  # a bad saved/env default must not become a traceback
        where = "VNOTE_MODE" if config.source("default_mode") == "env" else f"default_mode in {config.config_file()}"
        print(f"error: unknown cleanup mode {args.mode!r} (from {where}); expected one of {', '.join(MODES)}",
              file=sys.stderr)
        return 2
    args.language = args.language or config.language()

    if args.redo:
        return _do_redo(args, backend)

    started = datetime.now()
    tmp_wav: Path | None = None

    # 1. Obtain audio.
    if args.audio:
        audio_path = Path(args.audio).expanduser()
        if not audio_path.is_file():
            print(f"error: no such file: {audio_path}", file=sys.stderr)
            return 2
        _say(f"Using audio file: {audio_path}")
        rec_duration = None
    else:
        from .record import record_to_wav

        tmp_wav = Path(tempfile.mkdtemp(prefix="vnote-")) / "audio.wav"
        try:
            rec_duration = record_to_wav(tmp_wav)
        except Exception as exc:  # noqa: BLE001
            print(f"error: recording failed: {exc}", file=sys.stderr)
            return 1
        if rec_duration < 0.5:
            print("Nothing recorded (too short). Aborting.", file=sys.stderr)
            return 1
        _say(f"Recorded {rec_duration:.1f}s.")
        audio_path = tmp_wav

    # 2-4. Transcribe, clean up (unless --raw), write the session folder.
    transcribe_fn, clean_fn = _pipeline(args.no_daemon)
    _say("Transcribing ...")
    try:
        result = pipeline.make_note(
            audio_path,
            transcribe_fn=transcribe_fn,
            clean_fn=clean_fn,
            mode=args.mode,
            instructions=args.instructions,
            backend=backend,
            model=args.model,
            language=args.language,
            raw=args.raw,
            source="file" if args.audio else "mic",
            source_path=str(audio_path) if args.audio else None,
            rec_duration=rec_duration,
            started=started,
            on_stage=_report_stage,
        )
    except EmptyTranscriptError:
        print("Transcript is empty (no speech detected?). Aborting.", file=sys.stderr)
        return 1
    except TranscriptionError as exc:
        print(f"error: transcription failed: {exc}", file=sys.stderr)
        return 1

    session_dir, written, note_text = result.session_dir, result.written, result.note_text

    # 5. Clipboard.
    clipped = False
    if not args.no_clipboard:
        from .output import copy_to_clipboard

        clipped = copy_to_clipboard(note_text)

    # 6. Report (to stderr; the note itself goes to stdout only with --stdout).
    _say("")
    _say(f"📁 {session_dir}")
    for name in ("audio", "transcript", "note", "meta"):
        if name in written:
            _say(f"   {name:10s} {written[name].name}")
    if clipped:
        _say("   → copied to clipboard")
    elif not args.no_clipboard:
        _say("   (clipboard copy failed — no clipboard tool found; see README)")

    if args.to_stdout:
        sys.stdout.write(note_text if note_text.endswith("\n") else note_text + "\n")
    if args.open_editor and "note" in written:
        _open_in_editor(written["note"])

    # Clean up the temp recording dir; the wav has been copied into the session.
    if tmp_wav is not None and not args.keep_temp_audio:
        try:
            tmp_wav.unlink(missing_ok=True)
            tmp_wav.parent.rmdir()
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
