"""Maintain hidden symlink-folders so the in-Mixxx DJ Treta sidebar can browse
track *lists* (Library / Planned / Suggestions) using Mixxx's normal folder
browser — which only shows a single folder's direct files.

The daemon owns three folders under the music dir:
  _all/          → a symlink to every real track (the "Library" node)
  _planned/      → the planner's queue, in play order (NN_ prefix preserves it)
  _suggestions/  → the current Sarathi transition pick

Mixxx's BrowseTableModel follows symlinks, so these render as normal tracks and
load via the FLX4. Rebuilt cheaply on each state tick; only rewritten when the
contents change. APFS (local library) — no AppleDouble concerns.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("dj-treta")

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a")
_SYNTH_DIRS = ("_all", "_planned", "_suggestions")


def is_synthetic_dir(name: str) -> bool:
    """True for the daemon-managed symlink folders (excluded from Genres)."""
    return name in _SYNTH_DIRS


def _real_tracks(music_dir: Path) -> list[Path]:
    """Every real audio file across genre subfolders (skips dotfiles + the
    synthetic dirs themselves)."""
    out = []
    for genre in music_dir.iterdir():
        if not genre.is_dir() or genre.name.startswith(".") or is_synthetic_dir(genre.name):
            continue
        for f in genre.iterdir():
            if f.name.startswith(".") or f.suffix.lower() not in AUDIO_EXTS:
                continue
            if f.is_symlink():  # don't recurse our own links
                continue
            out.append(f)
    return out


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", name)


def _sync_dir(target: Path, links: list[tuple[str, Path]]) -> bool:
    """Make `target` contain exactly the given (linkname → source) symlinks.
    Returns True if anything changed. Best-effort."""
    try:
        target.mkdir(parents=True, exist_ok=True)
        want = {name: src for name, src in links}
        have = {p.name: p for p in target.iterdir()}
        changed = False
        # Remove stale links.
        for name, p in have.items():
            if name not in want:
                try:
                    p.unlink()
                    changed = True
                except Exception:
                    pass
        # Add/refresh.
        for name, src in want.items():
            link = target / name
            try:
                if link.is_symlink() and link.resolve() == src.resolve():
                    continue
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(src)
                changed = True
            except Exception:
                pass
        return changed
    except Exception as exc:
        log.debug(f"[browse] sync {target.name} failed: {exc}")
        return False


def sync_browse_folders(music_dir: Path, session) -> None:
    """Refresh _all / _planned / _suggestions from current state. Cheap; only
    rewrites links that changed."""
    try:
        music_dir = Path(music_dir).expanduser()
        if not music_dir.is_dir():
            return

        # _all — every real track (Library node).
        all_tracks = _real_tracks(music_dir)
        _sync_dir(music_dir / "_all",
                  [(_safe(f.name), f) for f in all_tracks])

        # _planned — planner queue in play order (NN_ prefix keeps order).
        planned = []
        pl = getattr(session, "playlist", None) if session else None
        for i, t in enumerate((pl or {}).get("tracks", [])):
            p = t.get("path", "")
            if not p:
                continue
            src = Path(p)
            if src.exists():
                planned.append((f"{i + 1:02d}_{_safe(src.name)}", src))
        _sync_dir(music_dir / "_planned", planned)

        # _suggestions — the current Sarathi pick (if any).
        sugg = []
        try:
            from .tools.sarathi import list_pending_suggestions
            for s in list_pending_suggestions():
                # The suggestion carries a track_title, not a path; resolve by
                # matching a real track filename. Best-effort.
                title = (s.get("track_title") or "").lower()
                if not title:
                    continue
                for f in all_tracks:
                    if title[:20] in f.name.lower():
                        sugg.append((_safe(f.name), f))
                        break
        except Exception:
            pass
        _sync_dir(music_dir / "_suggestions", sugg)
    except Exception as exc:
        log.debug(f"[browse] sync_browse_folders failed (non-fatal): {exc}")
