#!/usr/bin/env python3
"""NS-009: Ingest the pre-tagged library into the tracks table.

Every file under config.library.music_dir already carries full DJ metadata in
its ID3/AIFF tags (we wrote them): TBPM=bpm, TKEY=musical key, TIT1=Camelot
code, TCON=genre, COMM='Energy N | DJ Treta Library', TPE1/TIT2 artist/title.
The daemon's planner, however, only sees tracks WHERE analyzed_at IS NOT NULL
— and librosa analysis never ran on this volume, so the planner pool is empty
and the local LLM hallucinates track names.

This script walks the music dir, reads the tags with mutagen, and upserts each
track via agent.db.upsert_track (same path normalization + same DB resolution
as the daemon — safe to run while the daemon is live; upserts are ON
CONFLICT-atomic). analyzed_at is stamped only when bpm AND key are present.

Usage:
  .venv/bin/python scripts/ingest_tagged_library.py --dry-run   # report only
  .venv/bin/python scripts/ingest_tagged_library.py             # ingest
  .venv/bin/python scripts/ingest_tagged_library.py --prune-missing
      # additionally DELETE tracks rows whose file no longer exists on disk
      # (stale ~/Music/DJTreta download rows with dead paths pollute the pool)
"""

import argparse
import re
import sys
import time
from pathlib import Path

# Allow running as: python scripts/ingest_tagged_library.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.db as db
from agent.db import _SYNTH_DIRS, get_db, init_db, upsert_track

_SKIP_DIRS = set(_SYNTH_DIRS) | {"Playlists"}
_ENERGY_RE = re.compile(r"Energy\s+(\d+)", re.IGNORECASE)


def slugify_mood(genre: str) -> str:
    """'Melodic Techno' -> 'melodic-techno'; 'Minimal Deep Tech' -> 'minimal-deep-tech'."""
    return re.sub(r"\s+", "-", (genre or "").strip().lower())


def _frame_text(tags, frame: str) -> str:
    """First text value of an ID3 frame, stripped; '' if absent."""
    f = tags.get(frame)
    if f is None:
        return ""
    try:
        return str(f.text[0]).strip()
    except (AttributeError, IndexError):
        return str(f).strip()


def _parse_energy(tags) -> int | None:
    """Parse N from a COMM frame like 'Energy 7 | DJ Treta Library'."""
    for comm in tags.getall("COMM"):
        m = _ENERGY_RE.search(str(comm))
        if m:
            return int(m.group(1))
    return None


def read_track_tags(path: Path) -> dict | None:
    """Read one .mp3/.aiff; returns field dict or None if unreadable/untagged."""
    from mutagen import File as MutagenFile
    from mutagen.aiff import AIFF
    from mutagen.id3 import ID3

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            tags = ID3(str(path))
        elif suffix in (".aiff", ".aif"):
            tags = AIFF(str(path)).tags
        else:
            return None
    except Exception as exc:
        print(f"  ! unreadable tags: {path.name}: {exc}")
        return None
    if tags is None:
        return None

    bpm_raw = _frame_text(tags, "TBPM")
    try:
        bpm = float(bpm_raw) if bpm_raw else None
    except ValueError:
        bpm = None

    duration = None
    try:
        mf = MutagenFile(str(path))
        if mf is not None and mf.info is not None:
            duration = float(mf.info.length)
    except Exception:
        pass

    genre = _frame_text(tags, "TCON")
    return {
        "bpm": bpm,
        "key_musical": _frame_text(tags, "TKEY") or None,
        "key_camelot": _frame_text(tags, "TIT1") or None,
        "genre": genre or None,
        "artist": _frame_text(tags, "TPE1") or None,
        "title": _frame_text(tags, "TIT2") or None,
        "energy_peak": _parse_energy(tags),
        "duration_seconds": duration,
        "mood": slugify_mood(genre) if genre else None,
    }


def iter_audio_files(music_path: Path):
    """Yield .mp3/.aiff files under music_path, skipping AppleDouble ('._')
    files and the synthetic/Playlists top-level dirs."""
    for p in sorted(music_path.rglob("*")):
        if not p.is_file() or p.name.startswith("._"):
            continue
        if p.suffix.lower() not in (".mp3", ".aiff", ".aif"):
            continue
        rel_parts = p.relative_to(music_path).parts
        if rel_parts and rel_parts[0] in _SKIP_DIRS:
            continue
        yield p


def prune_missing(music_path: Path, dry_run: bool) -> int:
    """Delete tracks rows whose path no longer exists on disk. Returns count."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, path FROM tracks").fetchall()
        dead = []
        for r in rows:
            p = Path(r["path"])
            resolved = p if p.is_absolute() else music_path / p
            if not resolved.expanduser().exists():
                dead.append((r["id"], r["path"]))
        print(f"\nPrune: {len(dead)} tracks rows point at missing files")
        for _id, path in dead[:20]:
            print(f"  missing: {path}")
        if len(dead) > 20:
            print(f"  ... and {len(dead) - 20} more")
        if dead and not dry_run:
            conn.executemany(
                "DELETE FROM tracks WHERE id = ?", [(d[0],) for d in dead]
            )
            conn.commit()
            print(f"Deleted {len(dead)} stale rows.")
        elif dead:
            print("(dry-run: nothing deleted)")
        return len(dead)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; write nothing")
    ap.add_argument("--prune-missing", action="store_true",
                    help="DELETE tracks rows whose file no longer exists on disk")
    args = ap.parse_args()

    from agent.config import load_config
    music_path = load_config().library.music_path
    print(f"DB:        {db.DB_PATH}")
    print(f"Music dir: {music_path}")
    if not music_path.exists():
        print("FATAL: music dir does not exist (volume unmounted?)")
        return 1

    if not args.dry_run:
        init_db()

    # Pre-scan existing paths so we can report inserted vs updated.
    conn = get_db()
    try:
        existing = {r["path"] for r in conn.execute("SELECT path FROM tracks")}
    finally:
        conn.close()

    inserted = updated = skipped = 0
    genre_counts: dict[str, int] = {}
    for f in iter_audio_files(music_path):
        fields = read_track_tags(f)
        if fields is None:
            skipped += 1
            continue
        # analyzed_at only when the planner-critical fields are really there.
        if fields["bpm"] and fields["key_camelot"]:
            fields["analyzed_at"] = time.time()
        rel = str(f.relative_to(music_path))
        is_update = rel in existing or str(f) in existing
        if not args.dry_run:
            upsert_track(
                path=str(f),
                title=fields.pop("title"),
                artist=fields.pop("artist"),
                genre=fields["genre"],
                **{k: v for k, v in fields.items() if k != "genre"},
            )
        if is_update:
            updated += 1
        else:
            inserted += 1
        g = fields.get("genre") or "(none)"
        genre_counts[g] = genre_counts.get(g, 0) + 1

    pruned = 0
    if args.prune_missing:
        pruned = prune_missing(music_path, args.dry_run)

    print("\n=== Ingestion report ===")
    mode = "DRY-RUN — no writes" if args.dry_run else "written"
    print(f"Mode:     {mode}")
    print(f"Inserted: {inserted}")
    print(f"Updated:  {updated}")
    print(f"Skipped:  {skipped}")
    if args.prune_missing:
        print(f"Pruned:   {pruned}")
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        analyzed = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE analyzed_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"DB now:   {total} tracks, {analyzed} analyzed")
    print("Per-genre (files seen this run):")
    for g, n in sorted(genre_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {g:24s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
