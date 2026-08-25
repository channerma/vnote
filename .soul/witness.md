# Witness log — vnote

Append-only. Sequential IDs `VNOTE-001`, `VNOTE-002`, … Entry format per the Soul
System's `operations/witness-log-format.md`: ID / WHEN / WHERE / WHAT (two sentences,
no interpretation) / TYPE / CONSEQUENCE / STATUS. Five lines or it is not a witness entry.

---

```
ID:           VNOTE-001
WHEN:         2026-08-24 / PHASE9 research, before Phase 2
WHERE:        vnote/server.py > _StreamSession.append (the /stream/* endpoints kept in 0.5.0)
WHAT:         The kept streaming endpoints re-transcribed the whole buffer synchronously on
              every partial. The Body's real notes run 2.5–7 minutes; the design served 5-second utterances.
TYPE:         Universe Contradiction
CONSEQUENCE:  Phase 2 rewrote streaming (committed/tail model, per-session worker).
STATUS:       Resolved
```

```
ID:           VNOTE-002
WHEN:         2026-08-24 / 0.5.0 build, pre-commit review
WHERE:        vnote/server.py > _api_note; vnote/pipeline.py > make_note cleanup except-tuple
WHAT:         An LLM HTTP error was not in the cleanup fallback tuple, escaped make_note, and the
              server's finally unlinked the only copy of the recording. Found by fresh-context review, not by tests.
TYPE:         Failure Mode — data loss on an error path
CONSEQUENCE:  Caught before commit; cleanup made never-fatal, failed uploads kept under failed/.
STATUS:       Resolved
```

```
ID:           VNOTE-003
WHEN:         2026-08-24 / Phase 2, pre-commit review
WHERE:        vnote/server.py > _stream_finish
WHAT:         The session was popped and closed before validation and the spill unlinked on every
              non-200, so a bad mode parameter deleted the recording. Same failure family as VNOTE-002, same day.
TYPE:         Failure Mode — data loss on an error path (repeat)
CONSEQUENCE:  Validate before pop; no failure path unlinks the only copy; tests pin it.
STATUS:       Resolved
```

```
ID:           VNOTE-004
WHEN:         2026-08-24 / Phase 1, pre-commit review
WHERE:        vnote/versions.py > ensure_history / write_meta; server GET routes
WHAT:         Migration ran outside the commit lock and meta.json was written non-atomically; a
              concurrent GET during a commit re-migrated and wiped the version log and every meta field. Reproduced 6×30.
TYPE:         Failure Mode — race
CONSEQUENCE:  RLock, atomic write, corrupt meta refused; barrier-based concurrency test.
STATUS:       Resolved
```

```
ID:           VNOTE-005
WHEN:         2026-08-24 / Phase 2 commit
WHERE:        git index — vnote/web/pcm-worklet.js
WHAT:         `git add <paths>` then `git commit` committed a file a concurrent agent had staged for
              Phase 3 (agents stage new files for the DrvFs chmod rule).
TYPE:         Failure Mode — commit scope
CONSEQUENCE:  Commit redone (reset --soft, unstage, recommit). Rule: commit with pathspecs.
STATUS:       Resolved
```

```
ID:           VNOTE-006
WHEN:         2026-08-24 / PHASE8–PHASE9, eight implementer runs
WHERE:        Process — implementer agent output → diff-reviewer
WHAT:         Eight of eight implementer outputs came back accept-with-fixes from fresh-context review;
              two carried data-loss blockers (VNOTE-002, VNOTE-003) that the suites had not caught.
TYPE:         Council Note — review is load-bearing, not optional
CONSEQUENCE:  Every should-fix applied before commit; cost ≈ one review cycle per build.
STATUS:       Open (pattern)
```

```
ID:           VNOTE-007
WHEN:         2026-08-24 / CLI Space-pause build, pre-commit review
WHERE:        vnote/record.py > pipe backend read_chunk
WHAT:         BufferedReader.read(4096) waited for a full block, so a silent source made Enter and Space
              inert — a regression from the old signal-from-thread stop. Found by review, fixed with select + os.read.
TYPE:         Failure Mode — blocking read
CONSEQUENCE:  pty-based regression test added.
STATUS:       Resolved
```

```
ID:           VNOTE-008
WHEN:         before 2026-07-06 (carried in cursors until this record existed)
WHERE:        test runner — `uv run pytest`
WHAT:         Bare `uv run pytest` resolved to the miniforge Python and silently skipped the VAD suite;
              `uv run python -m pytest` runs the venv's.
TYPE:         Failure Mode — silent test skip
CONSEQUENCE:  Rule documented in USER_GUIDE.md (Development & testing).
STATUS:       Resolved
```

```
ID:           VNOTE-009
WHEN:         2026-07-08 → 2026-08-24
WHERE:        Verification — custom vocabulary correction applied with no daemon restart (mtime cache)
WHAT:         The Body's hand-test of the vocabulary path was never performed; the unknown rode four
              handoff cursors. 0.6.0's live-check list includes it.
TYPE:         Obligation Skipped — verification
CONSEQUENCE:  Unresolved; no field incident known.
STATUS:       Open
```

```
ID:           VNOTE-010
WHEN:         2026-08-25 / PHASE10 A–F, five implementer runs
WHERE:        Process — implementer output → diff-reviewer; vnote/versions.py _keep_original, vnote/takes.py ensure_takes + trash moves, vnote/cleanup.py keep_alive
WHAT:         Five of five implementer outputs came back accept-with-fixes; three carried data-loss-family blockers the
              suites had not caught (a non-atomic copy of Whisper's output, an interrupted flat→takes migration that the
              next Continue would join over the only full transcript, shutil.move's copytree+rmtree fallback on drvfs), plus
              keep_alive "-1" sent as a string, which Ollama 0.23.1 rejects on every cleanup.
TYPE:         Council Note — VNOTE-006 confirmed a second day (review is load-bearing); same failure family as VNOTE-002/003
CONSEQUENCE:  All fixed before commit; the migration now has a completion marker and every step is conditional; trash is
              os.rename only. Cost ≈ one review cycle per build, unchanged.
STATUS:       Open (pattern)
```
