#!/usr/bin/env python3
"""Batch-seed djtreta.db with tracks across multiple genres.

Walks a hard-coded list of (search_query, genre) pairs. For each:
  1. yt-dlp search for top candidate (2-10 min, no mixes/compilations).
  2. download_track (canonical dedup + canonical filename).
  3. synchronous _enrich_track (librosa BPM/key/energy/timeline).

Safe to re-run — URL and canonical-identity dedup skip tracks already
in DB. Only downloads what's missing.

Usage:
    python scripts/seed_library_batch.py              # full run
    python scripts/seed_library_batch.py --dry-run    # show what would happen
    python scripts/seed_library_batch.py --only dark-techno  # filter by genre
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.discovery import search_music, download_track, _enrich_track  # noqa: E402
from agent.db import get_db  # noqa: E402


# Seed list — artist + title chosen so the top YouTube result is the
# canonical release. Each pair:  ("<search query>", "<genre folder>")
SEEDS: list[tuple[str, str]] = [
    # Progressive house — smooth filter-sweeps, long breakdowns
    ("Eric Prydz Opus original mix", "progressive-house"),
    ("Lane 8 Atlas original mix", "progressive-house"),
    ("Yotto Aviate original mix", "progressive-house"),

    # Deep house — slower, silkier
    ("Maceo Plex Conjure Balearia", "deep-house"),
    ("Dixon Coming Home", "deep-house"),

    # Psytrance — 138-145 BPM, forces echo_out / hard_cut from techno
    ("Vini Vici Great Spirit original", "psytrance"),
    ("Astrix He.art", "psytrance"),

    # Dark techno — heavier, filter_sweep territory
    ("Charlotte de Witte Doppler", "dark-techno"),
    ("Amelie Lens Higher original mix", "dark-techno"),

    # Tech house — groovy, 124-127
    ("Fisher Losing It original", "tech-house"),
    ("CamelPhat Cola original mix", "tech-house"),

    # More melodic-techno but DIFFERENT keys — fills Camelot diversity
    ("Anyma Consciousness original mix", "melodic-techno"),
    ("Tale of Us Unity original", "melodic-techno"),

    # Ambient / downtempo — extreme BPM gap for edge-case echo_out
    ("Bonobo Kerala", "downtempo"),
    ("Tycho Awake", "downtempo"),
]


def _first_good_url(query: str) -> tuple[str, str] | None:
    """Search, return (url, title) of first viable candidate or None."""
    hits = search_music(query, limit=8)
    for h in hits:
        if "error" in h or "info" in h:
            continue
        url = h.get("url", "")
        if url.startswith("http"):
            return url, h.get("title", "")
    return None


def _wait_for_analysis(path: str, timeout: float = 60.0) -> bool:
    """Poll DB until track at `path` has analyzed_at populated."""
    db = get_db()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            row = db.execute(
                "SELECT analyzed_at FROM tracks WHERE path = ?", (path,)
            ).fetchone()
            if row and row[0]:
                return True
            time.sleep(1.0)
    finally:
        db.close()
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be downloaded, no side effects")
    ap.add_argument("--only", type=str, default=None,
                    help="only process seeds whose genre matches this substring")
    ap.add_argument("--analyze-timeout", type=float, default=90.0,
                    help="seconds to wait for each track's librosa analysis")
    args = ap.parse_args()

    filtered = SEEDS
    if args.only:
        filtered = [(q, g) for q, g in SEEDS if args.only.lower() in g.lower()]

    print(f"Processing {len(filtered)} seed candidates")
    print("=" * 70)

    ok = 0
    skipped = 0
    failed = 0

    for i, (query, genre) in enumerate(filtered, 1):
        print(f"\n[{i}/{len(filtered)}] ({genre}) {query}")
        if args.dry_run:
            print("  dry-run: skipping search/download")
            continue

        hit = _first_good_url(query)
        if not hit:
            print(f"  FAILED: no viable search result")
            failed += 1
            continue

        url, title = hit
        print(f"  candidate: {title[:70]} -> {url}")

        try:
            result = download_track(url, genre=genre)
        except Exception as e:
            print(f"  FAILED: download_track raised {e}")
            failed += 1
            continue

        print(f"  -> {result}")

        if result.startswith("ALREADY EXISTS"):
            skipped += 1
            continue
        if "failed" in result.lower() or result.startswith("Download failed"):
            failed += 1
            continue

        # Find the path of what we just inserted (newest DB row with source_url)
        db = get_db()
        try:
            row = db.execute(
                "SELECT path FROM tracks WHERE source_url = ?", (url,)
            ).fetchone()
            path = row[0] if row else None
        finally:
            db.close()

        if not path:
            print(f"  WARN: no DB row for URL — skipping analyze wait")
            ok += 1
            continue

        print(f"  waiting up to {args.analyze_timeout:.0f}s for analysis...")
        analyzed = _wait_for_analysis(path, timeout=args.analyze_timeout)
        if analyzed:
            print(f"  analyzed ✓")
            ok += 1
        else:
            print(f"  WARN: analysis did not complete in time (still in bg thread)")
            ok += 1  # still counts — DB row exists, analysis may finish later

    print("\n" + "=" * 70)
    print(f"downloaded + queued: {ok}")
    print(f"skipped (dedup):     {skipped}")
    print(f"failed:              {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
