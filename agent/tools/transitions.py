"""Transition techniques -- all 5 styles + scheduler."""

import json
import logging
import threading
import time as _time_mod
from pathlib import Path

from .helpers import _mixxx_failed, _mixxx_get, _mixxx_post
from ..runtime_paths import runtime_path

log = logging.getLogger("dj-treta")


def _wait_phrase_boundary(deck: int, timeout_s: float = 2.0, threshold: float = 0.05) -> bool:
    """Poll [Channel{deck}] beat_distance until near 0 (downbeat) or timeout.

    Returns True if a boundary was found within the timeout, False if it timed out.
    Mixxx exposes `beat_distance` on a channel as a 0..1 value relative to the
    nearest beat — near 0 (or near 1) means we're on a beat. We use only
    `< threshold` (post-beat) since pre-beat (close to 1.0) would mean firing
    slightly early.

    NOTE: TODO live-validate — `beat_distance` is the documented Mixxx 2.4
    control name; if the build differs the GET will silently fail and we'll
    hit the timeout, which is the safe behavior.
    """
    deadline = _time_mod.monotonic() + timeout_s
    group = f"[Channel{deck}]"
    while _time_mod.monotonic() < deadline:
        # Mixxx-fork HTTP API exposes generic control GET at /api/control?group=&key=
        resp = _mixxx_get(f"/api/control?group={group}&key=beat_distance")
        if not _mixxx_failed(resp):
            try:
                bd = float(resp.get("value", -1) if isinstance(resp, dict) else -1)
                if 0.0 <= bd < threshold or bd > (1.0 - threshold):
                    return True
            except (TypeError, ValueError):
                pass
        _time_mod.sleep(0.02)
    log.warning(f"phrase-boundary wait on deck {deck} timed out after {timeout_s}s")
    return False


def _bars_to_seconds(bars: int, bpm: float) -> float:
    """Convert N bars (assumes 4/4) at given BPM to seconds."""
    if not bpm or bpm <= 0:
        bpm = 120.0
    return bars * 4 * 60.0 / bpm


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


def do_transition(to_deck: int, duration: int = 60, bpm_after: str = "keep", glide_duration: int = 60, duration_bars: int = None) -> str:
    """Execute a smooth crossfade transition to a deck.
    Uses Mixxx's C++ engine (20fps S-curve). After transition completes,
    the outgoing deck is paused and EQ/volume reset.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (10-120).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
        duration_bars: Optional. If set, overrides `duration` by computing
            bars * 4 * 60 / outgoing_bpm. Lets the agent think in musical
            units instead of seconds.
    """
    import time as _time

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

    # Resolve duration_bars → seconds using outgoing-deck BPM.
    if duration_bars is not None and status:
        out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 0) or 0)
        if not out_bpm:
            out_bpm = float(status.get(f"deck{to_deck}", {}).get("bpm", 120.0) or 120.0)
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))

    duration = max(10, min(120, duration))

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

    # Phrase-boundary wait — start near a downbeat on outgoing deck so the
    # crossfade lines up musically. 2s timeout: if no boundary detected we
    # fire anyway (better to mix than to stall).
    _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)

    # Bass-bridge: dip incoming LO to 0 at start of fade, restore at ~70%
    # of duration. Prevents bass-collision against outgoing's still-present
    # low end. Restore happens in a background thread so we don't block the
    # main /api/transition call (which itself blocks for duration+2s).
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})
    bass_restore_at = duration * 0.70

    def _restore_incoming_bass():
        _time.sleep(bass_restore_at)
        _mixxx_post("/api/eq", {"deck": to_deck, "lo": 1.0})

    bass_thread = threading.Thread(target=_restore_incoming_bass, daemon=True)
    bass_thread.start()

    # Mixxx C++ S-curve transition (20fps, smooth)
    _mixxx_post("/api/transition", {"deck": to_deck, "duration": duration})
    _time.sleep(duration + 2)
    # Make sure bass-restore has run even if duration math drifted.
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 1.0})

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


