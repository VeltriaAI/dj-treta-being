"""Transition techniques -- all 5 styles + scheduler."""

import json
import logging
from pathlib import Path

from .helpers import _mixxx_failed, _mixxx_get, _mixxx_post
from ..runtime_paths import runtime_path

log = logging.getLogger("dj-treta")


def _tempo_ride(deck: int, target_bpm: float = None, duration_s: float = 60.0):
    """Tempo ride — gradually adjust BPM like a real DJ.

    Professional DJs adjust ~1 BPM per 30 seconds. The audience should never
    perceive a tempo change. This replaces the old 10s glide which was too
    fast and audible.

    target_bpm=None  → ride back to native file BPM
    target_bpm=128.0 → ride to a specific BPM
    duration_s=60    → take 60 seconds (default, ~1 BPM per 30s for typical gaps)
    """
    import time as _time

    status = _mixxx_get("/api/status")
    if not status:
        return
    d = status.get(f"deck{deck}", {})
    current_bpm = float(d.get("bpm", 0) or 0)
    file_bpm = float(d.get("file_bpm", 0) or 0)

    if not file_bpm or not current_bpm:
        _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})
        return

    goal_bpm = target_bpm if target_bpm is not None else file_bpm
    bpm_gap = abs(current_bpm - goal_bpm)

    if bpm_gap < 0.5:
        _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})
        return

    # Enable key lock to prevent pitch shift during tempo change
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "keylock", "value": 1})

    # Scale duration based on gap: ~30s per BPM difference, min 30s, max 120s
    auto_duration = max(30, min(120, bpm_gap * 30))
    duration_s = max(duration_s, auto_duration)

    # Apply in small increments (~0.5 BPM per step) with irregular timing
    # Real DJs don't use smooth linear ramps — the ear detects those
    import random

    steps = max(10, int(bpm_gap * 4))  # ~4 steps per BPM
    base_sleep = duration_s / steps

    for i in range(1, steps + 1):
        t = i / steps
        bpm_now = current_bpm + (goal_bpm - current_bpm) * t
        ratio = bpm_now / file_bpm if file_bpm else 1.0
        _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate_ratio", "value": ratio})
        # Irregular timing — harder for ear to detect pattern
        jitter = random.uniform(0.7, 1.3)
        _time.sleep(base_sleep * jitter)

    # Final: snap to exact target
    final_ratio = goal_bpm / file_bpm if file_bpm else 1.0
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate_ratio", "value": final_ratio})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})

    # Disable key lock once at native BPM (saves CPU, sounds cleaner)
    if target_bpm is None or abs(goal_bpm - file_bpm) < 0.5:
        _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "keylock", "value": 0})

    log.info(f"Tempo ride complete: deck {deck}, {current_bpm:.0f} → {goal_bpm:.0f} BPM over {duration_s:.0f}s")


def _apply_bpm_after(deck: int, bpm_after: str = "keep", glide_duration: int = 60):
    """Post-transition BPM handling.

    Default is "keep" — just disable sync, leave rate where it is.
    BPM is a creative choice for the whole set, not something to reset per-track.
    The DJ agent controls energy/BPM through track selection.

    bpm_after="keep"   → disable sync, leave rate untouched (default, correct)
    bpm_after="reset"  → tempo ride back to native file BPM (rarely needed)
    bpm_after="126.5"  → tempo ride to a specific BPM (agent decision)
    """
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})

    if bpm_after == "keep":
        return
    elif bpm_after == "reset":
        _tempo_ride(deck, target_bpm=None, duration_s=max(30, min(120, glide_duration)))
    else:
        try:
            target = float(bpm_after)
            _tempo_ride(deck, target_bpm=target, duration_s=max(30, min(120, glide_duration)))
        except (ValueError, TypeError):
            pass


