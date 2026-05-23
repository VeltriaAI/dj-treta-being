"""DJ Controls -- Mixxx API: deck controls, EQ, crossfader, BPM, beat alignment."""

from pathlib import Path

from .helpers import (
    _music_dir, _normalize_for_search,
    _mixxx_failed, _mixxx_get, _mixxx_post, _dj_get, _dj_post,
)


# ===================================================================
# DJ CONTROLS -- Mixxx API
# ===================================================================

def get_dj_status() -> dict:
    """Get full DJ status -- both decks, crossfader, BPM, key, remaining time, what's playing."""
    data = _mixxx_get("/api/status")
    if err := _mixxx_failed(data):
        return {"error": err, "_request_failed": True}
    return data


def get_deck_info(deck: int) -> dict:
    """Get detailed info for a specific deck -- track title, BPM, key, position, remaining time.

    Args:
        deck: The deck number, either 1 or 2.
    """
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return {"error": err}
    return status[f"deck{deck}"]


def load_track(deck: int, track_path: str) -> str:
    """Load a track onto a deck. Accepts full path OR partial name (will search library).

    Args:
        deck: The deck number to load onto, either 1 or 2.
        track_path: Full file path OR partial track name to search for.
    """
    # In Sarathi mode Treta does NOT load decks — Manish loads + mixes himself.
    # She advises via suggest_transition instead. Refuse here so the agent can't
    # drop a track onto the deck he's mid-transition into.
    try:
        from ..session_state import get_session
        _s = get_session()
        if _s is not None and getattr(_s, "sarathi_mode", False):
            return ("SKIPPED: Sarathi mode — Manish loads the decks himself. "
                    "Use suggest_transition() to recommend the next track instead "
                    "of loading it.")
    except Exception:
        pass

    from ..playback_applier import resolve_track_path

    resolved = resolve_track_path(track_path)
    if not resolved:
        return (
            f"ERROR: Track not found in library: '{track_path}'. "
            f"Use list_library_tracks to see available tracks."
        )

    result = _mixxx_post("/api/load", {"deck": deck, "track": resolved})
    if err := _mixxx_failed(result):
        return f"ERROR: Mixxx load failed: {err}"
    if result and result.get("ok"):
        return f"Loaded on Deck {deck}: {Path(resolved).stem}"
    return f"ERROR: Mixxx rejected load: {result}"


def play_deck(deck: int) -> dict:
    """Start playback on a deck.

    Args:
        deck: The deck number to play, either 1 or 2.
    """
    return _dj_post("/api/play", {"deck": deck})


def pause_deck(deck: int) -> dict:
    """Pause playback on a deck.

    Args:
        deck: The deck number to pause, either 1 or 2.
    """
    return _dj_post("/api/pause", {"deck": deck})


def set_volume(deck: int, volume: float) -> dict:
    """Set deck volume level.

    Args:
        deck: The deck number, either 1 or 2.
        volume: Volume level from 0.0 (silent) to 1.0 (full).
    """
    # Mixxx /api/volume expects {"deck", "level"} — NOT "volume"
    return _dj_post("/api/volume", {"deck": deck, "level": volume})


def set_crossfader(position: float) -> dict:
    """Set crossfader position between decks.

    Args:
        position: 0.0 = full Deck 1, 0.5 = center, 1.0 = full Deck 2.
    """
    # Clamp to valid range
    position = max(0.0, min(1.0, position))
    return _dj_post("/api/crossfade", {"position": position})


def set_eq(deck: int, band: str, value: float) -> dict:
    """Set EQ band on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        band: EQ band name -- 'hi', 'mid', or 'lo' (also accepts 'high'/'low').
        value: EQ value from 0.0 (cut) to 4.0 (boost), 1.0 is neutral.
    """
    # Mixxx /api/eq expects the band NAME as the JSON key, not a generic
    # {"band": "hi", "value": v} pair. See apiserver.cpp /api/eq.
    b = (band or "").lower().strip()
    if b == "high":
        b = "hi"
    elif b == "low":
        b = "lo"
    return _dj_post("/api/eq", {"deck": deck, b: value})


