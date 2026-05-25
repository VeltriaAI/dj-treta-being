"""Backfill structural analysis (mix_out_seconds + timeline) for library tracks.

Tracks imported outside the download path never got analyzed, so their
mix_out_seconds/timeline are NULL. That breaks schedule_transition's
`at_section_marker='mix_out'` resolution — it silently falls back to
"current position + 30s", so scheduled transitions fire at an arbitrary
point instead of the musical outro.

This walks every track missing mix_out_seconds, runs analyze_audio, and
upserts the result (mirrors agent/tools/perception.py). It prioritises the
currently-loaded deck tracks so live scheduling improves first, then backfills
the rest. Heavy (~15-20s/track via librosa), so it runs low-priority with a
breather between tracks to protect the live audio stream.

Run on the VM:
  cd /opt/djclaw && DJCLAW_CONFIG=/etc/djclaw/config.yaml \
    nice -n 19 ionice -c3 /opt/djclaw/venv/bin/python ops/backfill_analysis.py
"""
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

from agent.audio_analysis import analyze_audio
from agent.camelot import KEY_TO_CAMELOT
from agent.db import upsert_track
from agent.playback_applier import resolve_track_path

DB = os.path.expanduser("~/.local/share/djclaw/db/djtreta.db")
MIXXX = "http://localhost:7778"


def _loaded_deck_paths():
    out = []
    for dk in (1, 2):
        try:
            info = json.load(urllib.request.urlopen(f"{MIXXX}/api/deck/{dk}/track_info", timeout=5))
            fp = info.get("file_path", "")
            if fp:
                out.append(fp)
        except Exception:
            pass
    return out


def main():
    conn = sqlite3.connect(DB)
    unanalyzed = [r[0] for r in conn.execute(
        "SELECT path FROM tracks WHERE mix_out_seconds IS NULL"
    ).fetchall()]
    conn.close()

    prio = [p for p in _loaded_deck_paths() if p in unanalyzed]
    ordered = prio + [p for p in unanalyzed if p not in prio]
    total = len(ordered)
    print(f"backfill: {total} tracks need analysis ({len(prio)} currently loaded, prioritised)", flush=True)

    done = failed = 0
    for i, stored_path in enumerate(ordered, 1):
        real = resolve_track_path(stored_path) or (stored_path if Path(stored_path).exists() else None)
        name = Path(stored_path).name[:48]
        if not real:
            failed += 1
            print(f"[{i}/{total}] MISSING {name}", flush=True)
            continue
        try:
            a = analyze_audio(real)
            upsert_track(
                path=stored_path,
                bpm=a["bpm"], key_musical=a["key"],
                key_camelot=KEY_TO_CAMELOT.get(a["key"], ""),
                energy_peak=a["energy_peak"],
                duration_seconds=a["duration_seconds"],
                mix_in_seconds=a["mix_in_seconds"],
                mix_out_seconds=a["mix_out_seconds"],
                timeline=json.dumps(a["timeline"]),
                analyzed_at=time.time(),
            )
            done += 1
            print(f"[{i}/{total}] OK {name} mix_out={a['mix_out_seconds']}s sections={len(a['timeline'])}", flush=True)
        except Exception as e:
            failed += 1
            print(f"[{i}/{total}] FAIL {name}: {e}", flush=True)
        time.sleep(1)  # breathe — keep CPU headroom for the live stream

    print(f"DONE: {done} analyzed, {failed} failed/missing", flush=True)


if __name__ == "__main__":
    main()
