"""Transition techniques -- all 5 styles + scheduler."""

import json
from pathlib import Path

from .helpers import _mixxx_failed, _mixxx_get, _mixxx_post


def do_transition(to_deck: int, duration: int = 60) -> str:
    """Execute a smooth crossfade transition to a deck.
    Uses Mixxx's C++ engine (20fps S-curve). After transition completes,
    the outgoing deck is paused and EQ/volume reset.

    The brain picks compatible tracks. This tool just executes the transition.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (10-120).
    """
    import time as _time

    duration = max(10, min(120, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight: ABORT if incoming deck has no playable track
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first with load_track."
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left -- load a fresh track first."

    # Sync + play + phase align (let Mixxx handle BPM matching naturally)
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "quantize", "value": 1})
    _time.sleep(0.1)

    # Verify it actually started playing
    status2 = _mixxx_get("/api/status")
    if err2 := _mixxx_failed(status2):
        return f"ABORTED: lost Mixxx during transition prep: {err2}"
    if status2:
        deck_state2 = status2.get(f"deck{to_deck}", {})
        if not deck_state2.get("playing", False):
            return f"ABORTED: Deck {to_deck} failed to start playing."

    # Mixxx C++ S-curve transition (20fps, smooth)
    _mixxx_post("/api/transition", {"deck": to_deck, "duration": duration})
    _time.sleep(duration + 2)

    # Post-flight cleanup -- crossfader + pause + reset EQ
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})

    # Reset rate on active deck — prevent BPM drift from sync
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "rate", "value": 0.0})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "sync_enabled", "value": 0})

    # Eject outgoing deck -- prevents "loaded but finished" state
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Transitioned to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


def do_bass_swap(to_deck: int, duration: int = 60) -> str:
    """Execute a bass-swap transition (techno style).
    Phase 1: Bring incoming with bass cut. Phase 2: Swap bass. Phase 3: Fade out old.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Total transition duration in seconds (20-120).
    """
    import time as _time

    duration = max(20, min(120, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight: ABORT if incoming deck has no playable track
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left -- load a fresh track first."

    fps = 10
    total = int(duration * fps)

    # Move crossfader to center so both decks are audible through it
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Sync + play incoming with bass killed and volume at 0
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 0.0})
    _mixxx_post("/api/play", {"deck": to_deck})

    # Phase 1 (0-40%): Bring in incoming volume (bass still cut)
    # Phase 2 (40-60%): Swap bass -- cut outgoing bass, restore incoming bass
    # Phase 3 (60-100%): Fade out outgoing volume
    for i in range(total + 1):
        t = i / total
        if t <= 0.4:
            blend = t / 0.4
            _mixxx_post("/api/volume", {"deck": to_deck, "volume": round(blend, 2)})
        elif t <= 0.6:
            swap_t = (t - 0.4) / 0.2
            _mixxx_post("/api/eq", {"deck": out_deck, "lo": round(1.0 - swap_t, 2)})
            _mixxx_post("/api/eq", {"deck": to_deck, "lo": round(swap_t, 2)})
        else:
            fade = 1.0 - ((t - 0.6) / 0.4)
            _mixxx_post("/api/volume", {"deck": out_deck, "volume": round(fade, 2)})
        _time.sleep(1.0 / fps)

    # Cleanup -- smooth crossfader to final position, pause + reset
    xf_target = 0.0 if to_deck == 1 else 1.0
    # Glide crossfader over 1s instead of snapping
    steps = 10
    xf_start = 0.5
    for s in range(steps + 1):
        xf = xf_start + (xf_target - xf_start) * (s / steps)
        _mixxx_post("/api/crossfade", {"position": round(xf, 2)})
        _time.sleep(0.1)

    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})

    # Reset rate on active deck — prevent BPM drift
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "rate", "value": 0.0})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "sync_enabled", "value": 0})

    # Eject outgoing deck
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Bass-swapped to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


def do_filter_sweep(to_deck: int, duration: int = 45) -> str:
    """Filter sweep transition -- gradually reveal incoming track through a low-pass filter.
    Best for: progressive, melodic, atmospheric tracks.

    The incoming track starts muffled (low-pass filtered), then the filter opens
    while the outgoing track fades. Creates a smooth, evolving reveal.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (20-90).
    """
    import time as _time

    duration = max(20, min(90, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight
    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left."

    # Start incoming with filter closed (muffled), sync handles BPM
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.0})  # fully closed
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)

    # Gradually open filter on incoming + fade out outgoing
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total  # 0.0 -> 1.0

        # Open incoming filter: 0.0 -> 0.5 (neutral)
        _mixxx_post("/api/filter", {"deck": to_deck, "value": t * 0.5})

        # Close outgoing filter: 0.5 -> 0.0
        _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5 * (1 - t)})

        # Crossfader follows
        xf = t if to_deck == 2 else (1 - t)
        _mixxx_post("/api/crossfade", {"position": xf})

        _time.sleep(1 / fps)

    # Cleanup
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "rate", "value": 0.0})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Filter-swept to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


