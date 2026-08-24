"""Tests for the incremental live-transcription model (no HTTP, no models, no GPU).

The transcriber and the VAD are injected, so the worker is exercised with synthetic
PCM: ``\\x01\\x00`` samples are "speech", ``\\x00\\x00`` samples are silence, and the
fake VAD derives its spans from exactly that. Every assertion polls to a deadline —
the worker is a real thread, so nothing here may sleep for a fixed time and hope.
"""

import threading
import time

import pytest

from vnote import stream
from vnote.audio import BYTES_PER_S

SPEECH = b"\x01\x00"
SILENCE = b"\x00\x00"


def speech(seconds: float) -> bytes:
    return SPEECH * int(seconds * BYTES_PER_S / 2)


def hush(seconds: float) -> bytes:
    return SILENCE * int(seconds * BYTES_PER_S / 2)


def fake_vad(pcm: bytes) -> list[tuple[float, float]]:
    """Spans of nonzero samples, in seconds — the deterministic stand-in for Silero."""
    spans: list[tuple[float, float]] = []
    start = None
    for i in range(0, len(pcm) - 1, 2):
        loud = pcm[i] != 0 or pcm[i + 1] != 0
        if loud and start is None:
            start = i
        elif not loud and start is not None:
            spans.append((start / BYTES_PER_S, i / BYTES_PER_S))
            start = None
    if start is not None:
        spans.append((start / BYTES_PER_S, len(pcm) / BYTES_PER_S))
    return spans


def raising_vad(pcm: bytes) -> list[tuple[float, float]]:
    """A VAD that is down — its answer is *unknown*, which is not the same as silence."""
    raise RuntimeError("onnxruntime went away")


class FakeTranscriber:
    """Returns ``t<bytes>`` so a partial identifies the exact tail it saw."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []
        self.gate: threading.Event | None = None  # set to block a pass mid-flight
        self.fail_next = 0

    def __call__(self, pcm: bytes, language: str | None) -> tuple[str, dict]:
        self.calls.append((len(pcm), language))
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("model went away")
        return f"t{len(pcm)}", {"language": language}


def poll(fn, timeout: float = 5.0):
    """Wait for ``fn()`` to return something truthy; the worker is a real thread."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for the live worker")
        time.sleep(0.005)


def feed(session, pcm: bytes, chunk_s: float = 0.25) -> None:
    """Append in browser-sized chunks — where a segment breaks may not depend on chunk size."""
    n = int(chunk_s * BYTES_PER_S)
    for i in range(0, len(pcm), n):
        session.append(pcm[i:i + n])


@pytest.fixture
def make_session(tmp_path):
    """Builds LiveSessions and closes every one of them (worker threads must not outlive a test)."""
    made: list[stream.LiveSession] = []

    def factory(transcriber=None, **kwargs) -> stream.LiveSession:
        kwargs.setdefault("language", "en")
        kwargs.setdefault("vad", fake_vad)
        kwargs.setdefault("spill_dir", tmp_path)
        session = stream.LiveSession("sid", transcribe_pcm=transcriber or FakeTranscriber(), **kwargs)
        made.append(session)
        return session

    yield factory
    for session in made:
        session.close(keep_audio=False)


# --- should_commit ------------------------------------------------------------


def test_should_commit_rules():
    def n(seconds: float) -> int:
        return int(seconds * BYTES_PER_S)

    # A tail that grew past max_s commits whatever the VAD says.
    assert stream.should_commit(n(30.0), [(0.0, 30.0)]) is True
    assert stream.should_commit(n(31.0), []) is True
    # Too short to be worth committing, even if it is all silence.
    assert stream.should_commit(n(0.9), []) is False
    assert stream.should_commit(n(0.5), [(0.0, 0.2)]) is False
    # Speech followed by enough silence commits; a shorter gap keeps growing the tail.
    assert stream.should_commit(n(2.0), [(0.0, 1.0)]) is True
    assert stream.should_commit(n(2.0), [(0.0, 1.5)]) is False
    assert stream.should_commit(n(2.0), [(0.0, 1.0)], silence_s=1.5) is False
    # No speech at all: commit as an empty segment so the tail never grows unboundedly.
    assert stream.should_commit(n(1.5), []) is True
    # A VAD that raised says "unknown", not "silent": only the cap may commit that tail.
    assert stream.should_commit(n(1.5), None) is False
    assert stream.should_commit(n(29.0), None) is False
    assert stream.should_commit(n(30.0), None) is True


# --- the worker ---------------------------------------------------------------


