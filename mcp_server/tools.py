"""MCP tool implementations for DJ Treta.

Ten tools in three groups:

Read-only (safe, fast):
    dj_status          — compact now-playing + set info
    dj_playlist        — ranked upcoming candidates
    dj_session_state   — full session.json snapshot (debug)

Write (via command file, agent-mediated):
    dj_talk            — conversational intent to Being
    dj_set_mood        — change mood + replan
    dj_skip            — skip current track (fast or smooth)
    dj_request_track   — queue a specific artist/title for fetch

Co-being hooks (Phase 7 stubs — contract stable now):
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
