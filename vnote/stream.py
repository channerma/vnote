"""Incremental live transcription: committed segments + an uncommitted tail.

The old model re-transcribed the *whole* buffer inside every ``/stream/append``;
at five minutes one pass costs ~20 s and the requests pile up. Here a per-session
worker thread transcribes only the **tail** (the audio since the last commit) and
commits it at a silence boundary (VAD) or after ``max_tail_s``, so the cost of a
pass is bounded no matter how long the recording runs. ``append`` never waits for
the model: it stores the audio, wakes the worker and returns the latest snapshot.

No HTTP and no models in here — the transcription function (which takes the GPU
lock) and the VAD are injected, so this is testable with fakes and synthetic PCM.
The session also spills every chunk to a raw ``.pcm`` file as it arrives: the
daemon, not the browser, owns the audio, so a crashed tab loses nothing.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .audio import BYTES_PER_S

_PARAGRAPH_SILENCE_S = 2.0  # a pause at least this long joins two segments as a paragraph break
_WAKE_TIMEOUT_S = 1.0  # so a worker notices `stop` even if nothing wakes it
_MAX_BACKLOG_PASSES = 3  # untranscribed audio beyond this many passes is dropped behind (see _drop_behind)


@dataclass
class Segment:
    """One committed piece of the transcript, in seconds from the session start."""

    text: str
    start_s: float
    end_s: float
    trailing_silence_s: float


def should_commit(
    snapshot_len: int,
    spans: list[tuple[float, float]] | None,
    *,
    silence_s: float = 0.8,
    max_s: float = 30.0,
    min_s: float = 1.0,
) -> bool:
    """Should the tail we just transcribed become a committed segment?

    ``spans`` are the VAD speech spans of that same tail, or ``None`` when the VAD
    itself failed — unknown is not the same as silent, so such a tail commits on
    ``max_s`` alone. A tail commits when it grew past ``max_s`` (bounded pass cost),
    or when the speech in it is followed by ``silence_s`` of quiet. A tail with no
    speech at all commits too — pure silence would otherwise grow the tail forever.
    """
    duration = snapshot_len / BYTES_PER_S
    if duration >= max_s:
        return True
    if spans is None:
        return False
    if duration < min_s:
        return False
    if not spans:
        return duration >= silence_s
    return (duration - spans[-1][1]) >= silence_s


class LiveSession:
    """One live recording: committed segments, an uncommitted tail, and a worker."""

    def __init__(
        self,
        sid: str,
        *,
        language: str | None,
        transcribe_pcm,
        vad,
        note_name: str | None = None,
        spill_dir: Path | str | None = None,
        silence_s: float = 0.8,
        max_tail_s: float = 30.0,
        min_pass_s: float = 0.5,
    ) -> None:
        self.sid = sid
        self.language = language
        # Set when the session is a Continue: this recording becomes a take of that
        # note, and the note may not be deleted while the session lives.
        self.note_name = note_name
        self.transcribe_pcm = transcribe_pcm  # (pcm, language) -> (text, meta)
        self.vad = vad  # (pcm) -> [(start_s, end_s), ...]
        self.silence_s = silence_s
        self.max_tail_s = max_tail_s
        self._min_pass_bytes = max(int(min_pass_s * BYTES_PER_S), 1)
        self._cap_bytes = max(int(max_tail_s * BYTES_PER_S) // 2 * 2, 2)  # whole s16 samples

        self._lock = threading.Lock()
        self.committed: list[Segment] = []
        self.tail = bytearray()
        self.tail_text = ""
        self.total_bytes = 0
        self.last_seen = time.monotonic()
        self.dirty = False
        self._committed_bytes = 0
        self._epoch = 0  # bumped whenever the head of the tail moves under an in-flight pass

        fd, name = tempfile.mkstemp(prefix="vnote-live-", suffix=".pcm",
                                    dir=str(spill_dir) if spill_dir is not None else None)
        self._spill_path = Path(name)
        self._spill = os.fdopen(fd, "wb")

        self._wake = threading.Event()
        self._stop = False
        self._worker = threading.Thread(target=self._run, name=f"vnote-live-{sid}", daemon=True)
        self._worker.start()

    # --- the public surface (all of it cheap; nothing here waits on the model) ---

    def append(self, chunk: bytes) -> dict:
        """Store new PCM and return the current snapshot at once — never the GPU's pace."""
        with self._lock:
            if chunk:
                if self._spill is not None:
                    self._spill.write(chunk)
                    self._spill.flush()  # the daemon holds the audio: a crash must not lose the last chunk
                self.tail += chunk
                self.total_bytes += len(chunk)
                self.dirty = True
                self._drop_behind()  # bounded here, not in the worker: a stuck pass must not grow the tail
            self.last_seen = time.monotonic()
            snapshot = self._snapshot()
        self._wake.set()
        return snapshot

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot()

    def committed_text(self) -> str:
        with self._lock:
            return self._committed_text()

    def live_transcript(self) -> str:
        """Everything transcribed so far — best effort; the final pass is authoritative."""
        with self._lock:
            return self._join(self._committed_text(), self.tail_text)

    def ping(self) -> None:
        with self._lock:
            self.last_seen = time.monotonic()

    def pcm_path(self) -> Path:
        return self._spill_path

    def close(self, *, keep_audio: bool) -> Path | None:
        """Stop the worker and close the spill file; returns its path when kept."""
        self._stop = True
        self._wake.set()
        if self._worker.is_alive():
            self._worker.join(timeout=10.0)
        with self._lock:
            if self._spill is not None:
                try:
                    self._spill.close()
                finally:
                    self._spill = None
        if keep_audio:
            return self._spill_path
        self._spill_path.unlink(missing_ok=True)
        return None

    # --- internals (callers hold self._lock) ---

    def _snapshot(self) -> dict:
        return {
            "partial": self._join(self._committed_text(), self.tail_text),
            "committed": [asdict(seg) for seg in self.committed],
            "tail": self.tail_text,
            "seconds": self.total_bytes / BYTES_PER_S,
        }

    @staticmethod
    def _join(committed: str, tail: str) -> str:
        return " ".join(part for part in (committed, tail) if part)

    def _committed_text(self) -> str:
        out: list[str] = []
        gap = ""
        for seg in self.committed:
            if not seg.text:
                continue  # a silence-only segment carries no words, only its pause
            if out:
                out.append(gap)
            out.append(seg.text)
            gap = "\n\n" if seg.trailing_silence_s >= _PARAGRAPH_SILENCE_S else " "
        return "".join(out)

    def _merge_silence(self, duration: float) -> None:
        """A wordless snapshot is a pause, not a segment.

        Real chunking splits a long pause across several passes, so appending each one
        as its own empty segment would leave every kept segment with a tiny trailing
        gap and no paragraph break would ever fire. The pause lengthens the last
        segment that has words instead; before the first of those it is simply dropped.
        """
        for seg in reversed(self.committed):
            if seg.text:
                seg.end_s += duration
                seg.trailing_silence_s += duration
                return

    def _drop_behind(self) -> None:
        """Bound the untranscribed backlog when the model cannot keep up.

        The rule: audio older than ``_MAX_BACKLOG_PASSES * max_tail_s`` is committed in
        ``max_tail_s`` slices *without being transcribed*, as segments with no text and
        nothing logged. A slow or failing transcriber then costs the live view some
        words, never unbounded memory or an ever-growing pass — and no audio is lost:
        the spill file has every byte and ``/stream/finish`` transcribes the whole
        recording in one pass anyway.
        """
        limit = _MAX_BACKLOG_PASSES * self._cap_bytes
        while len(self.tail) > limit:
            n = min(self._cap_bytes, len(self.tail))
            start_s = self._committed_bytes / BYTES_PER_S
            self.committed.append(Segment("", start_s, start_s + n / BYTES_PER_S, 0.0))
            del self.tail[:n]
            self._committed_bytes += n
            self.tail_text = ""
            self._epoch += 1  # an in-flight pass is holding bytes that are no longer the head

    # --- the worker ---

    def _run(self) -> None:
        """One pass at a time, on the oldest ``max_tail_s`` of the tail (coalesced by `dirty`)."""
        while True:
            self._wake.wait(_WAKE_TIMEOUT_S)
            self._wake.clear()
            if self._stop:
                return
            with self._lock:
                if not self.dirty or len(self.tail) < self._min_pass_bytes:
                    continue
                # A capped snapshot is exactly max_tail_s long, so should_commit() always
                # commits it and the leftover feeds the next pass: no pass ever re-sends a
                # growing tail, however slowly the model answers.
                snapshot = bytes(self.tail[:self._cap_bytes])
                epoch = self._epoch
                self.dirty = False
                language = self.language
            try:
                spans = self.vad(snapshot)
            except Exception:  # noqa: BLE001 - "unknown", not "silent": only max_tail_s may commit it
                spans = None
            duration = len(snapshot) / BYTES_PER_S
            if spans == []:
                text = ""  # nothing was said: a GPU pass on pure silence buys nothing
            else:
                try:
                    text, _meta = self.transcribe_pcm(snapshot, language)
                except Exception:  # noqa: BLE001 - partials are best-effort; the final pass is authoritative
                    continue
                text = (text or "").strip()
            with self._lock:
                if epoch != self._epoch:
                    continue  # drop-behind committed these bytes already; this pass is stale
                if not should_commit(len(snapshot), spans, silence_s=self.silence_s, max_s=self.max_tail_s):
                    self.tail_text = text
                    continue
                if text:
                    start_s = self._committed_bytes / BYTES_PER_S
                    trailing = duration - spans[-1][1] if spans else 0.0  # spans is None: unknown, so none
                    self.committed.append(Segment(text, start_s, start_s + duration, max(trailing, 0.0)))
                else:
                    self._merge_silence(duration)
                del self.tail[:len(snapshot)]
                self._committed_bytes += len(snapshot)
                self.tail_text = ""
                if self.tail:  # audio arrived while we were transcribing — go again
                    self.dirty = True
                    self._wake.set()


