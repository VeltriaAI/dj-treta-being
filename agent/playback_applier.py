"""Thin Mixxx load helper — no decisions, just applies a pick to the idle deck.

Phase 3 of v8 splits track SELECTION (planner writes session.playlist) from
track LOADING (this module posts to Mixxx /api/load). Phase 4 moves the
selection step from heartbeat into the DJ agent itself; this module stays as
the last-mile executor either way.

All functions are Mixxx-only — they do not read mood, playlist, or SQL.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")

_AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a")


def resolve_track_path(track_path: str, music_dir: Path | str | None = None) -> str | None:
    """Resolve a stored track_path to a real filesystem path on this machine.

    DB rows historically held absolute Mac paths (`/Users/manish.pratap/...`)
    that don't resolve on Linux VMs, breaking `load_on_deck`. This resolver
    is the single source of truth for translating *any* stored path
    (absolute Mac, absolute Linux, relative, just a filename, or a renamed
    file) into a path that exists on the current filesystem.

    Resolution order:
      1. As-given absolute path that exists.
      2. Joined with `music_dir` (handles relative paths).
      3. Just the basename joined with `music_dir` (handles paths from
         a different machine where only the filename is portable).
      4. Fuzzy normalized-name search across `music_dir`'s genre subdirs
         (handles cross-OS Mac→VM, renames, partial input).

    Returns the resolved absolute path string, or None if nothing matched.
    """
    if not track_path:
        return None

    # Resolve music_dir lazily from config to avoid forcing every caller
    # to pass it; tests can inject explicitly.
    if music_dir is None:
        try:
            from .config import load_config
            music_dir = load_config().library.music_path
        except Exception:
            music_dir = Path.home() / "Music" / "DJTreta"
    music_dir = Path(music_dir).expanduser()

    # 1. As-given absolute path that exists.
    p = Path(track_path).expanduser()
    if p.is_absolute() and p.exists():
        return str(p)

    # 2. Joined with music_dir (relative path).
    if not p.is_absolute():
        candidate = music_dir / p
        if candidate.exists():
            return str(candidate)

    # 3. Just the basename joined with music_dir.
    basename = p.name
    if basename:
        # Walk music_dir/<genre>/<basename> for any genre subdir.
        if music_dir.exists():
            for genre_dir in music_dir.iterdir():
                if genre_dir.is_dir() and not genre_dir.name.startswith("."):
                    candidate = genre_dir / basename
                    if candidate.exists():
                        return str(candidate)

    # 4. Fuzzy normalized-name search.
    if not music_dir.exists():
        return None
    try:
        from .tools.helpers import _normalize_for_search
    except Exception:
        return None
    query = _normalize_for_search(basename or track_path)
    for genre_dir in sorted(music_dir.iterdir()):
        if not genre_dir.is_dir() or genre_dir.name.startswith("."):
            continue
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() not in _AUDIO_EXTENSIONS:
                continue
            if query in _normalize_for_search(f.stem) or query in _normalize_for_search(f.name):
                return str(f)
    return None


def load_on_deck(mixxx_url: str, deck: int, track_path: str, timeout: float = 5.0) -> bool:
    """Post a single track to a Mixxx deck. Returns True on success.

    Resolves `track_path` against the local filesystem first (handles cross-
    machine path differences — see `resolve_track_path`). On miss, logs a
    warning and returns False without hitting Mixxx.

    Does not wait for the track to analyze / cue up — Mixxx handles that
    asynchronously. Callers can poll /api/deck/N/track_info to confirm.
    """
    resolved = resolve_track_path(track_path)
    if not resolved:
        log.warning(
            f"Load deck {deck}: track not found on filesystem: {track_path!r} "
            f"(tried as absolute, relative, basename, fuzzy search)"
        )
        return False
    try:
        resp = httpx.post(
            f"{mixxx_url}/api/load",
            json={"deck": deck, "track": resolved},
            timeout=timeout,
        )
        result = resp.json() if resp.status_code == 200 else {}
        if result.get("ok"):
            log.info(f"Loaded deck {deck}: {Path(resolved).stem[:60]}")
            return True
        log.warning(f"Load deck {deck} returned not-ok: {result}")
        return False
    except Exception as exc:
        log.warning(f"Load deck {deck} failed: {exc}")
        return False


def get_deck_paths(mixxx_url: str) -> dict:
    """Return {deck_number: file_path} for both decks, normalized to the
    same path-space as session/playlist/DB (relative to library.music_dir).

    Mixxx's HTTP layer returns absolute paths; without normalizing here the
    set returned would never match `track["path"]` from the playlist (which
    became relative in the v9 portability migration), so dedup checks
    (planner exclude_paths, heartbeat duplicate detection) silently miss.
    Missing → empty string.
    """
    from .db import _normalize_track_path
    out = {1: "", 2: ""}
    for dk in (1, 2):
        try:
            resp = httpx.get(f"{mixxx_url}/api/deck/{dk}/track_info", timeout=2)
            info = resp.json() if resp.status_code == 200 else {}
            raw = info.get("file_path", "") or ""
            out[dk] = _normalize_track_path(raw) if raw else ""
        except Exception:
            pass
    return out


def refresh_duration(mixxx_url: str, deck: int, track_path: str) -> None:
    """After a load, read Mixxx's duration back and upsert to DB.

    Librosa analysis sometimes misses duration on unusual files; Mixxx always
    knows once it has parsed the file. Call this shortly after `load_on_deck`.
    """
    try:
        time.sleep(1.0)  # give Mixxx a moment to parse
        from .main import _get_status
        st = _get_status(mixxx_url)
        if not st:
            return
        duration = float(st.get(f"deck{deck}", {}).get("duration", 0) or 0)
        if duration > 0:
            from .db import upsert_track
            upsert_track(path=track_path, duration_seconds=duration)
    except Exception as exc:
        log.debug(f"refresh_duration skipped: {exc}")
