"""Quarantine bad tracks: off-genre, mis-analyzed BPM, or unanalyzed.

Moves the file to ~/Music/DJTreta/_quarantine/ and removes its DB row so it's
never a planner candidate again. SKIPS any track currently loaded on a deck
(never disrupt the live set). Safe to run repeatedly.

    cd ~/beings/dj-treta && .venv/bin/python3 ops/quarantine.py
"""
import json
import os
import shutil
import urllib.request

from agent.db import get_db, get_library_with_metadata
from agent.tools.helpers import _music_dir

SLUG = "melodic-techno"
BPM_LO, BPM_HI = 112, 132  # melodic-techno sane range


def _loaded_paths():
    """Absolute paths currently loaded on either deck — never quarantine these."""
    out = set()
    for dk in (1, 2):
        try:
            ti = json.load(urllib.request.urlopen(
                f"http://localhost:7778/api/deck/{dk}/track_info", timeout=3))
            p = ti.get("file_path") or ti.get("location") or ""
            if p:
                out.add(os.path.basename(p).lower())
        except Exception:
            pass
    return out


def _is_bad(t):
    """Return a reason string if the track should be quarantined, else None."""
    b = t.get("bpm")
    title = (t.get("title") or "").lower()
    path = (t.get("path") or "").lower()
    # Off-genre leftovers (pre-gate Bollywood etc.)
    for marker in ("aaj ki raat", "kya khoob", "bollywood", "punjabi", "hindi", "filmi"):
        if marker in title or marker in path:
            return f"off-genre ({marker})"
    if b is None:
        return "unanalyzed (no BPM)"
    try:
        if float(b) < BPM_LO or float(b) > BPM_HI:
            return f"off-BPM ({b})"
    except Exception:
        return "bad BPM value"
    return None


def main():
    qdir = os.path.join(str(_music_dir()), "_quarantine")
    os.makedirs(qdir, exist_ok=True)
    loaded = _loaded_paths()
    db = get_db()
    moved = 0
    for t in get_library_with_metadata(include_unanalyzed=True) or []:
        rel = t.get("path", "") or ""
        if f"{SLUG}/" not in rel:
            continue
        reason = _is_bad(t)
        if not reason:
            continue
        abspath = str(_music_dir() / rel) if not rel.startswith("/") else rel
        base = os.path.basename(abspath)
        if base.lower() in loaded:
            print(f"SKIP (playing): {base} [{reason}]")
            continue
        # Move file out (if present) + drop the DB row.
        try:
            if os.path.exists(abspath):
                shutil.move(abspath, os.path.join(qdir, base))
            db.execute("DELETE FROM tracks WHERE path = ?", (rel,))
            db.commit()
            moved += 1
            print(f"QUARANTINED: {base} [{reason}]")
        except Exception as e:
            print(f"FAIL {base}: {e}")
    db.close()
    print(f"--- quarantined {moved} track(s) ---")


if __name__ == "__main__":
    main()
