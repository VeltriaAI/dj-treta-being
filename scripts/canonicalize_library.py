#!/usr/bin/env python3
"""Backfill canonical identity for existing library tracks.

Two modes:
  default (safe):   compute canonical fields for all tracks; report duplicates.
  --merge-dupes:    actually merge duplicate groups (pick winner per group,
                    redirect set_history.track_id + track_aliases, delete losers).
  --delete-files:   when merging, also delete the orphan .mp3 files on disk.

Usage:
  python scripts/canonicalize_library.py                # dry scan + report
  python scripts/canonicalize_library.py --limit 10     # only first 10 (testing)
  python scripts/canonicalize_library.py --merge-dupes  # actually merge DB rows
  python scripts/canonicalize_library.py --merge-dupes --delete-files  # also rm files
"""

import argparse
import sys
from pathlib import Path

# Allow running as: python scripts/canonicalize_library.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.db import init_db, get_db
from agent.canonicalize import llm_canonicalize


def compute_canonical_for_row(row: dict) -> dict:
    """Derive LLM canonical identity from whatever we have on the existing row.

    We don't have the YouTube URL/uploader for already-downloaded tracks, so we
    use the stored title + artist + filename as the LLM input.
    """
    title = row.get("title") or ""
    artist = row.get("artist") or ""
    path = row.get("path") or ""
    stem = Path(path).stem if path else ""

    # Prefer the richest title-ish string available. Stored title is usually
    # the filename stem (which is the original YouTube string).
    best_title = title or stem or artist
    # Uploader unknown for retrospective — pass artist as a hint.
    return llm_canonicalize(best_title, uploader=artist, duration_seconds=0)


def backfill(limit: int | None = None) -> int:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, path, title, artist FROM tracks "
            "WHERE canonical_artist IS NULL OR canonical_artist = '' "
            "ORDER BY id"
        ).fetchall()
        if limit:
            rows = rows[:limit]

        print(f"Backfilling canonical identity for {len(rows)} tracks...")
        updated = 0
        for i, r in enumerate(rows, 1):
            row = dict(r)
            canon = compute_canonical_for_row(row)
            db.execute(
                "UPDATE tracks SET canonical_artist=?, canonical_song=?, "
                "canonical_version=?, remixer=?, canonical_confidence=? WHERE id=?",
                (canon["canonical_artist"], canon["canonical_song"],
                 canon["canonical_version"], canon["remixer"],
                 canon["canonical_confidence"], row["id"]),
            )
            updated += 1
            print(f"  [{i:>3}/{len(rows)}] {canon['canonical_artist']} - {canon['canonical_song']}"
                  f" ({canon['canonical_version'] or '—'})"
                  f" conf={canon['canonical_confidence']:.2f}")
            if i % 10 == 0:
                db.commit()
        db.commit()

        # Also lowercase existing genre (in-DB, does not touch disk)
        db.execute(
            "UPDATE tracks SET genre = LOWER(TRIM(genre)) "
            "WHERE genre IS NOT NULL AND genre <> LOWER(TRIM(genre))"
        )
        db.commit()
        return updated
    finally:
        db.close()


def find_duplicate_groups() -> list[dict]:
    db = get_db()
    try:
        rows = db.execute("""
            SELECT LOWER(canonical_artist) as a, LOWER(canonical_song) as s,
                   LOWER(COALESCE(canonical_version,'')) as v,
                   LOWER(COALESCE(remixer,'')) as r,
                   COUNT(*) as n, GROUP_CONCAT(id, ',') as ids
            FROM tracks
            WHERE canonical_artist IS NOT NULL AND canonical_artist <> ''
            GROUP BY a, s, v, r
            HAVING n > 1
            ORDER BY n DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _pick_winner(track_ids: list[int]) -> int:
    """Prefer the row with audio analysis (has bpm), then highest conf, then newest."""
    db = get_db()
    try:
        placeholders = ",".join("?" * len(track_ids))
        rows = db.execute(
            f"SELECT id, bpm, analyzed_at, canonical_confidence, created_at "
            f"FROM tracks WHERE id IN ({placeholders})",
            track_ids,
        ).fetchall()
        def score(r):
            return (
                1 if r["bpm"] else 0,
                r["analyzed_at"] or 0,
                r["canonical_confidence"] or 0,
                r["created_at"] or 0,
            )
        return sorted([dict(r) for r in rows], key=score, reverse=True)[0]["id"]
    finally:
        db.close()


def merge_duplicates(delete_files: bool = False) -> dict:
    groups = find_duplicate_groups()
    result = {"groups": len(groups), "rows_deleted": 0, "files_deleted": 0}
    if not groups:
        print("No duplicate groups found.")
        return result

    db = get_db()
    try:
        for g in groups:
            ids = [int(x) for x in g["ids"].split(",")]
            winner = _pick_winner(ids)
            losers = [i for i in ids if i != winner]
            print(f"\n{g['a']} — {g['s']} ({g['v'] or 'Original'}){' remix: ' + g['r'] if g['r'] else ''}")
            print(f"  winner id={winner}, losers={losers}")

            # Redirect references
            placeholders = ",".join("?" * len(losers))
            db.execute(
                f"UPDATE set_history SET track_id=? WHERE track_id IN ({placeholders})",
                [winner, *losers],
            )
            db.execute(
                f"UPDATE track_aliases SET track_id=? WHERE track_id IN ({placeholders})",
                [winner, *losers],
            )

            # Gather file paths of losers (before delete)
            loser_rows = db.execute(
                f"SELECT id, path FROM tracks WHERE id IN ({placeholders})",
                losers,
            ).fetchall()
            db.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", losers)
            result["rows_deleted"] += len(losers)

            if delete_files:
                for r in loser_rows:
                    p = Path(r["path"])
                    if p.exists():
                        try:
                            p.unlink()
                            result["files_deleted"] += 1
                            print(f"  deleted: {p.name}")
                        except Exception as e:
                            print(f"  failed to delete {p.name}: {e}")
        db.commit()
    finally:
        db.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only backfill first N tracks (testing)")
    ap.add_argument("--skip-backfill", action="store_true",
                    help="don't re-compute canonical, only scan for dupes")
    ap.add_argument("--merge-dupes", action="store_true",
                    help="actually merge duplicate groups (rewrites DB)")
    ap.add_argument("--delete-files", action="store_true",
                    help="with --merge-dupes, also delete orphan .mp3 files")
    args = ap.parse_args()

    init_db()

    if not args.skip_backfill:
        updated = backfill(limit=args.limit)
        print(f"\nBackfilled {updated} tracks.")

    print("\n=== Duplicate groups ===")
    groups = find_duplicate_groups()
    if not groups:
        print("None found.")
    else:
        for g in groups:
            ids = g["ids"].split(",")
            print(f"  [{g['n']}x] {g['a']} — {g['s']} ({g['v'] or 'Original'})"
                  f"{' remix: ' + g['r'] if g['r'] else ''}  ids={','.join(ids)}")

    if args.merge_dupes:
        print("\n=== Merging duplicates ===")
        res = merge_duplicates(delete_files=args.delete_files)
        print(f"\nResult: {res}")
    else:
        print("\n(Re-run with --merge-dupes to actually merge these groups.)")


if __name__ == "__main__":
    main()