def test_a_pass_shows_a_tail_without_committing(make_session):
    fake = FakeTranscriber()
    session = make_session(fake)
    session.append(speech(1.0))
    poll(lambda: session.snapshot()["tail"])
    snap = session.snapshot()
    assert snap["tail"] == f"t{len(speech(1.0))}"  # the pass saw the tail, not a growing buffer
    assert snap["partial"] == snap["tail"] and snap["committed"] == []
    assert snap["seconds"] == 1.0
    assert fake.calls == [(len(speech(1.0)), "en")]  # one pass, not one per wake


def test_silence_commits_the_tail(make_session):
    session = make_session()
    session.append(speech(1.0))
    poll(lambda: session.snapshot()["tail"])
    session.append(hush(1.0))
    committed = poll(lambda: session.snapshot()["committed"])

    assert len(committed) == 1
    seg = committed[0]
    assert seg["text"] == f"t{len(speech(1.0)) + len(hush(1.0))}"
    assert seg["start_s"] == 0.0 and seg["end_s"] == 2.0
    assert seg["trailing_silence_s"] == pytest.approx(1.0)
    assert session.snapshot()["tail"] == ""  # the tail emptied with the commit
    assert session.committed_text() == seg["text"]


def test_a_speech_only_tail_commits_at_max_tail_s(make_session):
    session = make_session(max_tail_s=2.0)
    session.append(speech(2.0))  # never any silence: only the cap can commit this
    committed = poll(lambda: session.snapshot()["committed"])
    assert len(committed) == 1
    assert committed[0]["trailing_silence_s"] == pytest.approx(0.0)
    assert session.snapshot()["tail"] == ""


def test_long_pauses_become_paragraph_breaks(make_session):
    session = make_session()
    session.append(speech(1.0) + hush(2.5))  # >= 2 s of silence after the words
    first = poll(lambda: session.snapshot()["committed"])[0]
    session.append(speech(1.0) + hush(1.0))  # a short pause
    second = poll(lambda: session.snapshot()["committed"][1:])[0]
    session.append(speech(1.0) + hush(1.0))
    third = poll(lambda: session.snapshot()["committed"][2:])[0]

    assert first["trailing_silence_s"] == pytest.approx(2.5)
    assert session.committed_text() == f"{first['text']}\n\n{second['text']} {third['text']}"
    assert session.live_transcript() == session.committed_text()


def test_append_never_waits_for_the_transcriber(make_session):
    fake = FakeTranscriber()
    session = make_session(fake)
    session.append(speech(0.6))
    old = poll(lambda: session.snapshot()["tail"])

    fake.gate = threading.Event()  # the next pass blocks inside the model
    session.append(speech(0.6))
    poll(lambda: len(fake.calls) >= 2)

    t0 = time.monotonic()
    snap = session.append(speech(0.6))
    elapsed = time.monotonic() - t0
    fake.gate.set()

    assert elapsed < 0.5  # the GPU is busy; the request is not
    assert snap["partial"] == old  # the old text, not a wait for the new one
    assert snap["seconds"] == pytest.approx(1.8)


def test_a_failing_pass_does_not_kill_the_worker(make_session):
    fake = FakeTranscriber()
    fake.fail_next = 1
    session = make_session(fake)
    session.append(speech(1.0))
    poll(lambda: len(fake.calls) >= 1)
    assert fake.calls[0][0] == len(speech(1.0))  # that pass raised; the worker swallowed it
    session.append(speech(1.0))
    tail = poll(lambda: session.snapshot()["tail"])
    assert tail == f"t{len(speech(2.0))}"


def test_the_spill_file_holds_everything_appended(make_session):
    session = make_session()
    first, second = speech(0.3), hush(0.2)
    session.append(first)
    assert session.pcm_path().read_bytes() == first  # flushed per append: a crash loses nothing
    session.append(second)
    assert session.pcm_path().read_bytes() == first + second

    kept = session.close(keep_audio=True)
    assert kept == session.pcm_path()
    assert kept.read_bytes() == first + second


def test_close_without_keeping_removes_the_audio(make_session):
    session = make_session()
    session.append(speech(0.3))
    path = session.pcm_path()
    assert session.close(keep_audio=False) is None
    assert not path.exists()


def test_a_long_pause_breaks_a_paragraph_across_real_chunks(make_session):
    # The browser sends 250 ms chunks, so a 3 s pause is spread over a dozen passes:
    # each one must lengthen the pause on the segment before it, not start a new one.
    session = make_session()
    feed(session, speech(1.0))
    feed(session, hush(3.0))
    poll(lambda: (session.snapshot()["committed"] or [{}])[0].get("trailing_silence_s", 0) >= 2.0)
    feed(session, speech(1.0) + hush(1.0))
    poll(lambda: session.snapshot()["committed"][1:])

    committed = session.snapshot()["committed"]
    assert len(committed) == 2  # one per utterance — the silence added no segments of its own
    assert committed[0]["trailing_silence_s"] >= 2.0
    assert session.committed_text() == f"{committed[0]['text']}\n\n{committed[1]['text']}"