class NoteBusy(RuntimeError):
    """A live session is already recording into that note (one live take per note)."""


class Registry:
    """The daemon's live-session table; the TTL is enforced on every touch."""

    def __init__(self, *, ttl_s: float = 1800.0, transcribe_pcm, vad, on_expire=None) -> None:
        self.ttl_s = ttl_s
        self.transcribe_pcm = transcribe_pcm
        self.vad = vad
        self.on_expire = on_expire  # called with the session before it is closed (the audio is still there)
        self.sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def start(self, language: str | None = None, note_name: str | None = None) -> LiveSession:
        """Open a session. With ``note_name``: NoteBusy if one is already bound to that note.

        The check and the insert happen under the one lock — two tabs pressing Continue
        at the same moment would otherwise both pass a separate check and each add a
        take to a note the other is still recording into.
        """
        self.sweep()
        sid = uuid.uuid4().hex
        with self._lock:
            if note_name is not None and self._bound(note_name):
                raise NoteBusy(f"a recording is already going into {note_name}")
            session = LiveSession(sid, language=language, note_name=note_name,
                                  transcribe_pcm=self.transcribe_pcm, vad=self.vad)
            self.sessions[sid] = session
        return session

    def _bound(self, name: str) -> bool:  # callers hold self._lock
        return any(s.note_name == name for s in self.sessions.values())

    def bound(self, name: str) -> bool:
        """Is a live session recording into note ``name``? Deletes are refused while it is.

        Sweeps first, so a session whose browser vanished stops blocking the note as
        soon as its TTL is up rather than until the next request happens to sweep.
        """
        self.sweep()
        with self._lock:
            return self._bound(name)

    def get(self, sid: str) -> LiveSession | None:
        self.sweep()
        with self._lock:
            return self.sessions.get(sid)

    def pop(self, sid: str) -> LiveSession | None:
        with self._lock:
            return self.sessions.pop(sid, None)

    def sweep(self) -> None:
        cutoff = time.monotonic() - self.ttl_s
        with self._lock:
            expired = [(sid, s) for sid, s in self.sessions.items() if s.last_seen < cutoff]
            for sid, _ in expired:
                del self.sessions[sid]
        for _, session in expired:
            if self.on_expire is not None:
                try:
                    self.on_expire(session)
                except Exception:  # noqa: BLE001 - an abandoned session must still be freed
                    pass
            session.close(keep_audio=False)