def do_bass_swap(to_deck: int, duration: int = 60, bpm_after: str = "keep", glide_duration: int = 60, swap_style: str = "trade") -> str:
    """Execute a bass-swap transition.
    Phase 1: Bring incoming volume up with bass cut + slight MID dip.
    Phase 2: Quantized 1-bar bass swap on a downbeat (kill or trade).
    Phase 3: Fade out outgoing volume.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Total transition duration in seconds (20-120).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
        swap_style: "trade" (smooth twist over 1 bar — house, default) or
            "kill" (snap LO 1.0→0.0 in a single step on the downbeat — techno).
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

    # Outgoing BPM → 1-bar duration (used to size the quantized swap window)
    out_bpm = 120.0
    if status:
        try:
            out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 120.0) or 120.0)
        except Exception:
            out_bpm = 120.0
    if out_bpm <= 0:
        out_bpm = 120.0
    bar_duration_s = 60.0 / out_bpm * 4.0

    fps = 10
    total = int(duration * fps)
    swap_window_frames = max(1, int(bar_duration_s * fps))
    # Plan the swap to land near the end of the fade so phase-1 volume blend
    # has settled. Center swap so it ends ~10% before total so phase-3 fade
    # has room to breathe.
    swap_end_frame = max(swap_window_frames, int(total * 0.90))
    swap_start_frame = max(0, swap_end_frame - swap_window_frames)

    # Move crossfader to center so both decks are audible through it
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Sync + play incoming with bass killed and volume at 0
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})
    # Slight MID dip on incoming during phase 1 so it doesn't clash with
    # outgoing's still-present mids.
    _mixxx_post("/api/eq", {"deck": to_deck, "mid": 0.5})
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/play", {"deck": to_deck})

    # Wait for downbeat alignment before starting (small wait, won't stall fade).
    _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)

    swap_done = False
    mid_restored = False
    for i in range(total + 1):
        t = i / total

        # Phase 1: ramp incoming volume up to 1.0 by start of swap window
        if i < swap_start_frame:
            blend = i / max(1, swap_start_frame)
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(blend, 2)})

        # Phase 2: quantized 1-bar swap, aligned to a downbeat at the start
        elif i < swap_end_frame:
            if not swap_done:
                # Make sure we hit a downbeat before swap commits.
                _wait_phrase_boundary(out_deck, timeout_s=1.0, threshold=0.05)

                if swap_style == "kill":
                    # Techno-style snap on the downbeat: outgoing LO → 0,
                    # incoming LO → 1.0 in a single step.
                    _mixxx_post("/api/eq", {"deck": out_deck, "lo": 0.0})
                    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 1.0})
                    swap_done = True
                # "trade" style: smooth twist over the bar; we keep iterating
                # below in the elif branch but mark the start so we only set
                # frame 0's value once.
                swap_done = True

            if swap_style == "trade":
                swap_t = (i - swap_start_frame) / max(1, (swap_end_frame - swap_start_frame))
                _mixxx_post("/api/eq", {"deck": out_deck, "lo": round(1.0 - swap_t, 2)})
                _mixxx_post("/api/eq", {"deck": to_deck, "lo": round(swap_t, 2)})

        # Phase 3: fade out outgoing volume, restore incoming MID
        else:
            if not mid_restored:
                _mixxx_post("/api/eq", {"deck": to_deck, "mid": 1.0})
                mid_restored = True
            phase3_t = (i - swap_end_frame) / max(1, (total - swap_end_frame))
            fade = 1.0 - phase3_t
            _mixxx_post("/api/volume", {"deck": out_deck, "level": round(max(0.0, fade), 2)})

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


def do_filter_sweep(to_deck: int, duration: int = 45, bpm_after: str = "keep", glide_duration: int = 60, duration_bars: int = None) -> str:
    """Filter sweep transition -- outgoing rises into HP shimmer while incoming opens from LP.
    Best for: progressive, melodic, atmospheric tracks.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (20-90).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
        duration_bars: Optional. Override `duration` from bars at outgoing BPM.
    """
    import time as _time

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

    # Resolve duration_bars → seconds using outgoing-deck BPM.
    if duration_bars is not None and status:
        out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 0) or 0) or 120.0
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))

    duration = max(20, min(90, duration))

    # Start incoming with filter closed (muffled = LP fully down), sync handles BPM
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.0})  # fully closed (LP)
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)

    # Outgoing resonance group — used in the last 25% to build a "scream"
    # before the handoff. TODO live-validate: parameter2 on the QuickEffect's
    # Effect1 is documented as the resonance/Q on the standard Mixxx filter
    # in 2.4 builds — if the build differs the writes silently no-op.
    out_q_group = f"[QuickEffectRack1_[Channel{out_deck}]_Effect1]"

    # Gradually OPEN incoming LP filter (0 → 0.5 neutral)
    # Gradually OPEN outgoing HP filter (0.5 neutral → 1.0 fully high-passed)
    # — flipped from the old 0.5 → 0.0 closing-LP which produced a muddy
    #   boomy mess. HP-up is what pros actually do (thins bass, leaves shimmer).
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total  # 0.0 -> 1.0

        # Incoming: LP opening up 0.0 → 0.5 (neutral)
        _mixxx_post("/api/filter", {"deck": to_deck, "value": round(t * 0.5, 3)})

        # Outgoing: HP opening up 0.5 → 1.0 (thins bass, leaves treble)
        _mixxx_post("/api/filter", {"deck": out_deck, "value": round(0.5 + 0.5 * t, 3)})

        # Resonance ramp on outgoing during the last 25% (0 → 0.6)
        if t >= 0.75:
            q_t = (t - 0.75) / 0.25  # 0 → 1
            q_val = round(0.6 * q_t, 3)
            # TODO live-validate exact key for resonance on QuickEffectRack1
            # (parameter2 in 2.4 docs — silent no-op if wrong).
            _mixxx_post("/api/control", {
                "group": out_q_group,
                "key": "parameter2",
                "value": q_val,
            })

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
    # Reset resonance to its neutral default. TODO live-validate the neutral
    # value for parameter2 on the Mixxx filter (0 is the safe assumption).
    _mixxx_post("/api/control", {"group": out_q_group, "key": "parameter2", "value": 0.0})
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Filter-swept to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_hard_cut(to_deck: int, bpm_after: str = "keep", glide_duration: int = 60, align: str = "downbeat") -> str:
    """Hard cut -- instant switch to the other deck. No blend, no crossfade.
    Best for: genre changes, drop moments, high energy transitions.

    Args:
        to_deck: Deck to switch TO (1 or 2).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
        align: When to fire the cut.
            - "downbeat" (default): wait up to 2s for outgoing beat_distance
              near 0 before cutting.
            - "now": fire immediately, no wait.
            - "phrase": count 16 beats on the outgoing deck, then cut on the
              next downbeat. Uses beat_active edge transitions.
    """
    import time as _time

    out_deck = 1 if to_deck == 2 else 2

    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"

    # Sync BPMs first so the post-cut feel is musical, even on hard cut.
    _mixxx_post("/api/sync", {"deck": to_deck})
    # Quantize on incoming so play landings snap to the grid.
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "quantize", "value": 1})

    # Alignment: optional wait for downbeat / phrase boundary before firing.
    if align == "phrase":
        # Count 16 beats on outgoing via beat_active edges, then wait for
        # the next downbeat. TODO live-validate: `beat_active` is the 2.4
        # docs name for the per-beat pulse. If silent, we still time out
        # via the per-beat poll budget.
        beats_seen = 0
        last_active = -1
        deadline = _time_mod.monotonic() + 16.0  # outer safety: ~16 beats @ 60bpm
        while beats_seen < 16 and _time_mod.monotonic() < deadline:
            r = _mixxx_get(f"/api/control?group=[Channel{out_deck}]&key=beat_active")
            if not _mixxx_failed(r):
                try:
                    v = int(float(r.get("value", 0) if isinstance(r, dict) else 0))
                    if v == 1 and last_active == 0:
                        beats_seen += 1
                    last_active = v
                except (TypeError, ValueError):
                    pass
            _time.sleep(0.02)
        _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)
    elif align == "downbeat":
        _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)
    # align == "now" → no wait

    _mixxx_post("/api/play", {"deck": to_deck})
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Hard-cut to Deck {to_deck} (align={align}, bpm_after={bpm_after}). Deck {out_deck} ejected."


# ──────────────────────────────────────────────────────────────────────────
# Echo-effect helpers (Mixxx EffectRack1 / EffectUnit1)
#
# Mixxx control-object names verified against the Mixxx 2.4 controls manual
# (https://manual.mixxx.org/2.4/en/chapters/appendix/mixxx_controls.html).
# These keys are stable across 2.3 / 2.4 / 2.5. The effect-loading API (how
# you put "Echo" into Effect1 of Unit1) is the part that *does* vary by
# version — see TODO inside _echo_engage.
#
# Convention used here:
#   - Unit 1 of Rack 1 is reserved for echo-out transitions.
#   - We assume the user has Echo (or an equivalent delay) pre-loaded in
#     [EffectRack1_EffectUnit1_Effect1]. If not, transition still completes
#     but the FX won't audibly engage — a clear, validatable failure mode.
#   - We DON'T touch effect parameters (delay time / feedback) because they
#     differ per-effect and per-version. We drive the unit via `mix` (dry/wet)
#     and `super1` (super-knob) which the user has dialed in to taste.
# ──────────────────────────────────────────────────────────────────────────

def _echo_engage(deck: int) -> None:
    """Route `deck` through EffectRack1/EffectUnit1 with mix=0 (silent insert).

    Caller then ramps `mix` 0 → ~0.8 over the fade. We DO NOT load the effect
    here — see TODO. We assume Echo is already in slot 1.
    """
    unit = "[EffectRack1_EffectUnit1]"

    # Start dry — we'll ramp mix up during phase A.
    _mixxx_post("/api/control", {"group": unit, "key": "mix", "value": 0.0})

    # Enable the unit itself (some Mixxx builds default it off).
    _mixxx_post("/api/control", {"group": unit, "key": "enabled", "value": 1})

    # Route the deck channel through this unit.
    _mixxx_post("/api/control", {
        "group": unit,
        "key": f"group_[Channel{deck}]_enable",
        "value": 1,
    })

    # Ensure Effect1 in the chain is enabled.
    _mixxx_post("/api/control", {
        "group": "[EffectRack1_EffectUnit1_Effect1]",
        "key": "enabled",
        "value": 1,
    })

    # TODO (live-validate): if the user hasn't pre-loaded Echo into
    # [EffectRack1_EffectUnit1_Effect1], we'd need to load it here. The
    # canonical 2.4 way is to write the effect's identifier into a chain
    # control, but the exact key (`loaded` / `effect_selector` / chain
    # `next_effect`) varies. Current plan: rely on the Mixxx session
    # having Echo pre-loaded in slot 1; if that's wrong this fade will
    # be silent-FX (still ducks volume, just no tail) — easy to spot live.


def _echo_disengage(deck: int) -> None:
    """Tear down the echo routing for `deck`. Idempotent."""
    unit = "[EffectRack1_EffectUnit1]"

    _mixxx_post("/api/control", {"group": unit, "key": "mix", "value": 0.0})
    _mixxx_post("/api/control", {
        "group": unit,
        "key": f"group_[Channel{deck}]_enable",
        "value": 0,
    })


def do_echo_out(to_deck: int, duration: int = 32, bpm_after: str = "keep", glide_duration: int = 60, freeze: bool = False, wait_for_incoming_drop: bool = True) -> str:
    """Echo out -- a discrete moment, not a smooth ramp. Outgoing rides at
    full level; in the last bar of Phase A the echo wet/dry snaps up to 0.7
    in 4 quick steps; at Phase A→B we HARD-CUT the outgoing fader (volume 0
    + LO-EQ kill) so the kick goes silent but the FX bus keeps ringing from
    the echo unit's internal buffer; incoming is then brought up under the
    tail.

    Best for: energy shifts, key/BPM gaps, mood changes, dramatic moments.

    Phase shape (over `duration` seconds, freeze=False):
      A  0%-50%  : outgoing volume held at 1.0. First ~75% of A is dry; in
                   the last ~25% wet snaps 0 → 0.7 in 4 discrete steps.
      A→B boundary: HARD CUT on outgoing (volume 1.0 → 0.0 + LO-EQ → 0.0).
                    Echo bus keeps ringing.
      B  50%-90% : bring incoming volume 0 → 1.0 under the echo tail.
      C  90%-100%: incoming pinned at 1.0.
      Tail        : 8 beats at outgoing BPM, clamped 2-8s. Wet ramps
                   0.7 → 0 linearly. Then disengage FX + eject outgoing.

    Echo Freeze variant (freeze=True):
      Snaps wet to 1.0, pauses outgoing immediately, holds the wet=1.0 echo
      loop ringing. Skips Phase B / tail / ejection — caller is expected to
      bring the incoming track in via a separate call.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: How long the echo fade takes in seconds (10-64). Default 32
            is ~16 bars at 120 BPM, which is the canonical pro echo-out.
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
        freeze: If True, engage Echo Freeze (wet=1.0, pause outgoing, hold
            the echo loop) and return early without touching the incoming
            deck.
    """
    import time as _time

    duration = max(10, min(64, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight ABORT (mirrors do_transition's discipline).
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        remaining_to = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining_to < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining_to:.0f}s left -- load a fresh track first."

    # Outgoing BPM (used for tail-ring beat math).
    out_bpm = 120.0
    if status:
        try:
            out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 120.0) or 120.0)
        except Exception:
            out_bpm = 120.0
    if out_bpm <= 0:
        out_bpm = 120.0

    # Center the crossfader so both decks share the master via the FX bus.
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Start incoming silently + synced.
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})

    # Engage echo on the outgoing deck.
    _echo_engage(out_deck)

    # ── Buildup-aware Phase-B gating (fix for echo_out energy-drop bug) ──
    # Look up incoming deck's intro_ends_at so we know when its drop hits.
    # If the hard-cut (Phase A→B) lands while incoming is still in its
    # intro/buildup, the listener hears [echo tail] → [quiet buildup] →
    # delayed drop = energy hole. Hold Phase A until incoming reaches its
    # drop, capped at +32s extra so we don't hang forever.
    incoming_intro_ends: float | None = None
    if wait_for_incoming_drop and not freeze:
        try:
            from ..db import get_track_by_path
            tinfo = _mixxx_get(f"/api/deck/{to_deck}/track_info") or {}
            in_path = tinfo.get("file_path", "")
            meta = get_track_by_path(in_path) if in_path else None
            if meta and meta.get("mix_in_seconds") is not None:
                incoming_intro_ends = float(meta["mix_in_seconds"])
            else:
                # Fallback heuristic: 32s ≈ 16 bars at 120bpm — conservative
                # floor for unknown melodic-techno intros (most run 32 bars).
                incoming_intro_ends = 32.0
        except Exception as _e:
            log.warning(f"[ECHO-OUT] intro_ends lookup failed: {_e}")
            incoming_intro_ends = 32.0

    # Echo Freeze: snap wet to 1.0, pause outgoing, hold the loop. No Phase
    # B, no tail decay, no ejection — caller handles the incoming track.
    if freeze:
        _mixxx_post("/api/control", {
            "group": "[EffectRack1_EffectUnit1]",
            "key": "mix",
            "value": 1.0,
        })
        _mixxx_post("/api/pause", {"deck": out_deck})
        return (
            f"Echo Freeze engaged on Deck {out_deck} (wet=1.0, paused). "
            f"Bring Deck {to_deck} in with a separate call."
        )

    fps = 10
    total = int(duration * fps)

    # Phase boundaries. Phase A holds outgoing at full level until the last
    # ~25% of A, where wet snaps up in 4 quick steps timed to the run-up of
    # the next "downbeat". At A→B we hard-cut outgoing; the FX bus rings
    # while the incoming track is brought up over Phase B.
    PHASE_A_END = 0.50
    PHASE_B_END = 0.90
    WET_SNAP_START = 0.75  # fraction inside Phase A where wet starts snapping
    WET_PEAK = 0.7          # echo wet/dry at peak (real echo-out, not full freeze)

    a_end_idx = int(total * PHASE_A_END)
    b_end_idx = int(total * PHASE_B_END)
    snap_start_idx = int(a_end_idx * WET_SNAP_START)
    # 4 discrete snap steps across the last 25% of Phase A.
    snap_total = max(1, a_end_idx - snap_start_idx)
    snap_step = max(1, snap_total // 4)
    last_wet_sent = -1.0
    hard_cut_done = False

    for i in range(total + 1):
        t = i / total if total else 1.0

        if i <= a_end_idx:
            # Phase A: outgoing volume held at 1.0. HPF/EQ untouched (this is
            # echo-out, not filter-fade — keep the kick punching until we
            # hard-cut at A→B).
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})

            if i < snap_start_idx:
                # First ~75% of Phase A: dry. Wet kept at 0.
                if last_wet_sent != 0.0:
                    _mixxx_post("/api/control", {
                        "group": "[EffectRack1_EffectUnit1]",
                        "key": "mix",
                        "value": 0.0,
                    })
                    last_wet_sent = 0.0
            else:
                # Last ~25%: 4 discrete snap steps up to WET_PEAK on the
                # run-up to the downbeat. (We don't have true downbeat
                # detection — we approximate "last bar before A→B" with
                # this 4-step snap.)
                step_idx = (i - snap_start_idx) // snap_step
                step_idx = min(step_idx, 3)
                wet = WET_PEAK * ((step_idx + 1) / 4.0)
                if abs(wet - last_wet_sent) > 1e-3:
                    _mixxx_post("/api/control", {
                        "group": "[EffectRack1_EffectUnit1]",
                        "key": "mix",
                        "value": round(wet, 3),
                    })
                    last_wet_sent = wet

        elif i <= b_end_idx:
            # Phase B opens with a HARD CUT on outgoing: volume 0 + LO-EQ kill
            # in a single step. The deck transport keeps playing into the FX
            # send so the echo unit's internal buffer continues to ring out.
            if not hard_cut_done:
                # ── Buildup-aware delay (do_echo_out fix) ──
                # If incoming is still in its intro/buildup, hold the echo
                # at peak with outgoing still at full level (Phase A shape)
                # until incoming pos >= intro_ends_at. Cap at 32s extra wait.
                if wait_for_incoming_drop and incoming_intro_ends is not None:
                    waited = 0.0
                    poll_interval = 0.5
                    max_wait = 32.0
                    in_pos = 0.0
                    in_status = _mixxx_get("/api/status") or {}
                    try:
                        in_pos = float(
                            in_status.get(f"deck{to_deck}", {})
                            .get("position_seconds", 0) or 0
                        )
                    except Exception:
                        in_pos = 0.0
                    if in_pos < incoming_intro_ends:
                        log.info(
                            f"[ECHO-OUT] holding Phase A — incoming pos "
                            f"{in_pos:.1f}s < intro_ends {incoming_intro_ends:.1f}s; "
                            f"max wait {max_wait:.0f}s"
                        )
                        # Pin echo at peak, outgoing still at 1.0, no cut yet.
                        _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
                        if abs(WET_PEAK - last_wet_sent) > 1e-3:
                            _mixxx_post("/api/control", {
                                "group": "[EffectRack1_EffectUnit1]",
                                "key": "mix",
                                "value": round(WET_PEAK, 3),
                            })
                            last_wet_sent = WET_PEAK
                        while waited < max_wait:
                            _time.sleep(poll_interval)
                            waited += poll_interval
                            in_status = _mixxx_get("/api/status") or {}
                            try:
                                in_pos = float(
                                    in_status.get(f"deck{to_deck}", {})
                                    .get("position_seconds", 0) or 0
                                )
                            except Exception:
                                in_pos = 0.0
                            if in_pos >= incoming_intro_ends:
                                log.info(
                                    f"[ECHO-OUT] incoming hit drop at "
                                    f"{in_pos:.1f}s after {waited:.1f}s wait — "
                                    f"firing hard-cut"
                                )
                                break
                        else:
                            log.warning(
                                f"[ECHO-OUT] still in buildup after {waited:.0f}s "
                                f"wait (pos {in_pos:.1f} < {incoming_intro_ends:.1f}); "
                                f"falling through to avoid hang"
                            )

                _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})
                _mixxx_post("/api/eq", {"deck": out_deck, "lo": 0.0})
                # Pin wet at peak so the tail rings cleanly under the blend.
                if abs(WET_PEAK - last_wet_sent) > 1e-3:
                    _mixxx_post("/api/control", {
                        "group": "[EffectRack1_EffectUnit1]",
                        "key": "mix",
                        "value": round(WET_PEAK, 3),
                    })
                    last_wet_sent = WET_PEAK
                hard_cut_done = True

            # Bring incoming up 0 → 1.0 across Phase B.
            b = (t - PHASE_A_END) / (PHASE_B_END - PHASE_A_END)
            in_vol = max(0.0, min(1.0, b))
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(in_vol, 3)})

        else:
            # Phase C: outgoing already silent; pin incoming at 1.0. Echo
            # tail keeps ringing from the FX bus.
            _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})

        _time.sleep(1.0 / fps)

    # Glide crossfader to final position (1s). Echo tail still ringing.
    xf_target = 0.0 if to_deck == 1 else 1.0
    steps = 10
    for s in range(steps + 1):
        xf = 0.5 + (xf_target - 0.5) * (s / steps)
        _mixxx_post("/api/crossfade", {"position": round(xf, 2)})
        _time.sleep(0.1)

    # Pause outgoing — its audio is already at 0 but the deck transport
    # was still playing into the FX send. Tail will continue from the
    # echo unit's internal buffer.
    _mixxx_post("/api/pause", {"deck": out_deck})

    # Let echo tail ring for ~8 beats at outgoing BPM, clamped 2-8s. During
    # the tail, ramp wet from WET_PEAK → 0 linearly so the echo decays
    # naturally instead of cutting.
    tail_seconds = (60.0 / out_bpm) * 8.0
    tail_seconds = max(2.0, min(8.0, tail_seconds))
    tail_steps = max(8, int(tail_seconds * fps))
    for s in range(tail_steps + 1):
        a = s / tail_steps
        wet = WET_PEAK * (1.0 - a)
        _mixxx_post("/api/control", {
            "group": "[EffectRack1_EffectUnit1]",
            "key": "mix",
            "value": round(wet, 3),
        })
        _time.sleep(tail_seconds / tail_steps)

    # Tear down FX + reset outgoing deck state.
    _echo_disengage(out_deck)
    _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})

    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Echo-out to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_riser(to_deck: int, duration: int = 32, bpm_after: str = "keep", glide_duration: int = 60, duration_bars: int = None) -> str:
    """Riser transition (inspired by djay's "Riser") — outgoing rides into a
    high-pass + resonance "zap" build while incoming swells underneath, then
    outgoing kills and incoming opens up.

    Phase A (0-60%): outgoing HP filter 0.5 → 0.95, vol 1.0 → 0.4.
                     incoming vol 0 → 0.5.
    Phase B (60-90%): outgoing HP pinned at 0.95, resonance 0 → 0.85 ("zap").
                      outgoing vol 0.4 → 0.1.
    Phase C (90-100%): outgoing kill (vol 0).
                       incoming vol 0.5 → 1.0. Reset outgoing filter+resonance.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Duration in seconds (10-64). Default 32 ≈ 16 bars at 120 BPM.
        bpm_after: "keep", "reset", or target BPM string.
        glide_duration: Seconds for BPM glide.
        duration_bars: Optional. Override `duration` from bars at outgoing BPM.
    """
    import time as _time

    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight ABORT (mirrors do_filter_sweep / do_transition).
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

    out_bpm = 120.0
    if status:
        try:
            out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 120.0) or 120.0)
        except Exception:
            out_bpm = 120.0
    if out_bpm <= 0:
        out_bpm = 120.0

    if duration_bars is not None:
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))
    duration = max(10, min(64, duration))

    out_q_group = f"[QuickEffectRack1_[Channel{out_deck}]_Effect1]"

    # Sync + start incoming silently.
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})

    # Center the crossfader so both decks pass through master.
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Wait for downbeat alignment.
    _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)

    fps = 10
    total = int(duration * fps)
    A_END = 0.60
    B_END = 0.90

    for i in range(total + 1):
        t = i / total if total else 1.0

        if t <= A_END:
            # Phase A: HP rises 0.5 → 0.95 on outgoing; vols swap.
            a = t / A_END
            out_filter = 0.5 + (0.95 - 0.5) * a
            out_vol = 1.0 + (0.4 - 1.0) * a  # 1.0 → 0.4
            in_vol = 0.0 + (0.5 - 0.0) * a   # 0 → 0.5
            _mixxx_post("/api/filter", {"deck": out_deck, "value": round(out_filter, 3)})
            _mixxx_post("/api/volume", {"deck": out_deck, "level": round(out_vol, 3)})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(in_vol, 3)})

        elif t <= B_END:
            # Phase B: outgoing HP pinned, resonance ramps 0 → 0.85, vol 0.4 → 0.1.
            b = (t - A_END) / (B_END - A_END)
            q_val = 0.0 + 0.85 * b
            out_vol = 0.4 + (0.1 - 0.4) * b
            _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.95})
            # TODO live-validate: parameter2 on QuickEffectRack1 = resonance.
            _mixxx_post("/api/control", {
                "group": out_q_group,
                "key": "parameter2",
                "value": round(q_val, 3),
            })
            _mixxx_post("/api/volume", {"deck": out_deck, "level": round(out_vol, 3)})

        else:
            # Phase C: outgoing kill, incoming opens up.
            c = (t - B_END) / max(1e-6, (1.0 - B_END))
            in_vol = 0.5 + (1.0 - 0.5) * c
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(in_vol, 3)})

        _time.sleep(1.0 / fps)

    # Cleanup — reset filter + resonance on outgoing, snap crossfader.
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/control", {"group": out_q_group, "key": "parameter2", "value": 0.0})
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})

    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Riser to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_dissolve(to_deck: int, duration: int = 16, bpm_after: str = "keep", glide_duration: int = 60, duration_bars: int = None) -> str:
    """Dissolve transition — short volume-only crossfade with neutral EQ.
    Faster than `do_transition` (60s default) but smoother than `do_hard_cut`.

    Default ~8 bars (16s at 120 BPM). No EQ or filter changes — just clean
    volume crossfade. Useful when both tracks already sit well together.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Duration in seconds (4-32). Default 16.
        bpm_after: "keep", "reset", or target BPM string.
        glide_duration: Seconds for BPM glide.
        duration_bars: Optional. Override `duration` from bars at outgoing BPM.
    """
    import time as _time

    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight ABORT.
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 20:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left -- load a fresh track first."

    if duration_bars is not None and status:
        out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 0) or 0) or 120.0
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))
    duration = max(4, min(32, duration))

    # Sync + start incoming at vol=0.
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})

    _mixxx_post("/api/crossfade", {"position": 0.5})
    _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)

    # Linear volume crossfade. No EQ/filter changes.
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total if total else 1.0
        _mixxx_post("/api/volume", {"deck": out_deck, "level": round(1.0 - t, 3)})
        _mixxx_post("/api/volume", {"deck": to_deck, "level": round(t, 3)})
        _time.sleep(1.0 / fps)

    # Cleanup.
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Dissolved to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


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
        technique: "crossfade" (smooth blend), "bass_swap" (EQ swap, techno), "filter_sweep" (progressive reveal), "echo_out" (fade with echo, mood shift), "hard_cut" (instant switch, genre change), "riser" (HP+resonance build then handoff), "dissolve" (short clean volume crossfade).
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

    # ── Echo-out duration floor (safety net for the 15s-default bug) ──
    # The DJ prompt mandates 32-64 bars (≥32s) for echo_out, but Flash has
    # been picking duration=15 (the schedule_transition default) anyway.
    # 15s = ~7.5 bars at 120bpm — way too short, listener hears the echo
    # tail land on the incoming buildup. Coerce up to 32s here so the
    # technique can't be silently misused.
    if technique == "echo_out" and duration < 32:
        old_duration = duration
        duration = 32
        log.warning(
            f"[ECHO-OUT-FLOOR] caller passed duration={old_duration}, "
            f"coerced to 32s (echo_out floor)"
        )

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