def test_a_short_pause_stays_in_one_paragraph(make_session):
    session = make_session()
    feed(session, speech(1.0) + hush(1.0))
    poll(lambda: session.snapshot()["committed"])
    feed(session, speech(1.0) + hush(1.0))
    poll(lambda: session.snapshot()["committed"][1:])

    committed = session.snapshot()["committed"]
    assert committed[0]["trailing_silence_s"] < 2.0
    assert session.committed_text() == f"{committed[0]['text']} {committed[1]['text']}"


def test_a_silent_tail_never_reaches_the_transcriber(make_session):
    fake = FakeTranscriber()
    session = make_session(fake)
    session.append(hush(3.0))
    poll(lambda: len(session.tail) == 0)  # the silence committed ...
    assert fake.calls == []  # ... without ever waking the GPU
    assert session.snapshot()["committed"] == []  # and left nothing behind: there was no segment to pause


def test_a_broken_vad_does_not_read_as_silence(make_session):
    fake = FakeTranscriber()
    session = make_session(fake, vad=raising_vad, max_tail_s=2.0)
    session.append(speech(1.2))
    poll(lambda: session.snapshot()["tail"])  # the pass ran ...
    assert session.snapshot()["committed"] == []  # ... and refused to call 1.2 s of speech a pause

    session.append(speech(1.0))  # only the cap may commit a tail we have no speech info for
    committed = poll(lambda: session.snapshot()["committed"])
    assert committed[0]["end_s"] == pytest.approx(2.0)
    assert committed[0]["trailing_silence_s"] == 0.0  # unknown silence is never a paragraph break


def test_a_failing_transcriber_never_sees_a_growing_tail(make_session):
    fake = FakeTranscriber()
    fake.fail_next = 99  # every pass raises: nothing ever commits the normal way
    session = make_session(fake, max_tail_s=1.0)
    for _ in range(8):  # 4 s of audio, half a second at a time
        before = len(fake.calls)
        session.append(speech(0.5))
        poll(lambda before=before: len(fake.calls) > before)

    assert all(n <= BYTES_PER_S for n, _ in fake.calls)  # every pass is capped at max_tail_s
    assert len(session.tail) <= 3 * BYTES_PER_S  # ... and the backlog behind it is bounded


def test_a_stalled_pass_drops_the_backlog_behind(make_session):
    fake = FakeTranscriber()
    fake.gate = threading.Event()  # the pass in flight never returns while we feed
    session = make_session(fake, max_tail_s=1.0)
    session.append(speech(1.0))
    poll(lambda: len(fake.calls) >= 1)
    for _ in range(8):  # 5 s in total against a model that is not answering
        session.append(speech(0.5))
        assert len(session.tail) <= 3 * BYTES_PER_S  # append itself bounds it; the worker cannot

    dropped = session.snapshot()["committed"]
    assert dropped and all(seg["text"] == "" for seg in dropped)  # committed, deliberately untranscribed
    assert dropped[0]["start_s"] == 0.0 and dropped[0]["end_s"] == pytest.approx(1.0)
    assert session.committed_text() == ""  # they carry no words: /stream/finish re-reads the audio
    fake.gate.set()


def test_close_takes_the_worker_thread_with_it(make_session):
    before = threading.active_count()
    session = make_session()
    session.append(speech(0.6))
    poll(lambda: session.snapshot()["tail"])
    session.close(keep_audio=False)
    poll(lambda: threading.active_count() == before)  # joined and gone, not just idle


# --- the registry -------------------------------------------------------------


def test_registry_hands_out_sessions_and_pops_them():
    registry = stream.Registry(transcribe_pcm=FakeTranscriber(), vad=fake_vad)
    session = registry.start("en")
    try:
        assert registry.get(session.sid) is session
        assert registry.get("nope") is None
        assert registry.pop(session.sid) is session
        assert registry.sessions == {}
    finally:
        session.close(keep_audio=False)


def test_registry_ttl_sweep_expires_and_closes():
    expired: list[stream.LiveSession] = []
    registry = stream.Registry(ttl_s=1800.0, transcribe_pcm=FakeTranscriber(), vad=fake_vad,
                               on_expire=expired.append)
    session = registry.start("en")
    session.append(speech(0.3))
    path = session.pcm_path()
    session.last_seen -= registry.ttl_s + 1

    assert registry.get(session.sid) is None  # every touch enforces the TTL
    assert expired == [session]  # on_expire ran while the audio was still there
    assert registry.sessions == {}
    assert not path.exists()  # ... and the session was closed behind it
