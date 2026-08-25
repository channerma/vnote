# Ideas — vnote

- I-001 Continue recording into a note: append PCM to audio.wav, transcribe the new segment, append the
  transcript with a timestamp line, offer re-clean (PHASE9 Phase 5; the note view already reserves the button).
- I-002 Compact live-mode audio to opus via PyAV (already a dependency) instead of 1.9 MB/min WAV.
- I-003 Keyboard shortcuts beyond Space (new note, stop, copy).
- I-004 If nobody uses "live transcript off", cut the toggle and its whole state (the designer's own note, HANDOFF.md).
- I-005 Make whisper_model / notes_dir runtime settings (today: env + restart) — the Settings page shows them read-only.
- I-006 A daemon-autostart recipe for native Windows (only WSL2/systemd are documented).
