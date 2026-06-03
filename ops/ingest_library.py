"""Unified "ingest library" entrypoint — import-or-analyze a directory/file.

E3 single command. Dispatches based on what you point it at:

  * a Rekordbox `*.xml`        → ops.import_rekordbox  (zero librosa)
  * a `_Serato_` dir (or a dir
    containing one)            → ops.import_serato      (zero librosa where
                                                         file tags exist)
  * any other directory of
    audio files               → scan_library() + librosa backfill for
                                 anything still missing analysis.

After any import, tracks that STILL lack `mix_out_seconds` (e.g. Serato files
with no cue tags) are handed to the existing librosa fallback so coverage
reaches 100%. Pass --no-librosa to skip the slow pass (import-only).

Goal: a DJ's pre-analyzed library imports in <60s with ZERO librosa.

Run:
    python3 -m ops.ingest_library ~/exports/rekordbox.xml
    python3 -m ops.ingest_library ~/Music/_Serato_
    python3 -m ops.ingest_library ~/Music/DJTreta            # plain folder
    python3 -m ops.ingest_library ~/Music/DJTreta --no-librosa
"""
import sys
from pathlib import Path

from agent.db import init_db


def _looks_like_serato(path: Path) -> bool:
    if path.name == "_Serato_" and (path / "Subcrates").exists():
        return True
    return (path / "_Serato_" / "Subcrates").exists()


def ingest(target: str, run_librosa: bool = True, verbose: bool = True) -> dict:
    init_db()  # ensure E3 schema exists
    p = Path(target).expanduser()
    summary = {"mode": None}

    if p.is_file() and p.suffix.lower() == ".xml":
        from ops.import_rekordbox import import_collection
        summary = {"mode": "rekordbox", **import_collection(str(p), verbose=verbose)}
    elif p.is_dir() and _looks_like_serato(p):
        from ops.import_serato import import_serato_library
        summary = {"mode": "serato", **import_serato_library(str(p), verbose=verbose)}
    elif p.is_dir():
        from agent.db import scan_library
        scan_library(p)
        summary = {"mode": "scan"}
        if verbose:
            print(f"Scanned plain folder {p} into DB", flush=True)
    else:
        raise SystemExit(f"Don't know how to ingest: {target}")

    if run_librosa:
        n = _librosa_backfill(verbose)
        summary["librosa_backfilled"] = n
    return summary


def _librosa_backfill(verbose: bool) -> int:
    """Run the existing librosa fallback for tracks still missing analysis.

    Reuses ops/backfill_analysis.py's per-track logic but counts only what's
    actually missing. Returns the number of tracks analyzed."""
    import json
    import time
    from agent.audio_analysis import analyze_audio
    from agent.camelot import KEY_TO_CAMELOT
    from agent.db import get_db, upsert_track
    from agent.playback_applier import resolve_track_path

    db = get_db()
    try:
        missing = [r["path"] for r in db.execute(
            "SELECT path FROM tracks WHERE mix_out_seconds IS NULL"
        ).fetchall()]
    finally:
        db.close()

    if verbose:
        print(f"librosa fallback: {len(missing)} track(s) need analysis", flush=True)
    done = 0
    for path in missing:
        real = resolve_track_path(path) or (path if Path(path).exists() else None)
        if not real:
            continue
        try:
            a = analyze_audio(real)
            upsert_track(
                path=path,
                bpm=a["bpm"], key_musical=a["key"],
                key_camelot=KEY_TO_CAMELOT.get(a["key"], ""),
                energy_peak=a["energy_peak"],
                duration_seconds=a["duration_seconds"],
                mix_in_seconds=a["mix_in_seconds"],
                mix_out_seconds=a["mix_out_seconds"],
                timeline=json.dumps(a["timeline"]),
                analysis_source="librosa",
                analyzed_at=time.time(),
            )
            done += 1
        except Exception as e:
            if verbose:
                print(f"  FAIL {Path(path).name[:48]}: {e}", flush=True)
        time.sleep(0.2)  # breathe — protect a live stream if one's running
    if verbose:
        print(f"librosa fallback DONE: {done} analyzed", flush=True)
    return done


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: python3 -m ops.ingest_library <xml|serato-dir|music-dir> "
              "[--no-librosa]")
        sys.exit(1)
    ingest(args[0], run_librosa="--no-librosa" not in sys.argv)
