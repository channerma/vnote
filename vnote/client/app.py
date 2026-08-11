"""``vnote-flow`` — dictate into the focused app via a running vnote daemon.

    vnote-flow                 press ctrl+shift+space to talk, again to stop & paste
    vnote-flow --once          one cycle, no hotkey: record now, press Enter to stop
    vnote-flow --print         print the text to stdout instead of pasting
    vnote-flow --clean         fast LLM dictation cleanup before pasting (default: raw)
    vnote-flow --vad           auto-stop after a pause
    vnote-flow --stream        live partials while you speak; near-instant final

Runs on the machine that owns the keyboard and mic. On WSL setups that's the
Windows side — install `vnote[flow]` under Windows Python and point
VNOTE_DAEMON_HOST/PORT at the WSL daemon (plain localhost works under NAT).
Requires a running daemon (`vnote --serve`); this client stays thin on purpose.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time

from .. import __version__, config, daemon
from ..commands import apply_commands
from ..config import MODES
from .capture import Recorder
from .inject import inject

MIN_SECONDS = 0.5  # same too-short guard as the CLI


def _say(*args: object) -> None:
    print(*args, file=sys.stderr)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="vnote-flow", description="Global push-to-talk dictation via the vnote daemon.")
    p.add_argument("--hotkey", default=config.HOTKEY,
                   help=f"toggle combo (default: {config.HOTKEY}; env VNOTE_HOTKEY)")
    p.add_argument("--clean", nargs="?", const="dictation", choices=("dictation", *MODES), default=None,
                   help="LLM-clean before injecting: bare --clean means the fast dictation profile "
                        "(default: raw transcript)")
    p.add_argument("--backend", choices=("ollama", "claude-code", "claude"), default=None,
                   help="cleanup backend for --clean (runs daemon-side)")
    p.add_argument("--model", help="override the cleanup model for --clean")
    p.add_argument("--tone", default=None,
                   help="tone for --clean output (e.g. 'casual', 'formal'); default: per-app "
                        "match from the config's app_tones map, else none")
    p.add_argument("--language", help="force transcription language (e.g. 'en'); default: auto-detect")
    p.add_argument("--inject", choices=("auto", "paste", "type"), default=config.INJECT, dest="inject_method",
                   help="how to put text into the focused app (default: %(default)s; env VNOTE_INJECT)")
    p.add_argument("--print", action="store_true", dest="to_stdout",
                   help="print the text to stdout instead of injecting it")
    p.add_argument("--once", action="store_true",
                   help="single cycle without a hotkey: record now, press Enter to stop")
    p.add_argument("--vad", action="store_true", default=config.VAD,
                   help="auto-stop after a pause instead of a second key press (env VNOTE_VAD)")
    p.add_argument("--vad-silence", type=float, default=config.VAD_SILENCE, metavar="S",
                   help="trailing-silence window for --vad, in seconds (default: %(default)s)")
    p.add_argument("--stream", action="store_true", default=config.STREAM,
                   help="transcribe while you speak: live partials on the console, near-instant "
                        "final at stop (env VNOTE_STREAM)")
    p.add_argument("--tray", action="store_true", default=config.TRAY,
                   help="show a system-tray status icon with toggles (needs the [flow] extra; "
                        "env VNOTE_TRAY)")
    p.add_argument("--no-history", action="store_false", dest="history", default=config.HISTORY,
                   help="don't save takes to the daemon's flow history (env VNOTE_HISTORY=0)")
    p.add_argument("--version", action="version", version=f"vnote-flow {__version__}")
    return p.parse_args(argv)


def _app_tone() -> str | None:
    """Tone matched from the focused window, if the user configured an app_tones map.

    Gated on the map being non-empty: fetching the title costs a subprocess
    (a powershell spawn on WSL), so don't pay it when it can't matter.
    """
    if not config.app_tones():
        return None
    from .window import active_window_title

    title = active_window_title()
    if not title:
        return None
    tone = config.app_tone_for(title)
    if tone:
        _say(f"  (tone: {tone} — matched {title[:40]!r})")
    return tone


def _deliver(text: str, args: argparse.Namespace, t0: float,
             wav: bytes | None = None, seconds: float = 0.0) -> None:
    """Transcript -> spoken commands -> (optional clean) -> inject/print -> history."""
    raw = text.strip()  # the ASR transcript pre-commands — what history logs as raw
    # With --clean, leave "scratch that" for the LLM — it can merge the correction
    # semantically; the deterministic rule can only cut back to a clause boundary.
    text = apply_commands(raw, scratch=not args.clean)
    if not text:
        _say("  (no speech detected)")
        return
    cleaned = None
    tone = None
    if args.clean:
        tone = args.tone or _app_tone()
        try:
            result = daemon.clean(text, mode=args.clean, backend=args.backend or config.backend(),
                                  model=args.model, tone=tone)
            cleaned = text = result.body.strip()  # body only — no title header when typing into an app
        except RuntimeError as exc:
            _say(f"  (cleanup failed: {exc}; using the raw transcript)")
            text = apply_commands(text)  # no LLM after all — scratch deterministically
    _say(f"  {len(text)} chars in {round(time.monotonic() - t0, 1)}s")
    if args.to_stdout:
        print(text, flush=True)
    elif inject(text, method=args.inject_method):
        _say("  → injected")
    else:
        _say("  (injection failed — the text may still be on the clipboard)")
    _log_history(raw, cleaned, wav, seconds, args, tone)


def _log_history(raw: str, cleaned: str | None, wav: bytes | None, seconds: float,
                 args: argparse.Namespace, tone: str | None) -> None:
    """Best-effort history save — one console line on failure, never an exception."""
    if not args.history:
        return
    try:
        daemon.log_history(
            wav=wav if config.HISTORY_AUDIO else None,
            raw=raw if config.HISTORY_RAW else None,
            clean=cleaned if config.HISTORY_CLEAN else None,
            seconds=seconds,
            mode=args.clean if cleaned is not None else None,
            tone=tone,
        )
    except RuntimeError as exc:
        _say(f"  (history save failed: {exc})")


def _process(wav: bytes, seconds: float, args: argparse.Namespace) -> None:
    """One batch utterance: WAV bytes -> daemon -> _deliver."""
    if seconds < MIN_SECONDS:
        _say("  (too short; ignored)")
        return
    t0 = time.monotonic()
    try:
        text, _meta = daemon.transcribe_bytes(wav, language=args.language)
    except RuntimeError as exc:
        _say(f"error: transcription failed: {exc}")
        return
    _deliver(text, args, t0, wav=wav, seconds=seconds)


class _Streamer:
    """Pumps new PCM from a Recorder into a daemon StreamSession while recording.

    Shows partials as a live console line. finish() (after Recorder.stop())
    flushes the tail and returns the final transcript; on any streaming error
    the caller still holds the full WAV and falls back to the batch route.
    """

    def __init__(self, recorder: Recorder, language: str | None) -> None:
        self._recorder = recorder
        self._session = daemon.StreamSession(language=language)
        self._sent = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _push(self) -> None:
        snapshot = self._recorder.pcm_snapshot()
        if len(snapshot) > self._sent:
            partial = self._session.append(snapshot[self._sent :])
            self._sent = len(snapshot)
            if partial:
                tail = partial[-70:].replace("\n", " ")
                print(f"\r  ≈ {tail:<70}", end="", file=sys.stderr, flush=True)

    def _pump(self) -> None:
        while not self._done.wait(0.5):
            try:
                self._push()
            except RuntimeError:
                return  # daemon hiccup — finish() will retry or the caller falls back

    def finish(self) -> tuple[str, dict]:
        """Flush the tail and return the final (transcript, meta). Call after Recorder.stop()."""
        self._done.set()
        self._thread.join(timeout=10)
        try:
            self._push()  # everything the pump hadn't sent yet
        finally:
            print(file=sys.stderr)  # end the \r partial line
        return self._session.finish()


def _start_tray(args: argparse.Namespace, events: queue.Queue):
    """A running Tray, or None (missing packages / no tray host) — console mode either way."""
    if not args.tray:
        return None
    try:
        from .tray import Tray

        tray = Tray(args, events)
        tray.start()
        return tray
    except Exception as exc:  # noqa: BLE001 - ImportError, no display, no tray host, ...
        _say(f"  (tray unavailable: {exc}; running console-only)")
        return None


def _start_streamer(recorder: Recorder, args: argparse.Namespace) -> _Streamer | None:
    if not args.stream:
        return None
    try:
        return _Streamer(recorder, args.language)
    except RuntimeError as exc:
        _say(f"  (streaming unavailable: {exc}; using batch mode)")
        return None


def _finish_take(recorder: Recorder, streamer: _Streamer | None, args: argparse.Namespace) -> None:
    """Stop the recorder and deliver the take — streamed finish, or batch fallback."""
    wav, seconds = recorder.stop()
    _say(f"  {seconds:.1f}s recorded.")
    if seconds < MIN_SECONDS:
        _say("  (too short; ignored)")
        return
    if streamer is not None:
        t0 = time.monotonic()
        try:
            text, _meta = streamer.finish()
        except RuntimeError as exc:  # the full WAV is still in hand — never lose the take
            _say(f"  (streaming failed: {exc}; falling back to batch)")
        else:
            _deliver(text, args, t0, wav=wav, seconds=seconds)
            return
    _process(wav, seconds, args)


def _run_once(args: argparse.Namespace) -> int:
    rec = Recorder()
    rec.start()
    streamer = _start_streamer(rec, args)
    if args.vad:
        from .. import vad

        _say("● recording — pause to stop.")
        while not vad.should_stop(rec.pcm_snapshot(), silence_s=args.vad_silence):
            time.sleep(0.3)
    else:
        _say("● recording — press Enter to stop.")
        try:
            input()
        except EOFError:
            pass
    _finish_take(rec, streamer, args)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    health = daemon.health()
    if health is None:
        host, port = config.daemon_addr()
        print(f"error: no vnote daemon at {host}:{port} — start one first:  vnote --serve", file=sys.stderr)
        return 1
    host, port = config.daemon_addr()
    _say(f"vnote-flow → daemon at {host}:{port} ({health.get('whisper_model')} on {health.get('device')})")

    if args.vad:  # first Silero call pays the faster_whisper/onnx import; do it before recording
        from .. import vad

        vad.speech_spans(b"\x00\x00" * 16_000)

    if args.once:
        return _run_once(args)

    # Validate the hotkey setup before starting the listener thread.
    from .hotkey import ensure_available, listen, to_pynput

    try:
        ensure_available()
        to_pynput(args.hotkey)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    events: queue.Queue[tuple[str, int]] = queue.Queue()
    listen_error: list[BaseException] = []

    def _listen() -> None:  # pynput can still die at runtime (e.g. display gone)
        try:
            listen(args.hotkey, lambda: events.put(("toggle", 0)))
        except BaseException as exc:  # noqa: BLE001
            listen_error.append(exc)
            events.put(("quit", 0))

    def _watch_vad(rec: Recorder, my_gen: int, done: threading.Event) -> None:
        from .. import vad

        while not done.wait(0.3):
            try:
                if vad.should_stop(rec.pcm_snapshot(), silence_s=args.vad_silence):
                    events.put(("vad-stop", my_gen))
                    return
            except Exception as exc:  # noqa: BLE001
                _say(f"  (vad failed: {exc}; press the hotkey to stop)")
                return

    threading.Thread(target=_listen, daemon=True).start()
    tray = _start_tray(args, events)
    stop_hint = "pause to stop (the hotkey also stops)" if args.vad else "press the hotkey again to stop"
    _say(f"ready — press {args.hotkey} to dictate; {stop_hint} (Ctrl-C quits).")

    recorder: Recorder | None = None
    streamer: _Streamer | None = None
    vad_done: threading.Event | None = None
    gen = 0  # recording generation, so a stale vad-stop can't cut the next take
    try:
        while True:
            kind, event_gen = events.get()
            if kind == "quit":
                print(f"error: hotkey listener failed: {listen_error[0]}", file=sys.stderr)
                return 1
            if kind == "exit":  # tray menu Quit
                _say("bye.")
                return 0
            if kind == "promote":  # tray action: last take -> its own note folder
                try:
                    name = daemon.promote("last")
                except RuntimeError as exc:
                    _say(f"  (promote failed: {exc})")
                else:
                    _say(f"  → note: {name}")
                continue
            if kind == "vad-stop" and (recorder is None or event_gen != gen):
                continue
            if recorder is None:
                gen += 1
                recorder = Recorder()
                recorder.start()
                streamer = _start_streamer(recorder, args)
                if args.vad:
                    vad_done = threading.Event()
                    threading.Thread(target=_watch_vad, args=(recorder, gen, vad_done), daemon=True).start()
                if tray:
                    tray.state("recording")
                _say(f"● recording — {'pause' if args.vad else 'press the hotkey again'} to stop.")
            else:
                if vad_done is not None:
                    vad_done.set()
                    vad_done = None
                if tray:
                    tray.state("processing")
                _finish_take(recorder, streamer, args)
                recorder = None
                streamer = None
                if tray:
                    tray.state("ready")
                _say("ready.")
    except KeyboardInterrupt:
        _say("\nbye.")
        return 0
    finally:
        if tray:
            tray.stop()


if __name__ == "__main__":
    raise SystemExit(main())