def set_filter(deck: int, value: float) -> dict:
    """Set quick-effect filter on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        value: Filter from 0.0 (full high-pass) through 0.5 (neutral) to 1.0 (full low-pass).
    """
    return _dj_post("/api/filter", {"deck": deck, "value": value})


def set_sync(deck: int, enabled: bool) -> dict:
    """Enable or disable beat sync on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        enabled: True to enable sync, False to disable.
    """
    # Mixxx has two endpoints: /api/sync (always enables) and /api/sync_off
    # (disables). The "enabled" field is ignored on /api/sync.
    path = "/api/sync" if enabled else "/api/sync_off"
    return _dj_post(path, {"deck": deck})


def get_live_data() -> dict:
    """Get real-time data -- VU meters, beat position, crossfader. For feeling the music."""
    return _dj_get("/api/live")


def get_track_info(deck: int) -> dict:
    """Get deep track metadata from Mixxx -- title, artist, BPM, key, duration, waveform, cue points, beat grid.

    Args:
        deck: The deck number, either 1 or 2.
    """
    return _dj_get(f"/api/deck/{deck}/track_info")


# ===================================================================
# BPM / RATE CONTROL
# ===================================================================

def set_rate(deck: int, rate: float = 0.0) -> str:
    """Set the playback rate/pitch of a deck. Use to change BPM.

    rate=0.0 means original BPM (reset to file's native tempo).
    Positive = faster, negative = slower. Range roughly -0.5 to 0.5.

    To reset to original BPM: set_rate(deck, 0.0)
    To speed up by 3%: set_rate(deck, 0.03)

    Args:
        deck: Deck number (1 or 2).
        rate: Rate adjustment. 0.0 = original BPM.
    """
    # Also disable sync so rate change sticks
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate", "value": rate})

    # Read back actual BPM
    status = _mixxx_get("/api/status")
    if status and not _mixxx_failed(status):
        bpm = status.get(f"deck{deck}", {}).get("bpm", 0)
        file_bpm = status.get(f"deck{deck}", {}).get("file_bpm", 0)
        return f"Deck {deck}: rate={rate}, BPM now {bpm:.1f} (file: {file_bpm:.0f})"
    return f"Deck {deck}: rate set to {rate}"


def reset_bpm(deck: int) -> str:
    """Reset a deck to its original BPM -- undoes any sync or rate changes.

    Args:
        deck: Deck number (1 or 2).
    """
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate_set_default", "value": 1})

    status = _mixxx_get("/api/status")
    if status and not _mixxx_failed(status):
        bpm = status.get(f"deck{deck}", {}).get("bpm", 0)
        file_bpm = status.get(f"deck{deck}", {}).get("file_bpm", 0)
        return f"Deck {deck}: BPM reset to original {file_bpm:.0f} (was {bpm:.1f})"
    return f"Deck {deck}: BPM reset to original"


# ===================================================================
# BEAT ALIGNMENT -- Phase matching like a human DJ
# ===================================================================

def align_beats(deck: int) -> str:
    """Align the beats of a deck to match the other playing deck.
    This is like a human DJ nudging the jog wheel to get kicks landing together.
    Call this AFTER loading and syncing a track, BEFORE or DURING a transition.

    Args:
        deck: The deck to align (1 or 2). Its beats will snap to the other deck's grid.
    """
    # beatsync_phase = align phase without changing BPM
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "beatsync_phase", "value": 1})
    # Also enable quantize so future actions stay on-grid
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "quantize", "value": 1})
    return f"Beats aligned on Deck {deck} -- phase matched and quantize enabled"


def nudge_track(deck: int, direction: str = "forward", strength: float = 0.5) -> str:
    """Nudge a track forward or backward slightly -- like touching the jog wheel.
    Use this to fine-tune beat alignment during a mix.

    Args:
        deck: Deck to nudge (1 or 2).
        direction: 'forward' to speed up momentarily, 'backward' to slow down.
        strength: Nudge strength 0.0 to 1.0 (0.5 = gentle, 1.0 = strong push).
    """
    import time as _time
    value = strength if direction == "forward" else -strength
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "wheel", "value": value})
    _time.sleep(0.1)
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "wheel", "value": 0})
    return f"Nudged Deck {deck} {direction} (strength {strength})"
