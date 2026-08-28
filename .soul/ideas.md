# Ideas — vnote

- I-001 [→ PHASE10 F, decided 2026-08-25: per-take folders, not PCM append] Continue recording into a note: append PCM to audio.wav, transcribe the new segment, append the
  transcript with a timestamp line, offer re-clean (PHASE9 Phase 5; the note view already reserves the button).
- I-002 Compact live-mode audio to opus via PyAV (already a dependency) instead of 1.9 MB/min WAV.
- I-003 Keyboard shortcuts beyond Space (new note, stop, copy).
- I-004 If nobody uses "live transcript off", cut the toggle and its whole state (the designer's own note, HANDOFF.md).
- I-005 Make whisper_model / notes_dir runtime settings (today: env + restart) — the Settings page shows them read-only.
- I-006 A daemon-autostart recipe for native Windows (only WSL2/systemd are documented).
- I-007 Project-local styles as an automatic group (`<notes_dir>/styles/`) — considered 2026-08-25, cut from
  PHASE10 E as ahead of the evidence; the `VNOTE_STYLES_DIRS` list in a project's .env covers it meanwhile.
- I-008 "Empty trash" (and a size readout) once `<notes_dir>/trash/` actually piles up (PHASE10 F).
- I-009 Delete-note / delete-take restore from the page (today: a folder move by hand, documented).
- I-010 Show the `audio_kept` path from `/stream/cancel?keep=1` in the Retry banner ("the daemon parked its copy at …");
  the page ignores it today (page review 2026-08-25).
- I-011 `rootJoin()`'s one-take branch returns the PUT text verbatim; a takes/ note reduced to one take differs by
  whitespace from the daemon's join until reload. Copy-only, watch only (page review 2026-08-25).
- I-012 [shipped 2026-08-27: double-clean] Save a second, varied cleanup beside every note: temp-0 baseline
  `<folder>_note.md` + a `variant_temperature` pass `<folder>_note_variant_tN.md` (N encodes the temp: t3 = 0.3).
  Env/config: VNOTE_DOUBLE_CLEAN, VNOTE_VARIANT_TEMPERATURE. Note: it runs in-process from pipeline.produce even
  when a warm daemon is up (the daemon's /clean has no temperature knob) — see the comment in produce().
- I-013 [shipped 2026-08-27] macOS LaunchAgent: `scripts/com.vnote.daemon.plist` (twin of
  vnote-daemon.service) with a header documenting the absolute-path + minimal-PATH + VNOTE_DIR
  edits; installed live on this box at ~/Library/LaunchAgents, `launchctl kickstart -k` restarts
  after reinstalls. The old ad-hoc plist is now the shipped template; the verify path is README/§
  USER_GUIDE "Start it automatically".
- I-014 [resolved 2026-08-27] The old tag kept only the fractional digits (1.3 and 0.3 both became
  t3; 1.0 became t0). Fixed: `_note_variant_t{tag}.md` spells the temp dot-free, integer part
  included (0.3 -> t0p3, 1.3 -> t1p3). Regression test: test_double_clean_variant_tag_cannot_collide_above_temp_1.