def do_hard_cut(to_deck: int) -> str:
    """Hard cut -- instant switch to the other deck. No blend, no crossfade.
    Best for: genre changes, drop moments, high energy transitions.

    Args:
        to_deck: Deck to switch TO (1 or 2).
    """
    out_deck = 1 if to_deck == 2 else 2

    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"

    # Instant switch
    _mixxx_post("/api/play", {"deck": to_deck})
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "rate", "value": 0.0})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Hard-cut to Deck {to_deck}. Deck {out_deck} ejected."


def do_echo_out(to_deck: int, duration: int = 30) -> str:
    """Echo out -- fade outgoing track with delay/echo tail, then drop incoming.
    Best for: energy shifts, mood changes, dramatic moments.

    The outgoing track fades with its echo reverberating, creating space,
    then the incoming track drops in clean.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: How long the echo fade takes in seconds (10-45).
    """
    import time as _time

    duration = max(10, min(45, duration))
    out_deck = 1 if to_deck == 2 else 2

    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"

    # Move crossfader to center so both decks are audible
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Start incoming silently -- it will be revealed after outgoing fades
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})

    # Fade out outgoing with filter closing (simulates echo decay)
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total

        # Close filter on outgoing (muffled decay)
        _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5 * (1 - t)})

        # Fade volume on outgoing
        _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0 - t})

        _time.sleep(1 / fps)

    # Outgoing silent -- bring in incoming clean
    _mixxx_post("/api/pause", {"deck": out_deck})
    # Quick volume rise on incoming (0.5s clean drop-in)
    for s in range(5):
        _mixxx_post("/api/volume", {"deck": to_deck, "volume": round((s + 1) / 5, 2)})
        _time.sleep(0.1)

    # Glide crossfader to final position
    xf_target = 0.0 if to_deck == 1 else 1.0
    for s in range(10):
        xf = 0.5 + (xf_target - 0.5) * ((s + 1) / 10)
        _mixxx_post("/api/crossfade", {"position": round(xf, 2)})
        _time.sleep(0.1)

    # Reset
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "rate", "value": 0.0})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Echo-out to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


def schedule_transition(to_deck: int, at_position: int, technique: str = "crossfade", duration: int = 45) -> str:
    """Schedule a transition at a specific track position. Returns immediately --
    Python executes the transition in the background when the track reaches at_position.

    Call this when you've decided the right moment to transition based on
    the track timeline (e.g., at a breakdown or outro).

    Args:
        to_deck: Deck to transition TO (1 or 2).
        at_position: Track position in seconds to START the transition.
        technique: "crossfade" (smooth blend), "bass_swap" (EQ swap, techno), "filter_sweep" (progressive reveal), "echo_out" (fade with echo, mood shift), "hard_cut" (instant switch, genre change).
        duration: Transition duration in seconds (10-90). Ignored for hard_cut.
    """
    duration = max(10, min(120, duration))

    # Don't schedule if one is already pending
    # Check both the schedule file AND the lock file (lock survives P3 deletion)
    lock_file = Path("/tmp/dj-treta-transition-pending.lock")
    sched_file = Path("/tmp/dj-treta-scheduled-transition.json")
    if lock_file.exists() or sched_file.exists():
        try:
            existing = json.loads(sched_file.read_text()) if sched_file.exists() else {}
            return (
                f"Transition already scheduled: {existing.get('technique', 'crossfade')} "
                f"to deck {existing.get('toDeck')} at {existing.get('atPosition')}s. "
                f"Wait for it to execute."
            )
        except Exception:
            pass

    # Get current track position
    status = _mixxx_get("/api/status")
    if not status or _mixxx_failed(status):
        return "ERROR: Mixxx not responding"

    active_deck = 1 if to_deck == 2 else 2
    d_active = status.get(f"deck{active_deck}", {})
    current_pos = float(d_active.get("position_seconds", 0) or 0)
    track_duration = float(d_active.get("duration", 0) or 0)

    # Safety: clamp at_position so transition completes before track ends
    if track_duration > 0:
        max_start = track_duration - duration - 5  # 5s safety margin
        if at_position > max_start:
            at_position = max(current_pos + 5, max_start)

    delay = max(0, at_position - current_pos)

    # If track is almost over, transition immediately
    remaining = float(d_active.get("remaining_seconds", 0) or 0)
    if remaining < duration + 10:
        at_position = current_pos + 2  # start in 2 seconds
        delay = 2

    # Write schedule -- Python (_execute_scheduled_transition in main.py) picks this up
    scheduled = {
        "toDeck": to_deck,
        "atPosition": at_position,
        "technique": technique,
        "duration": duration,
        "activeDeck": active_deck,
        "scheduledAt": current_pos,
        "executesIn": round(delay),
    }
    Path("/tmp/dj-treta-scheduled-transition.json").write_text(
        json.dumps(scheduled, indent=2)
    )
    # Lock file survives P3 deletion of schedule file — prevents duplicate scheduling
    Path("/tmp/dj-treta-transition-pending.lock").write_text(str(at_position))

    return (
        f"Scheduled {technique} to deck {to_deck} at position {at_position}s "
        f"(in {round(delay)}s). Python will execute it -- you're free now."
    )
