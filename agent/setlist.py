"""Setlist mode (2026-07-29) — hand DJ Treta a prepared set and she plays it.

Until now the track ORDER was always hers: the planner picked the next track
every cycle. There was no way to say "play this, in this order". A prepared
set (Manish's Bolly-Techno hour, a wedding sequence, a client brief) needs
the opposite contract.

The split stays the one that has worked all along — **code owns selection,
she owns the craft**:

  * The setlist is the queue. While one is loaded the planner does NOT run:
    the playlist is synthesised deterministically from the setlist, so no
    model can reorder or drop a track.
  * She still mixes it: phrase-lock, bar-quantised bass swaps, the blend
    length and technique carried per track, cue points from the set folder.
    Everything she learned still applies — only "what's next" is fixed.

Position is DERIVED, never stored as a counter: it's the first setlist entry
not yet in `tracks_played`. So a restart, a manual skip, or Manish loading a
deck by hand all self-correct instead of desyncing a pointer.

Sources: a `.m3u8` playlist (optionally with a sibling `cues.json` from the
set builder, which carries bpm/key/energy/blend/cues) or a plain folder of
audio files sorted by name.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("dj-treta")

AUDIO_EXTS = {".mp3", ".aiff", ".aif", ".wav", ".flac", ".m4a", ".ogg"}

# Phase → blend length in bars, used when the set folder carries no explicit
# blend. Mirrors the set builder: shorter cuts at peak, long blends elsewhere.
_PHASE_BLEND_BARS = {"WARMUP": 32, "BUILD": 32, "PEAK": 16, "SETTLE": 32}
_DEFAULT_BLEND_S = 60


def _norm(p: str | Path) -> str:
    return str(Path(p).expanduser().resolve())


def load_setlist(source: str, *, name: str = "", loop: bool = False) -> dict:
    """Build a setlist dict from an .m3u8, a cues.json, or a folder.

    Raises ValueError with a human-readable reason — the caller surfaces it
    to Manish rather than half-loading a broken set.
    """
    src = Path(source).expanduser()
    if not src.exists():
        raise ValueError(f"not found: {src}")

    meta_by_path: dict[str, dict] = {}
    entries: list[str] = []

    if src.is_dir():
        folder = src
        m3u = sorted(folder.glob("*.m3u8")) or sorted(folder.glob("*.m3u"))
        if m3u:
            src = m3u[0]
        else:
            entries = [str(f) for f in sorted(folder.iterdir())
                       if f.suffix.lower() in AUDIO_EXTS and not f.name.startswith("._")]
            if not entries:
                raise ValueError(f"no audio files in {folder}")

    if src.is_file() and src.suffix.lower() in (".m3u8", ".m3u"):
        base = src.parent
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = Path(line)
            entries.append(str(p if p.is_absolute() else (base / p)))
    elif src.is_file() and src.suffix.lower() == ".json":
        base = src.parent
        for row in json.loads(src.read_text(encoding="utf-8")):
            p = Path(row.get("file", ""))
            entries.append(str(p if p.is_absolute() else (base / p)))

    # Sibling cues.json from the set builder — bpm/key/energy/blend/cue points.
    folder = src.parent if src.is_file() else src
    cues_file = folder / "cues.json"
    if cues_file.exists():
        try:
            for row in json.loads(cues_file.read_text(encoding="utf-8")):
                f = Path(row.get("file", ""))
                meta_by_path[_norm(f if f.is_absolute() else folder / f)] = row
        except Exception as exc:
            log.warning(f"[setlist] cues.json unreadable ({exc}) — continuing without it")

    tracks, missing = [], []
    for raw in entries:
        p = Path(raw)
        if not p.exists():
            missing.append(raw)
            continue
        m = meta_by_path.get(_norm(p), {})
        # Blend length: explicit from the set folder → else derived from the
        # phase at this track's BPM (16 bars at peak for faster cuts, 32 bars
        # elsewhere — the same grid phrase-lock enforces live) → else default.
        blend = m.get("blend_s")
        if not blend:
            bars = _PHASE_BLEND_BARS.get((m.get("phase") or "").upper())
            bpm = m.get("bpm")
            blend = (bars * 4 * 60.0 / bpm) if (bars and bpm) else _DEFAULT_BLEND_S
        tracks.append({
            "path": str(p),
            "title": m.get("title") or p.stem,
            "bpm": m.get("bpm"),
            "key_camelot": m.get("camelot") or m.get("key_camelot"),
            "energy": m.get("energy"),
            "phase": m.get("phase", ""),
            "blend_s": int(max(10, min(90, float(blend)))),
            "technique": m.get("technique", "crossfade"),
            "mix_in_s": m.get("mix_in_s"),
            "mix_out_s": m.get("mix_out_s"),
            "cues": m.get("cues") or [],
        })

    if missing:
        log.warning(f"[setlist] {len(missing)} entr(ies) missing on disk, skipped: "
                    f"{missing[:3]}")
    if not tracks:
        raise ValueError(f"no playable tracks resolved from {source}")

    return {
        "name": name or folder.name,
        "source": str(src),
        "loaded_at": time.time(),
        "loop": bool(loop),
        "missing_count": len(missing),
        "tracks": tracks,
    }


def setlist_position(setlist: dict, played_paths: set[str]) -> int:
    """Index of the first setlist track not yet played. Derived, not stored."""
    tracks = (setlist or {}).get("tracks") or []
    played = {_norm(p) for p in (played_paths or set()) if p}
    for i, t in enumerate(tracks):
        if _norm(t["path"]) not in played:
            return i
    return len(tracks)


def setlist_to_playlist(setlist: dict, played_paths: set[str],
                        *, depth: int = 5) -> dict | None:
    """Synthesise a PlaylistV1 from the setlist's remaining tracks.

    Returns None when the set is finished (and not looping) — the caller then
    hands control back to the planner rather than leaving the decks dry.
    """
    tracks = (setlist or {}).get("tracks") or []
    if not tracks:
        return None
    pos = setlist_position(setlist, played_paths)
    if pos >= len(tracks):
        if not setlist.get("loop"):
            return None
        pos = 0  # loop: start the set again

    upcoming = tracks[pos:pos + depth]
    if setlist.get("loop") and len(upcoming) < depth:
        upcoming += tracks[:depth - len(upcoming)]

    out = []
    for rank, t in enumerate(upcoming, 1):
        entry = {
            "rank": rank,
            "path": t["path"],
            "title": t.get("title") or Path(t["path"]).stem,
            "reason": (f"setlist '{setlist.get('name')}' "
                       f"#{pos + rank}/{len(tracks)}"
                       + (f" [{t['phase']}]" if t.get("phase") else "")),
            "transition_hint": {
                "technique": t.get("technique") or "crossfade",
                "duration": int(t.get("blend_s") or _DEFAULT_BLEND_S),
                "at_section": "outro",
            },
        }
        for k_src, k_dst in (("bpm", "bpm"), ("key_camelot", "key_camelot"),
                             ("energy", "energy")):
            if t.get(k_src) is not None:
                entry[k_dst] = t[k_src]
        out.append(entry)

    return {
        "planned_at": time.time(),
        "mood_snapshot": (setlist.get("name") or "setlist").lower().replace(" ", "-")[:60],
        "reasoning_summary": (
            f"SETLIST MODE — '{setlist.get('name')}', track {pos + 1} of "
            f"{len(tracks)}. Order is fixed; planner is bypassed."
        ),
        "tracks": out,
    }


def setlist_status(setlist: dict | None, played_paths: set[str]) -> str:
    if not setlist:
        return "No setlist loaded — Treta picks her own tracks."
    tracks = setlist.get("tracks") or []
    pos = setlist_position(setlist, played_paths)
    done = min(pos, len(tracks))
    nxt = tracks[pos]["title"] if pos < len(tracks) else "— finished —"
    remaining = sum(1 for _ in tracks[pos:])
    return (f"Setlist '{setlist.get('name')}': {done}/{len(tracks)} played, "
            f"{remaining} to go{' (looping)' if setlist.get('loop') else ''}. "
            f"Next: {nxt}")