def do_transition(to_deck: int, duration: int = 60, bpm_after: str = "keep", glide_duration: int = 60) -> str:
    """Execute a smooth crossfade transition to a deck.
    Uses Mixxx's C++ engine (20fps S-curve). After transition completes,
    the outgoing deck is paused and EQ/volume reset.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (10-120).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
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
    _mixxx_post("/api/volume", {"deck": out_deck, "level":1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "level":1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})

    _apply_bpm_after(to_deck, bpm_after, glide_duration)

    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Transitioned to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_bass_swap(to_deck: int, duration: int = 60, bpm_after: str = "keep", glide_duration: int = 60) -> str:
    """Execute a bass-swap transition (techno style).
    Phase 1: Bring incoming with bass cut. Phase 2: Swap bass. Phase 3: Fade out old.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Total transition duration in seconds (20-120).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
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
    _mixxx_post("/api/volume", {"deck": to_deck, "level":0.0})
    _mixxx_post("/api/play", {"deck": to_deck})

    # Phase 1 (0-40%): Bring in incoming volume (bass still cut)
    # Phase 2 (40-60%): Swap bass -- cut outgoing bass, restore incoming bass
    # Phase 3 (60-100%): Fade out outgoing volume
    for i in range(total + 1):
        t = i / total
        if t <= 0.4:
            blend = t / 0.4
            _mixxx_post("/api/volume", {"deck": to_deck, "level":round(blend, 2)})
        elif t <= 0.6:
            swap_t = (t - 0.4) / 0.2
            _mixxx_post("/api/eq", {"deck": out_deck, "lo": round(1.0 - swap_t, 2)})
            _mixxx_post("/api/eq", {"deck": to_deck, "lo": round(swap_t, 2)})
        else:
            fade = 1.0 - ((t - 0.6) / 0.4)
            _mixxx_post("/api/volume", {"deck": out_deck, "level":round(fade, 2)})
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
    _mixxx_post("/api/volume", {"deck": out_deck, "level":1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "level":1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})

    _apply_bpm_after(to_deck, bpm_after, glide_duration)

    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Bass-swapped to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_filter_sweep(to_deck: int, duration: int = 45, bpm_after: str = "keep", glide_duration: int = 60) -> str:
    """Filter sweep transition -- gradually reveal incoming track through a low-pass filter.
    Best for: progressive, melodic, atmospheric tracks.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (20-90).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
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
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Filter-swept to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_hard_cut(to_deck: int, bpm_after: str = "keep", glide_duration: int = 60) -> str:
    """Hard cut -- instant switch to the other deck. No blend, no crossfade.
    Best for: genre changes, drop moments, high energy transitions.

    Args:
        to_deck: Deck to switch TO (1 or 2).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
    """
    out_deck = 1 if to_deck == 2 else 2

    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"

    _mixxx_post("/api/play", {"deck": to_deck})
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Hard-cut to Deck {to_deck} (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_echo_out(to_deck: int, duration: int = 30, bpm_after: str = "keep", glide_duration: int = 60) -> str:
    """Echo out -- fade outgoing track with delay/echo tail, then drop incoming.
    Best for: energy shifts, mood changes, dramatic moments.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: How long the echo fade takes in seconds (10-45).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
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
    _mixxx_post("/api/volume", {"deck": to_deck, "level":0.0})
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
        _mixxx_post("/api/volume", {"deck": out_deck, "level":1.0 - t})

        _time.sleep(1 / fps)

    # Outgoing silent -- bring in incoming clean
    _mixxx_post("/api/pause", {"deck": out_deck})
    # Quick volume rise on incoming (0.5s clean drop-in)
    for s in range(5):
        _mixxx_post("/api/volume", {"deck": to_deck, "level":round((s + 1) / 5, 2)})
        _time.sleep(0.1)

    # Glide crossfader to final position
    xf_target = 0.0 if to_deck == 1 else 1.0
    for s in range(10):
        xf = 0.5 + (xf_target - 0.5) * ((s + 1) / 10)
        _mixxx_post("/api/crossfade", {"position": round(xf, 2)})
        _time.sleep(0.1)

    _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Echo-out to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def schedule_transition(to_deck: int, at_position: int, technique: str = "crossfade", duration: int = 45, bpm_after: str = "keep", glide_duration: int = 60, at_section_marker: str = "") -> str:
    """Schedule a transition at a specific track position. Returns immediately --
    Python executes the transition in the background when the track reaches at_position.

    Call this when you've decided the right moment to transition based on
    the track timeline (e.g., at a breakdown or outro).

    Args:
        to_deck: Deck to transition TO (1 or 2).
        at_position: Track position in seconds to START the transition.
            Server clamps to [current_position+5, duration-5-transition_duration]
            so a hallucinated or stale value can't fire past-end / in-the-past.
        technique: "crossfade" (smooth blend), "bass_swap" (EQ swap, techno), "filter_sweep" (progressive reveal), "echo_out" (fade with echo, mood shift), "hard_cut" (instant switch, genre change).
        duration: Transition duration in seconds (10-90). Ignored for hard_cut.
        bpm_after: What to do with BPM after the transition completes. "keep" = leave synced BPM (default, best when ±5 BPM), "reset" = glide back to native file BPM, or a number like "126.0" = glide to that specific BPM.
        glide_duration: Seconds for the BPM change when bpm_after is "reset" or a target BPM (5-60, default 10). Ignored when bpm_after="keep".
        at_section_marker: Optional symbolic position (overrides at_position
            when set). One of:
            - "mix_out" / "outro_start" — start at active.mix_out_seconds
              from the analyzer (the canonical outro entry)
            - "next_breakdown" / "next_outro" — first matching section in
              the active timeline at or after current position
            Resolves to seconds via the active deck's track metadata. If the
            metadata is missing the marker, falls back to at_position.
    """
    duration = max(10, min(120, duration))

    # Don't schedule if one is already pending
    # Check both the schedule file AND the lock file (lock survives P3 deletion)
    lock_file = runtime_path("transition-pending.lock")
    sched_file = runtime_path("scheduled-transition.json")
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

    # Resolve at_section_marker to seconds via DB metadata if provided.
    # Markers take priority over at_position so DJ can express intent
    # symbolically and let the server pick the right second.
    marker_used = ""
    if at_section_marker:
        try:
            from ..db import get_track_by_path
            tinfo = _mixxx_get(f"/api/deck/{active_deck}/track_info") or {}
            file_path = tinfo.get("file_path", "")
            meta = get_track_by_path(file_path) if file_path else None
            resolved: float | None = None
            marker = at_section_marker.lower().strip()
            if meta and marker in ("mix_out", "outro_start"):
                v = meta.get("mix_out_seconds")
                if v is not None:
                    resolved = float(v)
                    marker_used = "mix_out"
            elif meta and marker in ("next_outro", "next_breakdown"):
                target_section = "outro" if marker == "next_outro" else "breakdown"
                tl_raw = meta.get("timeline") or ""
                if tl_raw:
                    sections = json.loads(tl_raw) if isinstance(tl_raw, str) else tl_raw
                    for s in sections:
                        try:
                            start = float(s.get("start", 0))
                        except Exception:
                            continue
                        if (s.get("section") == target_section
                                and start >= current_pos):
                            resolved = start
                            marker_used = marker
                            break
            if resolved is not None:
                at_position = int(resolved)
        except Exception:
            # Marker resolution is advisory — silent fall-back to at_position.
            pass

    # Safety clamp — both bounds. Hallucinated or stale at_positions
    # could fire past-end (the 419-on-a-398s-track bug from 2026-04-30)
    # or in-the-past (transition immediately, no usable lead time).
    # Floor: current_pos + 5  (need a moment to set up the executor)
    # Ceil:  duration - duration_arg - 5  (transition must finish before
    #        the track ends with a 5s safety margin)
    if track_duration > 0:
        max_start = track_duration - duration - 5
        min_start = current_pos + 5
        clamped = False
        if at_position > max_start:
            at_position = max(min_start, max_start)
            clamped = True
        elif at_position < min_start:
            at_position = min_start
            clamped = True
        if clamped:
            log = __import__("logging").getLogger("dj-treta")
            log.warning(
                f"schedule_transition: clamped at_position to {at_position} "
                f"(orig pick out of [{min_start:.0f}, {max_start:.0f}] window)"
            )

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
        "bpmAfter": str(bpm_after),
        "glideDuration": max(5, min(60, glide_duration)),
    }
    runtime_path("scheduled-transition.json").write_text(
        json.dumps(scheduled, indent=2)
    )
    # Lock file survives P3 deletion of schedule file — prevents duplicate scheduling
    runtime_path("transition-pending.lock").write_text(str(at_position))

    return (
        f"Scheduled {technique} to deck {to_deck} at position {at_position}s "
        f"(in {round(delay)}s). Python will execute it -- you're free now."
    )
