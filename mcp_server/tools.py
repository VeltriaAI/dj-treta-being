"""MCP tool implementations for DJ Treta.

Read-only (safe, fast):
    dj_status          — compact now-playing + set info
    dj_playlist        — ranked upcoming candidates
    dj_session_state   — full session.json snapshot (debug)
    dj_search_library  — fuzzy search the local library DB

Write — via command file (agent-mediated, asynchronous):
    dj_talk            — conversational intent to Being
    dj_set_mood        — change mood + replan
    dj_skip            — skip current track (fast or smooth)
    dj_request_track   — queue a specific artist/title for fetch
    dj_feedback        — like / dislike current track
    dj_set_sources     — toggle youtube / originals source

Write — direct to Mixxx (fast, live mixer controls):
    dj_deck_play       — unpause / start a deck
    dj_deck_pause      — pause a deck
    dj_set_volume      — set deck volume 0.0..1.0
    dj_set_crossfader  — set crossfader 0.0..1.0 (0 = deck1, 1 = deck2)
    dj_set_eq          — set EQ band (hi/mid/lo) for a deck
    dj_set_filter      — set quick-effect filter
    dj_load_track      — load an absolute path onto a deck (no ownership)

Co-being hooks (Phase 7 — DJ agent honours reservations):
    dj_take_deck
    dj_release_deck
    dj_load_on_deck
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from .session_writer import (
    read_session_json,
    read_state_json,
    wait_for_command_result,
    write_command,
)


MIXXX_URL = "http://localhost:7778"
DECK_OWNERSHIP_FILE = Path("/tmp/dj-treta-deck-ownership.json")


# ────────────────────────── read-only ──────────────────────────

def dj_status() -> Dict[str, Any]:
    """Compact now-playing snapshot: current track, next track, mood,
    energy, set elapsed/remaining, emergency count, playlist depth.
    """
    state = read_state_json()
    session = read_session_json()

    current = state.get("current_track") or {}
    next_track = state.get("next_track") or {}
    set_info = state.get("set") or {}

    playlist = session.get("playlist") or {}
    candidates = playlist.get("tracks") or []
    downloaded = [t for t in candidates if t.get("downloaded", False)]

    return {
        "phase": state.get("phase"),
        "mood": state.get("mood") or session.get("mood"),
        "mood_profile": session.get("mood_profile") or {},
        "tracks_played": state.get("tracks_played"),
        "current_track": {
            "title": current.get("title"),
            "deck": current.get("deck"),
            "bpm": current.get("bpm"),
            "key": current.get("key"),
            "remaining": current.get("remaining"),
            "position": current.get("position"),
            "duration": current.get("duration"),
            "file_path": current.get("file_path"),
        } if current else None,
        "next_track": {
            "title": next_track.get("title"),
            "deck": next_track.get("deck"),
            "file_path": next_track.get("file_path"),
        } if next_track else None,
        "set": {
            "id": set_info.get("id"),
            "number": set_info.get("number"),
            "title": set_info.get("title"),
            "mood": set_info.get("mood"),
            "elapsed_s": set_info.get("elapsed"),
            "remaining_s": set_info.get("remaining"),
            "target_minutes": set_info.get("target_minutes"),
            "peak_energy": set_info.get("peak_energy"),
        },
        "user_intent": session.get("user_intent") or "",
        "planner_directive": session.get("planner_directive") or "",
        "dj_directive": session.get("dj_directive") or "",
        "playlist_depth": len(candidates),
        "downloaded_depth": len(downloaded),
        "last_command_id": state.get("last_command_id"),
        "last_result": state.get("last_result"),
    }


def dj_playlist() -> Dict[str, Any]:
    """Ranked upcoming candidates with downloaded flags."""
    session = read_session_json()
    playlist = session.get("playlist") or {}
    tracks = playlist.get("tracks") or []
    compact = [
        {
            "rank": t.get("rank"),
            "title": t.get("title"),
            "bpm": t.get("bpm"),
            "key_camelot": t.get("key_camelot"),
            "energy": t.get("energy"),
            "downloaded": t.get("downloaded", False),
            "path": t.get("path"),
            "video_id": t.get("video_id"),
            "mbid": t.get("mbid"),
            "reason": t.get("reason"),
            "transition_hint": t.get("transition_hint"),
        }
        for t in tracks
    ]
    return {
        "count": len(compact),
        "tracks": compact,
        "planned_at": playlist.get("planned_at"),
        "mood_snapshot": playlist.get("mood_snapshot"),
        "reasoning_summary": playlist.get("reasoning_summary"),
    }


def dj_session_state() -> Dict[str, Any]:
    """Full session.json snapshot — for debugging and deep monitoring."""
    return read_session_json()


# ────────────────────────── write ──────────────────────────

def dj_talk(message: str) -> Dict[str, Any]:
    """Send DJ Treta a conversational intent. She'll respond via her Being
    agent. Returns the response if available within 10s, otherwise
    acknowledges fire-and-forget.

    Examples: "play something darker", "bring it up a notch", "how are you".
    """
    if not message or not message.strip():
        return {"ok": False, "message": "message is required"}
    cmd_id = write_command("talk", {"message": message, "readonly": False})
    # Being responses can take a few seconds — wait up to 30s.
    outcome = wait_for_command_result(cmd_id, timeout=30.0)
    return {
        "ok": True,
        "message": outcome["result"] if outcome["processed"] else "queued (Being will respond asynchronously)",
        "cmd_id": cmd_id,
        "processed": outcome["processed"],
        "elapsed_s": round(outcome["elapsed"], 2),
    }


def dj_set_mood(mood: str) -> Dict[str, Any]:
    """Change DJ Treta's current mood. Triggers mood resolver + replan.

    Accepts natural language ("darker techno", "afro house", "uplifting
    progressive") — the daemon's mood resolver canonicalises it.
    """
    if not mood or not mood.strip():
        return {"ok": False, "message": "mood is required"}
    cmd_id = write_command("change_mood", {"mood": mood.strip()})
    outcome = wait_for_command_result(cmd_id, timeout=5.0)
    return {
        "ok": True,
        "message": outcome["result"] if outcome["processed"] else f"mood change to '{mood}' queued",
        "cmd_id": cmd_id,
    }


def dj_skip(style: str = "fast") -> Dict[str, Any]:
    """Skip current track.

    style:
      - "fast"   → hard crossfade, ~2s
      - "smooth" → graceful crossfade over ~10s
    """
    style = (style or "fast").lower()
    if style not in ("fast", "smooth"):
        return {"ok": False, "message": "style must be 'fast' or 'smooth'"}
    # The existing skip command emits user_skip{style:"fast"}. For "smooth"
    # we route the preference through the command args — agent reads
    # args.style if present (falls back to fast if not wired yet).
    cmd_id = write_command("skip", {"style": style})
    outcome = wait_for_command_result(cmd_id, timeout=3.0)
    return {
        "ok": True,
        "message": outcome["result"] if outcome["processed"] else f"skip ({style}) signaled",
        "cmd_id": cmd_id,
    }


def dj_request_track(artist: str, title: str) -> Dict[str, Any]:
    """Request DJ Treta to fetch and queue a specific track.

    Routes a targeted library_need signal via the command-file channel.
    The library agent (K5) picks it up, searches YouTube, downloads,
    canonicalises, and queues the track for the planner.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return {"ok": False, "message": "both artist and title are required"}
    # Route via a talk intent — the Being will call the library tools.
    # This avoids needing a new command handler in the daemon right now.
    msg = (
        f"Please fetch and queue the track '{title}' by {artist}. "
        "Use the library agent to search and download it."
    )
    cmd_id = write_command("talk", {"message": msg, "readonly": False})
    return {
        "ok": True,
        "message": f"request for '{artist} — {title}' submitted to Being",
        "cmd_id": cmd_id,
    }


def dj_feedback(kind: str) -> Dict[str, Any]:
    """Mark the current track as liked or disliked.

    kind: 'like' or 'dislike' (case-insensitive). Routes to the Being's
    feedback command; the planner reads feedback history to bias future
    recommendations.
    """
    kind = (kind or "").lower().strip()
    if kind not in ("like", "dislike"):
        return {"ok": False, "message": "kind must be 'like' or 'dislike'"}
    cmd_id = write_command("feedback", {"type": kind})
    outcome = wait_for_command_result(cmd_id, timeout=3.0)
    return {
        "ok": True,
        "message": outcome["result"] if outcome["processed"] else f"{kind} queued",
        "cmd_id": cmd_id,
    }


def dj_set_sources(source: str, enabled: bool = True) -> Dict[str, Any]:
    """Toggle a music source on or off.

    source: 'youtube' (alias 'yt') or 'originals'.
    enabled: True to enable, False to disable.
    """
    source = (source or "").lower().strip()
    if source == "yt":
        source = "youtube"
    if source not in ("youtube", "originals"):
        return {"ok": False, "message": "source must be 'youtube' or 'originals'"}
    cmd_id = write_command(
        "change_sources", {"source": source, "enabled": bool(enabled)}
    )
    outcome = wait_for_command_result(cmd_id, timeout=3.0)
    return {
        "ok": True,
        "message": outcome["result"]
        if outcome["processed"]
        else f"source {source} → {'on' if enabled else 'off'}",
        "cmd_id": cmd_id,
    }


# ──────────────────────── direct mixer / deck ────────────────────────
#
# These tools post directly to the Mixxx HTTP API on the same host as the
# daemon (localhost:7778 on the VM). They are NOT routed through the
# Being's command file — they're sub-second operations an AI agent or
# co-being wants to feel immediately. The DJ agent still owns track
# loading + transitions; these are the "hands on the mixer" controls.


def _mixxx_post(path: str, payload: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    """Thin wrapper around a Mixxx POST. Returns the normalised status dict."""
    try:
        resp = httpx.post(f"{MIXXX_URL}{path}", json=payload, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "message": f"mixxx unreachable: {exc}"}
    ok = resp.status_code < 400
    body = ""
    try:
        body = resp.text[:200]
    except Exception:
        pass
    return {
        "ok": ok,
        "message": body or (f"HTTP {resp.status_code}"),
        "status": resp.status_code,
    }


def dj_deck_play(deck_num: int) -> Dict[str, Any]:
    """Resume / start playback on a deck. deck_num: 1 or 2."""
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    return _mixxx_post("/api/play", {"deck": deck_num})


def dj_deck_pause(deck_num: int) -> Dict[str, Any]:
    """Pause a deck. deck_num: 1 or 2.

    Note: pausing the deck currently routed to the live stream will cause
    dead air. Prefer dj_skip for set-time interruptions.
    """
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    return _mixxx_post("/api/pause", {"deck": deck_num})


def dj_set_volume(deck_num: int, value: float) -> Dict[str, Any]:
    """Set deck volume. value: 0.0 (silent) to 1.0 (unity)."""
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    try:
        v = max(0.0, min(1.0, float(value)))
    except Exception:
        return {"ok": False, "message": "value must be a number"}
    # Mixxx /api/volume expects {"deck", "level"} — not "volume"
    return _mixxx_post("/api/volume", {"deck": deck_num, "level": v})


def dj_set_crossfader(value: float) -> Dict[str, Any]:
    """Set the crossfader position.

    value: 0.0 = full Deck 1, 0.5 = center, 1.0 = full Deck 2.
    (Matches Mixxx /api/crossfade convention.)
    """
    try:
        v = max(0.0, min(1.0, float(value)))
    except Exception:
        return {"ok": False, "message": "value must be a number"}
    return _mixxx_post("/api/crossfade", {"position": v})


def dj_set_eq(deck_num: int, band: str, value: float) -> Dict[str, Any]:
    """Set an EQ band on a deck.

    band: 'hi', 'mid', or 'lo' (also accepts 'high'/'low').
    value: 0.0 (cut) .. 1.0 (unity) .. ~4.0 (boost). 1.0 is neutral.
    """
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    b = (band or "").lower().strip()
    if b == "high":
        b = "hi"
    if b == "low":
        b = "lo"
    if b not in ("hi", "mid", "lo"):
        return {"ok": False, "message": "band must be hi / mid / lo"}
    try:
        v = max(0.0, min(4.0, float(value)))
    except Exception:
        return {"ok": False, "message": "value must be a number"}
    # Mixxx /api/eq uses the band name as the JSON key (hi/mid/lo), not a
    # generic {"band": ..., "value": ...} pair. See apiserver.cpp /api/eq.
    return _mixxx_post("/api/eq", {"deck": deck_num, b: v})


def dj_set_filter(deck_num: int, value: float) -> Dict[str, Any]:
    """Set the quick-effect filter on a deck.

    value: 0.0 = full high-pass, 0.5 = neutral, 1.0 = full low-pass.
    """
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    try:
        v = max(0.0, min(1.0, float(value)))
    except Exception:
        return {"ok": False, "message": "value must be a number"}
    return _mixxx_post("/api/filter", {"deck": deck_num, "value": v})


def dj_load_track(deck_num: int, path: str) -> Dict[str, Any]:
    """Load an absolute filesystem path onto a deck directly via Mixxx.

    Does NOT claim deck ownership. Use dj_take_deck + dj_load_on_deck if
    you need to lock the deck against DJ Treta's auto-load. This tool is
    for the operator who IS the DJ.
    """
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    p = (path or "").strip()
    if not p:
        return {"ok": False, "message": "path is required"}
    if not p.startswith("/"):
        return {"ok": False, "message": "path must be absolute"}
    import os as _os
    if not _os.path.exists(p):
        return {"ok": False, "message": f"file not found: {p}"}
    return _mixxx_post("/api/load", {"deck": deck_num, "track": p})


# ──────────────────────── library search ────────────────────────

DB_PATH_CANDIDATES = [
    "/mnt/data/dj-treta/djtreta.db",
    str(Path.home() / "beings" / "dj-treta" / "djtreta.db"),
    str(Path(__file__).resolve().parent.parent / "djtreta.db"),
]


def _find_db_path() -> str | None:
    for p in DB_PATH_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def dj_search_library(query: str, limit: int = 10) -> Dict[str, Any]:
    """Fuzzy-search the local library DB by title or artist.

    Returns up to `limit` matches with BPM, key, energy and absolute path.
    The path is what clients can feed into dj_load_track or dj_load_on_deck.
    """
    q = (query or "").strip().lower()
    if not q:
        return {"ok": False, "message": "query is required", "tracks": []}
    try:
        limit = max(1, min(50, int(limit)))
    except Exception:
        limit = 10

    db_path = _find_db_path()
    if not db_path:
        return {"ok": False, "message": "library DB not found", "tracks": []}

    import sqlite3
    like = f"%{q}%"
    rows = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT path, title, canonical_artist, bpm, key_camelot, energy_peak "
                "FROM tracks "
                "WHERE LOWER(title) LIKE ? OR LOWER(canonical_artist) LIKE ? "
                "LIMIT ?",
                (like, like, limit),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            # Older schema — fall back to title-only
            cur = conn.execute(
                "SELECT path, title, NULL as canonical_artist, bpm, "
                "key_camelot, energy_peak FROM tracks "
                "WHERE LOWER(title) LIKE ? LIMIT ?",
                (like, limit),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "message": f"DB error: {exc}", "tracks": []}

    tracks = [
        {
            "path": r["path"],
            "title": r["title"],
            "artist": r["canonical_artist"],
            "bpm": r["bpm"],
            "key": r["key_camelot"],
            "energy": r["energy_peak"],
        }
        for r in rows
    ]
    return {"ok": True, "count": len(tracks), "tracks": tracks}


# ──────────────────── co-being deck ownership (Phase 7 stubs) ────────────────────

def _read_deck_ownership() -> Dict[str, Any]:
    try:
        return json.loads(DECK_OWNERSHIP_FILE.read_text())
    except Exception:
        return {}


def _write_deck_ownership(data: Dict[str, Any]) -> None:
    tmp = DECK_OWNERSHIP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(DECK_OWNERSHIP_FILE)


def dj_take_deck(deck_num: int, being_id: str) -> Dict[str, Any]:
    """Reserve a deck for external (co-being) control. DJ Treta's agent
    will skip that deck in auto-load decisions once Phase 7 lands.

    NOTE (Phase 6): deck ownership is recorded in a sidecar file but
    not yet honoured by the DJ agent. The contract is stable; honouring
    ships in v9 Phase 7.
    """
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    if not being_id or not being_id.strip():
        return {"ok": False, "message": "being_id is required"}
    ownership = _read_deck_ownership()
    current = ownership.get(str(deck_num))
    if current and current.get("being_id") != being_id.strip():
        return {
            "ok": False,
            "message": (
                f"deck {deck_num} is already held by {current.get('being_id')}; "
                "call dj_release_deck first"
            ),
        }
    ownership[str(deck_num)] = {
        "being_id": being_id.strip(),
        "taken_at": time.time(),
    }
    _write_deck_ownership(ownership)
    return {
        "ok": True,
        "message": f"deck {deck_num} reserved for {being_id}",
        "phase_7_ready": True,
    }


def dj_release_deck(deck_num: int) -> Dict[str, Any]:
    """Release a previously-taken deck back to DJ Treta."""
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    ownership = _read_deck_ownership()
    if str(deck_num) not in ownership:
        return {"ok": True, "message": f"deck {deck_num} was not held (no-op)"}
    prev = ownership.pop(str(deck_num))
    _write_deck_ownership(ownership)
    return {
        "ok": True,
        "message": f"deck {deck_num} released (was held by {prev.get('being_id')})",
    }


def _resolve_mbid_to_path(mbid: str) -> str | None:
    """Resolve an MBID to a local filesystem path via the library DB.

    Phase 7: uses agent.knowledge.merge.find_local_by_mbid when the `mbid`
    column exists in the local tracks table. Returns None on any miss.
    """
    mbid = (mbid or "").strip()
    if not mbid:
        return None
    try:
        from agent.knowledge.merge import find_local_by_mbid  # type: ignore
    except Exception:
        return None
    try:
        rows = find_local_by_mbid([mbid])
    except Exception:
        return None
    row = rows.get(mbid)
    if not row:
        return None
    return row.get("path") or row.get("file_path")


def dj_load_on_deck(
    deck_num: int,
    path_or_mbid: str,
    being_id: str | None = None,
) -> Dict[str, Any]:
    """Co-being: load a specific track onto a deck the caller has reserved.

    Requires the deck to have been taken via dj_take_deck. Posts directly
    to Mixxx /api/load — DJ Treta's heartbeat honours the ownership file
    and will NOT overwrite reserved decks (Phase 7).

    Args:
        deck_num: 1 or 2.
        path_or_mbid: absolute filesystem path, OR 'mbid:XXXX' to resolve
            against the local library DB.
        being_id: optional; if supplied, must match the current holder.
    """
    if deck_num not in (1, 2):
        return {"ok": False, "message": "deck_num must be 1 or 2"}
    if not path_or_mbid or not path_or_mbid.strip():
        return {"ok": False, "message": "path_or_mbid is required"}

    ownership = _read_deck_ownership()
    holder = ownership.get(str(deck_num))
    if not holder:
        return {
            "ok": False,
            "message": f"deck {deck_num} is not reserved — call dj_take_deck first",
        }

    holder_id = holder.get("being_id") if isinstance(holder, dict) else holder
    if being_id and holder_id != being_id.strip():
        return {
            "ok": False,
            "message": (
                f"deck {deck_num} is held by {holder_id}, not {being_id.strip()}"
            ),
        }

    raw = path_or_mbid.strip()
    if raw.startswith("mbid:"):
        resolved = _resolve_mbid_to_path(raw[5:])
        if not resolved:
            return {
                "ok": False,
                "message": (
                    f"mbid {raw[5:]!r} not found in local library — "
                    "use dj_request_track to fetch it first"
                ),
            }
        target = resolved
    else:
        target = raw

    import os as _os
    if not target.startswith("/"):
        return {"ok": False, "message": "path must be absolute (start with /)"}
    if not _os.path.exists(target):
        return {"ok": False, "message": f"file not found on disk: {target}"}

    try:
        resp = httpx.post(
            f"{MIXXX_URL}/api/load",
            json={"deck": deck_num, "track": target},
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as exc:
        return {"ok": False, "message": f"Mixxx load failed: {exc}"}

    return {
        "ok": True,
        "message": f"loaded {target!r} on deck {deck_num} for {holder_id}",
        "deck": deck_num,
        "path": target,
    }
