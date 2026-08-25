"""``vnote`` — record a voice note (or take an audio file), transcribe, clean up.

    vnote                      record from mic, Enter to stop, transcribe + clean
    vnote memo.m4a             process an existing audio file
    vnote --light / --summary  cleanup intensity (default: --edit)
    vnote --raw                transcript only, skip the LLM cleanup
    vnote --backend claude-code  clean up with Claude Code (uses your subscription)
    vnote --backend opencode     clean up with opencode (uses its configured model)
    vnote --redo DIR           re-run cleanup on a saved note (skips transcription)
    vnote --promote [TAKE]     turn a dictated flow take into its own note folder
    vnote --serve              keep models warm in a localhost daemon (faster runs)
    vnote --doctor             check the environment; vnote --config / --setup
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import __version__, config, firstrun
from .config import CLAUDE_MODEL, DEFAULT_MODE, MODES


def _say(*args: object) -> None:
    """Print a status/progress message to stderr (keeps stdout clean for --stdout)."""
    print(*args, file=sys.stderr)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="vnote", description="Local voice notes: record -> transcribe -> tidy up.")
    p.add_argument("audio", nargs="?", help="existing audio file to process; omit to record from the mic")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--light", action="store_const", const="light", dest="mode", help="light cleanup (faithful)")
    mode.add_argument("--edit", action="store_const", const="edit", dest="mode", help="editorial cleanup (default)")
    mode.add_argument("--summary", action="store_const", const="summary", dest="mode", help="condensed rewrite")
    p.add_argument("--raw", action="store_true", help="skip the LLM cleanup; keep only the transcript")
    p.add_argument("--backend", choices=("ollama", "claude-code", "opencode", "claude"), default=None,
                   help="cleanup backend: ollama (local), claude-code (your Claude "
                        "subscription), opencode (your opencode provider), claude (metered API). "
                        "Default: your saved first-run choice")
    p.add_argument("--model", help="override the cleanup model name")
    p.add_argument("--language", help="force transcription language (e.g. 'en'); default: auto-detect")
    p.add_argument("--no-clipboard", action="store_true", help="do not copy the result to the clipboard")
    p.add_argument("--stdout", action="store_true", dest="to_stdout",
                   help="also print the cleaned note to stdout (for piping)")
    p.add_argument("-o", "--open", action="store_true", dest="open_editor",
                   help="open the new note in $EDITOR after writing")
    p.add_argument("--redo", metavar="PATH",
                   help="re-run cleanup on a saved note dir or transcript.txt (no re-transcription)")
    p.add_argument("--keep-temp-audio", action="store_true",
                   help="when recording, also keep the temp wav if writing fails")
    p.add_argument("--no-daemon", action="store_true",
                   help="ignore any running vnote daemon; load models in-process for this run")
    # Utility actions (each short-circuits the normal flow).
    p.add_argument("--serve", action="store_true",
                   help="run the warm-model daemon in the foreground (Ctrl-C to stop)")
    p.add_argument("--promote", nargs="?", const="last", metavar="TAKE",
                   help="promote a flow-history take to its own note folder: bare = the last "
                        "take, 'HH:MM:SS' = that take in the newest day, "
                        "'YYYY-MM-DD HH:MM:SS' = explicit")
    p.add_argument("--doctor", action="store_true", help="check the environment and exit")
    p.add_argument("--config", action="store_true", dest="show_config", help="print resolved configuration and exit")
    p.add_argument("--setup", action="store_true", help="(re-)run the interactive first-run setup and exit")
    p.add_argument("--version", action="version", version=f"vnote {__version__}")
    p.set_defaults(mode=DEFAULT_MODE)
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
    cf = config.config_file()
    print("vnote configuration (env VNOTE_* override the saved file):")
    print(f"  config file : {cf} {'(exists)' if cf.exists() else '(none yet — run `vnote --setup`)'}")
    print(f"  backend     : {config.backend()}")
    print(f"  ollama_model: {config.ollama_model()}")
    print(f"  claude_model: {CLAUDE_MODEL} (--backend claude only)")
    print(f"  claude_code : {config.CLAUDE_CODE_BIN} (--backend claude-code)")
    print(f"  opencode    : {config.OPENCODE_BIN} (--backend opencode; "
          f"model {config.opencode_model() or 'opencode default'})")
    print(f"  dictation   : {config.dictation_model()} (vnote-flow --clean)")
    print(f"  whisper     : {config.WHISPER_MODEL}")
    print(f"  ollama_host : {config.OLLAMA_HOST}")
    daemon_host, daemon_port = config.daemon_addr()
    print(f"  daemon      : {daemon_host}:{daemon_port} (start one with `vnote --serve`)")
    from .vocab import vocab_file

    vf = vocab_file()
    print(f"  vocab       : {vf} {'(exists)' if vf.exists() else '(none — create it for hotwords/corrections)'}")
    print(f"  hotkey      : {config.HOTKEY} (vnote-flow toggle)")
    print(f"  inject      : {config.INJECT}")
    print(f"  notes_dir   : {config.NOTES_DIR}")
    return 0


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


def _do_promote(args: argparse.Namespace) -> int:
    """Daemon-first (it may be appending takes concurrently); in-process fallback."""
    name = None
    if not args.no_daemon:
        from . import daemon

        if daemon.is_up():
            try:
                name = daemon.promote(args.promote)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
    if name is None:
        from . import history

        try:
            name = history.promote_take(args.promote).name
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    _say(f"📁 promoted → {config.NOTES_DIR / name}")
    return 0


# --- re-clean an existing note (no transcription) ---------------------------


def _resolve_redo(path: Path) -> tuple[str, Path | None]:
    """Return (transcript_text, session_dir_or_None) for a --redo target.

    ``path`` may be a session directory (uses its transcript.txt) or a transcript
    file directly. session_dir is returned only when we can write a note back.
    """
    path = path.expanduser()
    if path.is_dir():
        tx = path / "transcript.txt"
        if not tx.is_file():
            raise FileNotFoundError(f"no transcript.txt in {path}")
        return tx.read_text(encoding="utf-8").strip(), path
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        session = path.parent if path.name == "transcript.txt" and (path.parent / "meta.json").exists() else None
        return text, session
    raise FileNotFoundError(f"no such path: {path}")


def _resolved_model(backend: str, model: str | None) -> str:
    """The model name to record in meta.json for a finished cleanup."""
    if model:
        return model
    if backend == "ollama":
        return config.ollama_model()
    if backend == "claude-code":
        return "claude-code (session default)"  # the CLI picks; vnote doesn't pin it
    if backend == "opencode":
        return config.opencode_model() or "opencode (session default)"
    return CLAUDE_MODEL


def _update_meta(session_dir: Path, mode: str, backend: str, model: str | None) -> None:
    meta_path = session_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    meta["cleanup_mode"] = mode
    meta["cleanup_backend"] = backend
    meta["cleanup_model"] = _resolved_model(backend, model)
    meta["recleaned"] = True
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _do_redo(args: argparse.Namespace, backend: str) -> int:
    try:
        transcript, session_dir = _resolve_redo(Path(args.redo))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not transcript:
        print("error: transcript is empty", file=sys.stderr)
        return 1

    _say(f"Re-cleaning via {backend} ({args.mode}) ...")
    _, clean_fn = _pipeline(args.no_daemon)

    try:
        result = clean_fn(transcript, mode=args.mode, backend=backend, model=args.model)
    except (RuntimeError, ValueError) as exc:
        print(f"error: cleanup failed: {exc}", file=sys.stderr)
        return 1

    note_text = f"# {result.title}\n\n{result.body.strip()}\n"
    if session_dir is not None:
        (session_dir / "note.md").write_text(note_text, encoding="utf-8")
        _update_meta(session_dir, args.mode, backend, args.model)
        _say(f"📁 updated {session_dir / 'note.md'}")

    if not args.no_clipboard:
        from .output import copy_to_clipboard

        if copy_to_clipboard(note_text):
            _say("   → copied to clipboard")
    if args.to_stdout:
        sys.stdout.write(note_text)
    if args.open_editor and session_dir is not None:
        _open_in_editor(session_dir / "note.md")
    return 0


# --- normal flow ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    assert args.mode in MODES

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

        return server.serve()
    if args.promote:
        return _do_promote(args)

    # First-run setup (interactive TTY only; a no-op otherwise), then resolve the
    # backend: explicit --backend flag > saved choice / env > built-in default.
    firstrun.run(args.backend)
    backend = args.backend or config.backend()

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

    # 2. Transcribe.
    transcribe_fn, clean_fn = _pipeline(args.no_daemon)
    _say("Transcribing ...")
    t0 = time.monotonic()
    try:
        transcript, tmeta = transcribe_fn(audio_path, language=args.language)
    except Exception as exc:  # noqa: BLE001
        print(f"error: transcription failed: {exc}", file=sys.stderr)
        return 1
    transcribe_s = round(time.monotonic() - t0, 1)
    if not transcript:
        print("Transcript is empty (no speech detected?). Aborting.", file=sys.stderr)
        return 1
    _say(f"  {len(transcript)} chars in {transcribe_s}s (lang={tmeta.get('language')}).")

    # 3. Clean up (unless --raw).
    note_body: str | None = None
    title: str
    cleanup_s = None
    cleanup_backend = None
    cleanup_model = None
    if args.raw:
        words = transcript.split()
        title = " ".join(words[:6]) if words else "voice note"
    else:
        _say(f"Cleaning up via {backend} ({args.mode}) ...")
        t0 = time.monotonic()
        try:
            result = clean_fn(transcript, mode=args.mode, backend=backend, model=args.model)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            _say(f"\nCleanup unavailable: {exc}\n")
            _say("Keeping the raw transcript instead.")
            words = transcript.split()
            title = " ".join(words[:6]) if words else "voice note"
        else:
            cleanup_s = round(time.monotonic() - t0, 1)
            title = result.title
            note_body = result.body
            cleanup_backend = backend
            cleanup_model = _resolved_model(backend, args.model)
            _say(f"  done in {cleanup_s}s.")

    # 4. Write session folder.
    from .output import copy_to_clipboard, make_session_dir, write_session

    session_dir = make_session_dir(title, when=started)
    meta = {
        "created": started.isoformat(timespec="seconds"),
        "source": "file" if args.audio else "mic",
        "source_path": str(audio_path) if args.audio else None,
        "recording_duration_s": rec_duration,
        "transcribe_seconds": transcribe_s,
        "cleanup_mode": None if args.raw else args.mode,
        "cleanup_backend": cleanup_backend,
        "cleanup_model": cleanup_model,
        "cleanup_seconds": cleanup_s,
        "title": title,
        **tmeta,
    }
    written = write_session(
        session_dir,
        audio_src=audio_path,
        transcript=transcript,
        note_md=note_body,
        title=title,
        meta=meta,
    )

    # 5. Clipboard.
    note_text = transcript if note_body is None else f"# {title}\n\n{note_body.strip()}\n"
    clipped = False
    if not args.no_clipboard:
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
