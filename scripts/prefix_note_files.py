#!/usr/bin/env python3
"""One-off migration: rename bare note.md / minutes.md / audio.<ext> to <folder>_note.md etc.

Dry-run by default; pass --apply to rename. Stop the vnote daemon first (it can write).
Safety: verify-then-rename only (os.rename/os.replace, never a copy), a fsynced JSONL manifest
is written before the first change, an existing differing <folder>_note.md is overwritten ONLY
if its bytes equal a versions/note-*.md (so nothing is lost), otherwise the run aborts.
Idempotent: a rerun skips anything already migrated. Undo: replay the manifest in reverse.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

AUDIO = (".wav", ".webm", ".ogg", ".mp4", ".m4a", ".flac", ".mp3")


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(root: Path):
    """Return (actions, errors). action = (op, src, dst) with op in rename|replace|drop."""
    acts, errs = [], []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        if not (d / "transcript.txt").is_file():
            continue  # not a note folder (e.g. flow/)
        pairs = [(d / "note.md", d / f"{d.name}_note.md"), (d / "minutes.md", d / f"{d.name}_minutes.md")]
        pairs += [(d / f"audio{e}", d / f"{d.name}_audio{e}") for e in AUDIO]
        for src, dst in pairs:
            if not src.exists() and not src.is_symlink():
                continue  # nothing to do / already migrated
            if src.is_symlink() or src.stat().st_nlink > 1:
                errs.append(f"{src}: symlink or hard-linked")
                continue
            if not dst.exists():
                acts.append(("rename", src, dst))
            elif sha(src) == sha(dst):
                acts.append(("drop", src, dst))  # identical copy already there
            elif dst.name.endswith("_note.md") and any(
                sha(v) == sha(dst) for v in (d / "versions").glob("note-*.md")
            ):
                acts.append(("replace", src, dst))  # dst is a stale copy preserved in versions/
            else:
                errs.append(f"{dst}: exists, differs, and is not preserved in versions/")
    return acts, errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    acts, errs = plan(a.root)
    for op, s, d in acts:
        print(f"{op:8s} {s.parent.name}/{s.name} -> {d.name}")
    print(f"{len(acts)} actions, {len(errs)} problems")
    for e in errs:
        print("PROBLEM", e, file=sys.stderr)
    if errs:
        return 2
    if not a.apply:
        print("dry run; nothing changed")
        return 0
    with a.manifest.open("a") as m:  # manifest first, fsynced, before any change
        for op, s, d in acts:
            st = s.stat()
            m.write(json.dumps({"op": op, "src": str(s), "dst": str(d), "size": st.st_size,
                                "sha256": sha(s), "dst_sha256_before": sha(d) if d.exists() else None,
                                "ts": time.time()}) + "\n")
        m.flush()
        os.fsync(m.fileno())
    for op, s, d in acts:
        size, digest = s.stat().st_size, sha(s)
        if op == "drop":
            assert sha(d) == digest
            s.unlink()
        else:
            os.replace(s, d) if op == "replace" else os.rename(s, d)
            assert d.stat().st_size == size and sha(d) == digest, f"post-check failed: {d}"
    print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
