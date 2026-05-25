"""Bulk-grow the melodic-techno crate with genre-gated, deduped downloads.

Searches YouTube Music for the mood, runs each candidate through the strict
genre gate (rejects Bollywood / off-genre), skips dupes already on disk, and
downloads until the folder hits TARGET. Run in the background:

    cd ~/beings/dj-treta && nohup .venv/bin/python3 ops/grow_library.py > /tmp/grow-lib.log 2>&1 &
"""
import glob
import os
import sys
import time

from agent.tools.discovery import search_music, download_track
from agent.tools.helpers import _music_dir
from agent.canonicalize import genre_matches

MOOD = "melodic techno"
SLUG = "melodic-techno"
TARGET = 30
# Varied queries so we don't pull the same top-10 over and over.
QUERIES = [
    "melodic techno", "deep melodic techno", "hypnotic melodic techno",
    "driving melodic techno", "atmospheric melodic techno", "anyma afterlife",
    "argy melodic techno", "massano melodic techno", "artbat melodic techno",
    "monolink melodic techno", "stephan bodzin", "colyn melodic techno",
]


def count():
    folder = os.path.join(str(_music_dir()), SLUG)
    return len([p for p in glob.glob(os.path.join(folder, "*.mp3"))
                if not os.path.basename(p).startswith("._")])


def existing_stems():
    folder = os.path.join(str(_music_dir()), SLUG)
    return {os.path.basename(p).lower()[:18]
            for p in glob.glob(os.path.join(folder, "*.mp3"))
            if not os.path.basename(p).startswith("._")}


def main():
    print(f"[grow] start: {count()} tracks, target {TARGET}", flush=True)
    qi = 0
    stale_rounds = 0
    while count() < TARGET and stale_rounds < 6:
        q = QUERIES[qi % len(QUERIES)]
        qi += 1
        before = count()
        try:
            results = search_music(query=q, limit=15) or []
        except Exception as e:
            print(f"[grow] search '{q}' failed: {e}", flush=True)
            continue
        have = existing_stems()
        added_this_round = 0
        for r in results:
            if count() >= TARGET:
                break
            if not isinstance(r, dict) or not r.get("url"):
                continue
            artist, title = (r.get("artist") or "").strip(), (r.get("title") or "").strip()
            if not (artist or title):
                continue
            stem = title.lower()[:18]
            if stem and any(stem in h or h in stem for h in have):
                continue
            if not genre_matches(artist, title, MOOD):
                print(f"[grow] skip off-genre: {artist} - {title}", flush=True)
                continue
            try:
                res = download_track(r["url"], genre=SLUG)
                ok = bool(res.get("ok")) if isinstance(res, dict) else False
                print(f"[grow] {'OK' if ok else 'fail'}: {artist} - {title} ({count()}/{TARGET})", flush=True)
                if ok:
                    added_this_round += 1
                    have.add(stem)
            except Exception as e:
                print(f"[grow] download failed: {e}", flush=True)
            time.sleep(1)
        stale_rounds = stale_rounds + 1 if count() == before else 0
    print(f"[grow] done: {count()} tracks", flush=True)


if __name__ == "__main__":
    main()
