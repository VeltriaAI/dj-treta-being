"""Migrate djtreta.db tracks.path from absolute → relative-to-music_dir.

Why: the schema's `tracks.path TEXT UNIQUE NOT NULL` historically held the
absolute path on the analysing machine. When the DB was copied from Mac to
VM (or scanned again at a different mount point), the same track ended up
as 2-3 rows with different prefixes:

  /Users/manish.pratap/Music/DJTreta/<genre>/<file>.mp3
  /mnt/data/library/DJTreta/<genre>/<file>.mp3
  /home/manish.pratap/Music/DJTreta/<genre>/<file>.mp3

Code that did `load_on_deck(/Users/...)` on Linux silently failed → idle
deck never preloaded → every transition fell into emergency_play.

What this script does:
  1. Resolve `library.music_dir` from config.yaml on this machine.
  2. For each row, compute the path RELATIVE to music_dir, stripping any
     of the known cross-machine absolute prefixes.
  3. Group rows by new relative path. Within each group, keep the row
     that has `analyzed_at IS NOT NULL` (most-recent if multiple); delete
     the others.
  4. UPDATE survivors with the relative path.
  5. Add a partial UNIQUE INDEX on the canonical 4-tuple (only enforced
     for rows where canonical_* are non-NULL, so legacy rows aren't
     blocked).

Idempotent — safe to re-run. Run on Mac DB AND VM DB independently. Results
will be identical (same relative paths, same canonical identity).

Usage:
    python scripts/migrate_paths_to_relative.py            # apply
    python scripts/migrate_paths_to_relative.py --dry-run  # plan only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Known cross-machine absolute prefixes that should map to music_dir.
# Order matters — most-specific first.
KNOWN_PREFIXES = [
    "/Users/manish.pratap/Music/DJTreta/",
    "/home/manish.pratap/Music/DJTreta/",
    "/mnt/data/library/DJTreta/",
]


def relativize(path: str, music_dir: Path) -> str | None:
    """Convert any stored path into a relative-to-music_dir path.

    Returns None if the path can't be relativized (unknown prefix outside
    music_dir + not already relative). Caller decides whether to leave the
    row alone or flag it.
    """
    if not path:
        return None
    p = path.strip()

    # Already relative? Validate it points under a real subdir of music_dir.
    if not p.startswith("/"):
        return p  # leave as-is — already relative

    # Try each known prefix (cross-machine).
    for prefix in KNOWN_PREFIXES:
        if p.startswith(prefix):
            return p[len(prefix):]

    # Try the resolved music_dir (handles `~/Music/DJTreta` → its expanded form).
    md_str = str(music_dir.resolve()) + "/"
    if p.startswith(md_str):
        return p[len(md_str):]

    # Unknown absolute prefix — return None so caller can decide.
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Plan only, no changes")
    parser.add_argument(
        "--db",
        default=str(Path(__file__).parent.parent / "djtreta.db"),
        help="Path to djtreta.db (default: repo root)",
    )
    parser.add_argument(
        "--music-dir",
        default=None,
        help="Override library.music_dir (default: read from config.yaml)",
    )
    args = parser.parse_args()

    if args.music_dir:
        music_dir = Path(args.music_dir).expanduser()
    else:
        # Read from config — same loader the agent uses.
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent.config import load_config
        music_dir = load_config().library.music_path

    print(f"music_dir: {music_dir}")
    print(f"db:        {args.db}")
    print(f"dry_run:   {args.dry_run}")
    print()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT id, path, analyzed_at, canonical_artist, canonical_song, "
        "canonical_version, remixer FROM tracks"
    ).fetchall()
    print(f"loaded {len(rows)} rows")

    # 1. Compute new relative path per row.
    rebased = []  # (id, old_path, new_relative_path, analyzed_at)
    unknown = []  # rows with absolute paths we can't strip
    for r in rows:
        new = relativize(r["path"], music_dir)
        if new is None:
            unknown.append(dict(r))
        else:
            rebased.append({
                "id": r["id"],
                "old": r["path"],
                "new": new,
                "analyzed_at": r["analyzed_at"],
            })

    if unknown:
        print(f"WARNING: {len(unknown)} rows with unknown absolute prefix:")
        for r in unknown[:5]:
            print(f"  id={r['id']} path={r['path'][:80]!r}")
        if len(unknown) > 5:
            print(f"  ... and {len(unknown) - 5} more")
        print(f"  These rows will be left UNCHANGED. Add their prefix to "
              f"KNOWN_PREFIXES at the top of this script if needed.")

    # 2. Group by new path → dedupe within group.
    groups = defaultdict(list)
    for r in rebased:
        groups[r["new"]].append(r)

    keepers = []  # (id, new_path) — survivor of each group
    deletes = []  # ids to delete (collisions)
    for new_path, group in groups.items():
        if len(group) == 1:
            keepers.append((group[0]["id"], new_path))
        else:
            # Multiple rows resolve to the same relative path. Keep the one
            # with analyzed_at NOT NULL; if multiple, keep most-recent.
            with_analysis = [r for r in group if r["analyzed_at"] is not None]
            if with_analysis:
                winner = max(with_analysis, key=lambda r: r["analyzed_at"])
            else:
                winner = group[0]  # arbitrary — none have analysis
            keepers.append((winner["id"], new_path))
            for r in group:
                if r["id"] != winner["id"]:
                    deletes.append(r["id"])

    print()
    print(f"plan:")
    print(f"  rebase:  {len(keepers)} rows path → relative")
    print(f"  delete:  {len(deletes)} duplicate rows (collisions)")
    print(f"  unknown: {len(unknown)} rows untouched")

    if args.dry_run:
        print()
        print("(dry-run — no changes applied)")
        return 0

    # 3. Apply changes in a single transaction.
    print()
    print("applying...")
    try:
        # First delete duplicates (so the UPDATE doesn't violate UNIQUE constraint
        # mid-flight on a row whose new path matches an existing row).
        for tid in deletes:
            db.execute("DELETE FROM tracks WHERE id=?", (tid,))
        # Then UPDATE survivors. Some rows may already have the relative path
        # if a previous run partially completed — that's a no-op UPDATE.
        for tid, new_path in keepers:
            db.execute("UPDATE tracks SET path=? WHERE id=?", (new_path, tid))
        db.commit()
        print(f"  deleted {len(deletes)} duplicate rows")
        print(f"  rebased {len(keepers)} rows to relative paths")
    except sqlite3.IntegrityError as e:
        db.rollback()
        print(f"ERROR (rolled back): {e}")
        print("This usually means two rows resolve to the same relative path "
              "but the dedupe logic missed them. Re-run with --dry-run and inspect.")
        return 1

    # 4. Add partial UNIQUE index on canonical 4-tuple.
    # Partial — only enforced for rows where canonical fields are populated.
    # Legacy rows with NULL canonicals stay valid.
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_canonical_unique "
        "ON tracks(canonical_artist, canonical_song, canonical_version, remixer) "
        "WHERE canonical_artist IS NOT NULL AND canonical_song IS NOT NULL"
    )
    db.commit()
    print("  added partial UNIQUE index on canonical 4-tuple (idx_tracks_canonical_unique)")

    # Verify.
    n_after = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    n_relative = db.execute("SELECT COUNT(*) FROM tracks WHERE path NOT LIKE '/%'").fetchone()[0]
    print()
    print(f"verify: {n_after} rows total, {n_relative} relative-path rows "
          f"({n_after - n_relative} still absolute = unknown-prefix rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
