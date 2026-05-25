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


# ──────────────────────────────────────────────────────────────────────────
# E1 — Master Clock & Bar-Quantized Transitions
#
# The outgoing deck is the master clock for a transition: every blend starts on
# its next DOWNBEAT (bar boundary), not at a raw position in seconds. We read
# beat phase from Mixxx control objects on [ChannelN]:
#   - `beat_distance` : 0..1 fraction of the way through the CURRENT beat
#                       (0.0 == on the beat). Documented, stable 2.3/2.4/2.5.
#   - `beat_active`   : 1 on the beat sample, 0 otherwise — a per-beat pulse we
#                       edge-count to find bar (every-4th-beat) boundaries.
#   - `bpm`           : current (rate-adjusted) tempo, from /api/status.
# We DON'T assume the build exposes an absolute beat index, so bars are tracked
# by counting beat_active rising edges modulo `beats_per_bar`.
#
# Pure math is split into helpers (`_beats_to_next_bar`, `_seconds_until_bar`)
# so the bar-boundary logic is unit-testable without a live Mixxx.
# ──────────────────────────────────────────────────────────────────────────

_BEATS_PER_BAR = 4  # 4/4 assumed throughout (matches _bars_to_seconds)


def _beats_to_next_bar(beats_into_bar: int, beat_distance: float,
                       beats_per_bar: int = _BEATS_PER_BAR) -> float:
    """Pure: fractional beats remaining until the next bar downbeat.

    `beats_into_bar` is how many whole beats we are past the last downbeat
    (0..beats_per_bar-1). `beat_distance` is 0..1 progress through the current
    beat. Returns beats remaining to the NEXT downbeat — always in
    (0, beats_per_bar]. When we are exactly on a downbeat (beats_into_bar==0,
    beat_distance≈0) the next bar is a full `beats_per_bar` away.
    """
    beats_into_bar = int(beats_into_bar) % beats_per_bar
    bd = min(max(float(beat_distance), 0.0), 1.0)
    # Position within the bar, in beats: completed beats + progress through now.
    pos = beats_into_bar + bd
    remaining = beats_per_bar - pos
    # If we're within a hair of the downbeat, the "next" bar is a full bar away
    # (avoid firing a zero-length wait that lands us on the current downbeat).
    if remaining <= 1e-6:
        remaining = float(beats_per_bar)
    return remaining


def _seconds_until_bar(beats_into_bar: int, beat_distance: float, bpm: float,
                       beats_per_bar: int = _BEATS_PER_BAR) -> float:
    """Pure: seconds until the next bar downbeat at `bpm`."""
    if not bpm or bpm <= 0:
        bpm = 120.0
    beats = _beats_to_next_bar(beats_into_bar, beat_distance, beats_per_bar)
    return beats * 60.0 / bpm


def _read_beat_phase(deck: int) -> dict | None:
    """Read live beat phase for `deck`: {bpm, beat_distance, beat_active}.

    Returns None if Mixxx is unreachable or the controls are missing (the
    callers then degrade gracefully to the legacy beat-only wait).
    """
    group = f"[Channel{deck}]"
    out: dict = {}
    bd = _mixxx_get(f"/api/control?group={group}&key=beat_distance")
    if _mixxx_failed(bd):
        return None
    try:
        out["beat_distance"] = float(bd.get("value", -1) if isinstance(bd, dict) else -1)
    except (TypeError, ValueError):
        return None
    ba = _mixxx_get(f"/api/control?group={group}&key=beat_active")
    try:
        out["beat_active"] = int(float(ba.get("value", 0))) if not _mixxx_failed(ba) else 0
    except (TypeError, ValueError):
        out["beat_active"] = 0
    st = _mixxx_get("/api/status")
    bpm = 0.0
    if not _mixxx_failed(st) and isinstance(st, dict):
        try:
            bpm = float(st.get(f"deck{deck}", {}).get("bpm", 0) or 0)
        except (TypeError, ValueError):
            bpm = 0.0
    out["bpm"] = bpm
    return out


def _ensure_quantize(deck: int) -> None:
    """E1 invariant: turn Mixxx `quantize` ON for `deck` so cue/loop/play and
    sync land snapped to the beatgrid. Idempotent — safe to call on every
    transition entry (load is owned by load_track, so we re-assert here)."""
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "quantize", "value": 1})


def _wait_bar_boundary(deck: int, beats_per_bar: int = _BEATS_PER_BAR,
                       timeout_s: float = 4.0, threshold: float = 0.05) -> bool:
    """Block until the next BAR downbeat on `deck` (master clock), or timeout.

    Counts `beat_active` rising edges to track position within the bar, then
    returns on the beat that completes a full bar (i.e. the next downbeat).
    The first detected beat seeds the bar-phase, so the very next bar boundary
    is at most `beats_per_bar` beats away. Falls back to the single-beat wait
    (`_wait_phrase_boundary`) if `beat_active` never pulses (control missing).

    Returns True if a bar boundary was hit, False on timeout.

    NOTE: live-validate — without an absolute beat index we can't know WHICH
    beat of the bar we start on; we treat the first observed beat as beat 0 of
    a bar. This still guarantees bar-LENGTH spacing between fires, which is what
    keeps successive transitions phase-consistent. Tighter absolute-downbeat
    alignment needs a Mixxx-side beat-in-bar control (follow-up for integrator).
    """
    deadline = _time_mod.monotonic() + timeout_s
    group = f"[Channel{deck}]"
    beats_counted = 0
    last_active = -1
    saw_any_beat = False
    while _time_mod.monotonic() < deadline:
        r = _mixxx_get(f"/api/control?group={group}&key=beat_active")
        if not _mixxx_failed(r):
            try:
                v = int(float(r.get("value", 0) if isinstance(r, dict) else 0))
            except (TypeError, ValueError):
                v = 0
            if v == 1 and last_active == 0:
                saw_any_beat = True
                beats_counted += 1
                if beats_counted % beats_per_bar == 0:
                    return True
            last_active = v
        _time_mod.sleep(0.02)
    if not saw_any_beat:
        # beat_active unavailable on this build — degrade to a single-beat align.
        return _wait_phrase_boundary(deck, timeout_s=min(2.0, timeout_s), threshold=threshold)
    log.warning(f"bar-boundary wait on deck {deck} timed out after {timeout_s}s")
    return False


def transition_timing_selftest(n: int = 8, deck: int = None,
                               beats_per_bar: int = _BEATS_PER_BAR) -> str:
    """E1 timing self-test: measure bar-boundary alignment drift over N fires.

    For each of `n` iterations it waits for the next bar boundary on the
    master (outgoing) deck and records the residual beat-phase error
    (`beat_distance` at the moment we "fire"). Reports mean/max absolute drift
    as a FRACTION OF A BEAT and the implied % timing error. With no live Mixxx
    this returns a clear "unavailable" string rather than fabricating numbers.

    Args:
        n: number of simulated bar fires to sample (default 8).
        deck: which deck is the master clock; defaults to whichever deck is
            currently playing (per /api/status), else deck 1.
        beats_per_bar: bar length in beats (default 4).
    """
    status = _mixxx_get("/api/status")
    if _mixxx_failed(status):
        return f"timing-selftest: Mixxx unreachable ({_mixxx_failed(status)}) — cannot measure drift."
    if deck is None:
        deck = 1
        if isinstance(status, dict):
            for d in (1, 2):
                if status.get(f"deck{d}", {}).get("playing"):
                    deck = d
                    break
    phase0 = _read_beat_phase(deck)
    if phase0 is None:
        return (f"timing-selftest: beat-phase controls unavailable on deck {deck} "
                f"(beat_distance not exposed). Bar-quantize degrades to beat-only align.")

    errors: list[float] = []
    for _ in range(max(1, n)):
        hit = _wait_bar_boundary(deck, beats_per_bar=beats_per_bar, timeout_s=4.0)
        ph = _read_beat_phase(deck)
        if ph is None:
            break
        bd = ph.get("beat_distance", 0.0)
        # Residual: distance from the nearest beat (0 == perfect). Both ends of
        # the 0..1 range are "on the beat" (just before / just after).
        residual = min(bd, 1.0 - bd) if bd >= 0 else 0.0
        errors.append(residual)
        if not hit:
            break

    if not errors:
        return f"timing-selftest: no bar boundaries observed on deck {deck} (deck not playing?)."
    mean_err = sum(errors) / len(errors)
    max_err = max(errors)
    bpm = phase0.get("bpm") or 120.0
    # 1 beat == (100/beats_per_bar)% of a bar; express drift vs a full beat.
    return (
        f"timing-selftest (deck {deck}, n={len(errors)}, bpm≈{bpm:.1f}): "
        f"mean drift {mean_err*100:.1f}% of a beat, max {max_err*100:.1f}% — "
        f"{'PASS (≤5% of a beat)' if max_err <= 0.05 else 'CHECK (>5% of a beat)'}. "
        f"Bar boundaries quantized to the {beats_per_bar}-beat downbeat of the master deck."
    )


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


def _anchor_target_bpm():
    """The mood profile's BPM-range center, or None if unavailable.

    This is the post-transition tempo anchor. Anchoring every transition to a
    stable, mood-defined tempo stops cumulative sync drift: without it, each
    transition syncs the incoming deck to the (possibly already-drifted) outgoing
    deck and then "keep" leaves it there, so a half-detected/slow track becomes
    the anchor and every later track sync-pulls toward it (observed +20.3% rate).
    """
    try:
        from ..session_state import get_session
        sess = get_session()
        if sess is None:
            return None
        mp = getattr(sess, "mood_profile", None) or {}
        if isinstance(mp, dict):
            rng = mp.get("bpm_range")
            if rng and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng):
                return (float(rng[0]) + float(rng[1])) / 2.0
    except Exception as e:
        log.debug(f"[bpm-anchor] mood lookup failed: {e}")
    return None


def _apply_bpm_after(deck: int, bpm_after: str = "anchor", glide_duration: int = 60):
    """Post-transition BPM handling.

    Default is "anchor" — gently tempo-ride the incoming deck back to the mood
    profile's BPM-range center so per-transition sync drift can't accumulate
    across a set. _tempo_ride no-ops when the deck is already within 0.5 BPM of
    the target, so an on-tempo deck is never touched — the ride only fires when
    the deck has actually drifted. The ride runs after the crossfade completes
    (deck already audible solo), so blocking here is fine.

    bpm_after="anchor" → (default) release sync and SNAP the surviving deck back
                         to its native tempo (rate_ratio=1.0) instantly. During a
                         blend the incoming deck is sync-stretched to beatmatch the
                         outgoing; releasing sync alone LEAVES that stretch, so a
                         wide-BPM-gap mix strands the deck running fast/slow for the
                         rest of the track (observed live: a 149-BPM track left at
                         128 = -14%, and a 128 left at 142 = +11%). The outgoing deck
                         is already paused+silent when this runs (see
                         _finish_channel_fader), so the snap is inaudible as a
                         beat-slip — it just returns the now-solo track to its true
                         speed. This is an INSTANT snap, NOT a glide: the old
                         tempo-RIDE (disabled 2026-05-23) caused audible gradual
                         creep and fought mis-detected beatgrids. A single snap at
                         the transition boundary reads as "new track, its own tempo".
    bpm_after="keep"   → disable sync, leave rate exactly as the blend left it
                         (explicit opt-out of the native snap — for close-BPM mixes
                         where holding the matched tempo is desired).
    bpm_after="reset"  → tempo ride back to native file BPM (explicit only)
    bpm_after="126.5"  → tempo ride to a specific BPM (explicit agent decision)
    """
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})

    if bpm_after == "anchor":
        # Release sync (done above) + snap to native so no synced stretch survives.
        _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate_ratio", "value": 1.0})
        return
    elif bpm_after == "keep":
        # Release sync only; caller explicitly wants the matched tempo held.
        return
    elif bpm_after == "reset":
        _tempo_ride(deck, target_bpm=None, duration_s=max(30, min(120, glide_duration)))
    else:
        try:
            target = float(bpm_after)
            _tempo_ride(deck, target_bpm=target, duration_s=max(30, min(120, glide_duration)))
        except (ValueError, TypeError):
            pass


# Crossfader is parked at center for the whole set — every transition blends
# on the channel (volume) faders, the way club DJs actually mix. The crossfader
# is reserved for scratch/cut work we don't do.
_XF_CENTER = 0.5


def _center_crossfader() -> None:
    _mixxx_post("/api/crossfade", {"position": _XF_CENTER})


def _finish_channel_fader(out_deck: int, to_deck: int, bpm_after: str,
                          glide_duration: int, *, reset_filter: bool = False,
                          out_q_group: str = "") -> None:
    """Club-DJ transition finish: crossfader stays CENTERED; the outgoing deck
    is silenced by its VOLUME fader (not the crossfader), then paused + ejected.
    Resets EQ/volume/filter so the freed deck is clean for the next load.

    This is the shared end-state for every technique so none of them leave the
    crossfader pushed to one side — Manish keeps it centered and rides the
    channel faders, and so does Treta.
    """
    _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})  # fully out
    _mixxx_post("/api/crossfade", {"position": _XF_CENTER})        # keep centered
    _mixxx_post("/api/pause", {"deck": out_deck})
    # Paused now → resetting its volume to 1.0 is silent but leaves it ready.
    _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    for band in ("hi", "mid", "lo"):
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})
    if reset_filter:
        _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
        _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})
    if out_q_group:
        _mixxx_post("/api/control", {"group": out_q_group, "key": "parameter2", "value": 0.0})
    _apply_bpm_after(to_deck, bpm_after, glide_duration)
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})


def do_transition(to_deck: int, duration: int = 60, bpm_after: str = "anchor", glide_duration: int = 60, duration_bars: int = None) -> str:
    """Execute a smooth channel-fader blend to a deck (crossfader stays center).
    Outgoing volume rides down, incoming rides up, with a bass bridge so the
    low ends don't collide. After it completes the outgoing deck is paused +
    ejected and EQ/volume reset.

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

    # Clamp `duration` to fit within the OUTGOING track's remaining audio.
    # Without this, a long crossfade (e.g. 60s) on a track with 30s left
    # runs past the audio cliff — the safety-belt watchdog (Patch C below)
    # catches the cliff but only after a noticeable mid-fade jump. Clamping
    # up front gives a smooth fade for the whole duration. 4s margin so
    # the tail breathes; floor at 10s to keep the fade musical.
    if status:
        try:
            out_rem = float(
                status.get(f"deck{out_deck}", {}).get("remaining_seconds", 0) or 0
            )
        except Exception:
            out_rem = 0.0
        if out_rem > 0 and duration > out_rem - 4:
            new_duration = max(10, int(out_rem - 4))
            log.warning(
                f"[CROSSFADE] clamping duration {duration}s → {new_duration}s "
                f"because outgoing deck{out_deck} only has {out_rem:.1f}s left"
            )
            duration = new_duration

    # Sync + play + phase align (let Mixxx handle BPM matching naturally)
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    # E1: quantize ON for BOTH decks so cue/loop/sync snap to the grid.
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)
    _time.sleep(0.1)

    # Verify it actually started playing
    status2 = _mixxx_get("/api/status")
    if err2 := _mixxx_failed(status2):
        return f"ABORTED: lost Mixxx during transition prep: {err2}"
    if status2:
        deck_state2 = status2.get(f"deck{to_deck}", {})
        if not deck_state2.get("playing", False):
            return f"ABORTED: Deck {to_deck} failed to start playing."

    # E1: bar-quantized start — block until the next DOWNBEAT (bar boundary)
    # on the outgoing deck (the master clock) so the crossfade begins on a
    # musical bar, not a raw beat. Falls back to single-beat align if the
    # build lacks beat_active; fires anyway on timeout (better to mix than stall).
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

    # Crossfader parked center; blend on the channel faders (club-DJ style).
    _center_crossfader()
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})  # cut incoming bass first

    # Channel-fader crossfade, the way Manish rides it:
    #   Phase 1 (first half): bring the INCOMING fader fully UP (outgoing stays
    #     up) — both tracks now playing, incoming bass cut so the low ends
    #     don't collide.
    #   Bass swap: at the midpoint, wait for a DOWNBEAT, then restore the
    #     incoming bass + cut the outgoing bass — on the beat, not at an
    #     arbitrary time.
    #   Phase 2 (second half): bring the OUTGOING fader DOWN to 0.
    fps = 20
    total = max(1, int(duration * fps))
    half = max(1, total // 2)
    forced_end = False
    bass_swapped = False

    def _cliff_hit() -> bool:
        try:
            st = _mixxx_get("/api/status") or {}
            d_out = st.get(f"deck{out_deck}", {})
            rem = float(d_out.get("remaining_seconds", 0) or 0)
            pl = bool(d_out.get("playing", False))
            return (pl and rem < 0.5) or (not pl and rem <= 0)
        except Exception:
            return False

    for i in range(total + 1):
        # Phase 1 — incoming rides up to full; outgoing untouched (full).
        if i <= half:
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(i / half, 3)})
        # Bass swap on the beat, once the incoming fader is up.
        if not bass_swapped and i >= half:
            _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
            _wait_phrase_boundary(out_deck, timeout_s=2.0, threshold=0.05)
            _mixxx_post("/api/eq", {"deck": to_deck, "lo": 1.0})   # restore incoming bass ON BEAT
            _mixxx_post("/api/eq", {"deck": out_deck, "lo": 0.0})  # drop outgoing bass
            bass_swapped = True
        # Phase 2 — outgoing rides down; incoming stays full.
        if i > half:
            vout = 1.0 - (i - half) / max(1, (total - half))
            _mixxx_post("/api/volume", {"deck": out_deck, "level": round(max(0.0, vout), 3)})
        # End-safety: outgoing hit the cliff mid-blend → snap to incoming.
        if i % 5 == 0 and _cliff_hit():
            _mixxx_post("/api/eq", {"deck": to_deck, "lo": 1.0})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})
            forced_end = True
            bass_swapped = True
            log.warning(
                f"[BLEND-FORCE-END] outgoing deck {out_deck} hit end mid-blend — snapped to incoming"
            )
            break
        _time.sleep(1.0 / fps)

    # Ensure incoming bass is up even if the loop broke before the swap.
    if not bass_swapped:
        _mixxx_post("/api/eq", {"deck": to_deck, "lo": 1.0})

    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration, reset_filter=True)
    return f"Transitioned to Deck {to_deck} over {duration}s, channel-fader blend (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_bass_swap(to_deck: int, duration: int = 60, bpm_after: str = "anchor", glide_duration: int = 60, swap_style: str = "trade") -> str:
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

    # Clamp `duration` to fit within the OUTGOING track's remaining audio.
    # bass_swap floor is 20s (smaller than crossfade) because the swap
    # itself only needs ~1 bar; the surrounding fade can be tighter. 4s
    # margin matches crossfade + echo_out for consistency.
    if status:
        try:
            out_rem = float(
                status.get(f"deck{out_deck}", {}).get("remaining_seconds", 0) or 0
            )
        except Exception:
            out_rem = 0.0
        if out_rem > 0 and duration > out_rem - 4:
            new_duration = max(20, int(out_rem - 4))
            log.warning(
                f"[BASS-SWAP] clamping duration {duration}s → {new_duration}s "
                f"because outgoing deck{out_deck} only has {out_rem:.1f}s left"
            )
            duration = new_duration

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

    # E1: quantize both decks + bar-quantized start so the swap lands on a bar.
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

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

    # Cleanup — crossfader stays centered; outgoing already faded to ~0 by
    # phase 3, the finish helper silences it fully via volume + pauses/ejects.
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration)

    return f"Bass-swapped to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_filter_sweep(to_deck: int, duration: int = 45, bpm_after: str = "anchor", glide_duration: int = 60, duration_bars: int = None) -> str:
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
    # Crossfader parked center; incoming starts silent and rides up on its fader.
    _center_crossfader()
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)

    # E1: quantize both decks + bar-quantized start so the sweep opens on a bar.
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

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

        # Level handoff on the channel faders (crossfader stays centered):
        # incoming rides up, outgoing rides down, alongside the filter sweep.
        _mixxx_post("/api/volume", {"deck": to_deck, "level": round(t, 3)})
        _mixxx_post("/api/volume", {"deck": out_deck, "level": round(1.0 - t, 3)})

        _time.sleep(1 / fps)

    # Cleanup — centered crossfader, volume-silenced outgoing, reset filters + Q.
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration,
                          reset_filter=True, out_q_group=out_q_group)

    return f"Filter-swept to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_hard_cut(to_deck: int, bpm_after: str = "anchor", glide_duration: int = 60, align: str = "downbeat") -> str:
    """Hard cut -- instant switch to the other deck. No blend, no crossfade.
    Best for: genre changes, drop moments, high energy transitions.

    Args:
        to_deck: Deck to switch TO (1 or 2).
        bpm_after: "keep", "reset", or a target BPM string.
        glide_duration: Seconds for BPM glide (used when bpm_after != "keep").
        align: When to fire the cut.
            - "downbeat" (default): wait up to 2s for outgoing beat_distance
              near 0 before cutting.
            - "bar": E1 bar-quantized — fire on the next BAR downbeat (every
              4th beat) of the outgoing master clock. Tightest musical cut.
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
    # E1: quantize on BOTH decks so play landings + the cut snap to the grid.
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)

    # Alignment: optional wait for downbeat / bar / phrase boundary before firing.
    if align == "bar":
        # E1: fire on the next BAR downbeat of the outgoing master clock.
        _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)
    elif align == "phrase":
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

    # Instant volume swap (crossfader stays centered): incoming full, outgoing
    # silenced. No crossfader slam — matches the channel-fader workflow.
    _center_crossfader()
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    _mixxx_post("/api/play", {"deck": to_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration)

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


def do_echo_out(to_deck: int, duration: int = 32, bpm_after: str = "anchor", glide_duration: int = 60, freeze: bool = False, wait_for_incoming_drop: bool = True) -> str:
    """Echo out -- a wash-blend, not a hard cut. Echo wet ramps up on the
    outgoing during Phase A; through Phase B the outgoing fader rides DOWN
    while the incoming fader rides UP simultaneously, with the echo wash
    sitting on top of both. Both decks are audible together for ~65% of the
    transition, so the seam is masked by the wash and there is no silent
    valley between cut and rise.

    Best for: energy shifts, key/BPM gaps, mood changes, dramatic moments.

    Phase shape (over `duration` seconds, freeze=False):
      A  0%-25%  : outgoing volume held at 1.0. Wet ramps smoothly 0 → 0.7.
      B  25%-90% : DUAL FADE — outgoing 1.0 → 0.0 simultaneous with incoming
                   0.0 → 1.0. Wet pinned at 0.7 throughout (echo wash sits
                   on top of the dual-deck blend).
      C  90%-100%: outgoing 0.0, incoming 1.0. Wet still 0.7.
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

    # Clamp `duration` to fit within the outgoing track's remaining audio.
    # Without this, a long echo_out (e.g. 32s) on a track with only 20s
    # left runs PAST the audio cliff: outgoing file ends mid-Phase-B while
    # incoming is still ramping up — listener hears "one deck ended while
    # the other is still rising". 4s safety margin lets the tail breathe
    # without riding the cliff.
    if status:
        try:
            out_remaining = float(
                status.get(f"deck{out_deck}", {}).get("remaining_seconds", 0) or 0
            )
        except Exception:
            out_remaining = 0.0
        if out_remaining > 0 and duration > out_remaining - 4:
            new_duration = max(10, int(out_remaining - 4))
            log.warning(
                f"[ECHO-OUT] clamping duration {duration}s → {new_duration}s "
                f"because outgoing deck{out_deck} only has {out_remaining:.1f}s left"
            )
            duration = new_duration

    # Center the crossfader so both decks share the master via the FX bus.
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Start incoming silently + synced.
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    # E1: quantize both decks so the echo wash + handoff stay grid-locked.
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)

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

    # Phase boundaries. Phase A is short — just enough to ramp the wet up to
    # peak. Phase B is the dual-fade overlap where both decks are audible
    # together, with the echo wash sitting on top. No hard cut anywhere; the
    # outgoing simply rides down as the incoming rides up.
    PHASE_A_END = 0.25
    PHASE_B_END = 0.90
    WET_PEAK = 0.7          # echo wet/dry during the wash blend

    a_end_idx = int(total * PHASE_A_END)
    b_end_idx = int(total * PHASE_B_END)
    last_wet_sent = -1.0
    blend_started = False

    def _set_wet(value: float):
        nonlocal last_wet_sent
        if abs(value - last_wet_sent) > 1e-3:
            _mixxx_post("/api/control", {
                "group": "[EffectRack1_EffectUnit1]",
                "key": "mix",
                "value": round(value, 3),
            })
            last_wet_sent = value

    for i in range(total + 1):
        t = i / total if total else 1.0

        if i <= a_end_idx:
            # Phase A: outgoing held at 1.0. Wet ramps smoothly 0 → 0.7.
            # HPF/EQ untouched — the kick keeps punching while the echo
            # builds up the wash.
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
            a = i / max(1, a_end_idx)
            _set_wet(WET_PEAK * a)

        elif i <= b_end_idx:
            # Phase B opens (once) with the buildup-aware hold: if incoming
            # is still in its intro, pin both decks at Phase A shape until
            # incoming reaches its drop, capped at +32s. Wait-cap safety
            # abort still fires if outgoing runs out during the hold.
            if not blend_started:
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
                        _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
                        _set_wet(WET_PEAK)
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
                            try:
                                outgoing_rem = float(
                                    in_status.get(f"deck{out_deck}", {})
                                    .get("remaining_seconds", 0) or 0
                                )
                            except Exception:
                                outgoing_rem = 0.0
                            if outgoing_rem < 5.0:
                                log.warning(
                                    f"[ECHO-OUT-ABORT] outgoing deck{out_deck} "
                                    f"remaining {outgoing_rem:.1f}s < 5s while "
                                    f"waiting for incoming drop "
                                    f"(in_pos {in_pos:.1f}/{incoming_intro_ends:.1f}); "
                                    f"starting dual-fade now to avoid silence"
                                )
                                break
                            if in_pos >= incoming_intro_ends:
                                log.info(
                                    f"[ECHO-OUT] incoming hit drop at "
                                    f"{in_pos:.1f}s after {waited:.1f}s wait — "
                                    f"starting dual-fade"
                                )
                                break
                        else:
                            log.warning(
                                f"[ECHO-OUT] still in buildup after {waited:.0f}s "
                                f"wait (pos {in_pos:.1f} < {incoming_intro_ends:.1f}); "
                                f"falling through to avoid hang"
                            )

                # Pin wet at peak for the duration of the dual-fade.
                _set_wet(WET_PEAK)
                blend_started = True

            # Phase B: dual fade. Both decks audible together; echo wash on
            # top. b runs 0 → 1 across Phase B. No hard cut, no LO-EQ kill;
            # the kicks blend naturally beneath the wet.
            b = (t - PHASE_A_END) / (PHASE_B_END - PHASE_A_END)
            b = max(0.0, min(1.0, b))
            out_vol = 1.0 - b
            in_vol = b
            _mixxx_post("/api/volume", {"deck": out_deck, "level": round(out_vol, 3)})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(in_vol, 3)})

        else:
            # Phase C: outgoing 0, incoming pinned 1.0, wet still at peak.
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
            _set_wet(WET_PEAK)

        _time.sleep(1.0 / fps)

    # Crossfader stays centered — outgoing is already silenced via its volume
    # fader (set to 0 above). Echo tail still ringing through the FX send.

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


def do_riser(to_deck: int, duration: int = 32, bpm_after: str = "anchor", glide_duration: int = 60, duration_bars: int = None) -> str:
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

    # E1: quantize both decks + bar-quantized start (riser build begins on a bar).
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

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

    # Cleanup — crossfader stays centered; outgoing already at 0 volume.
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration,
                          reset_filter=True, out_q_group=out_q_group)

    return f"Riser to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


def do_dissolve(to_deck: int, duration: int = 16, bpm_after: str = "anchor", glide_duration: int = 60, duration_bars: int = None) -> str:
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
    # E1: quantize both decks + bar-quantized start for the dissolve.
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

    # Linear volume crossfade. No EQ/filter changes.
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total if total else 1.0
        _mixxx_post("/api/volume", {"deck": out_deck, "level": round(1.0 - t, 3)})
        _mixxx_post("/api/volume", {"deck": to_deck, "level": round(t, 3)})
        _time.sleep(1.0 / fps)

    # Cleanup — crossfader stays centered; outgoing already at 0 volume.
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration)

    return f"Dissolved to Deck {to_deck} over {duration}s (bpm_after={bpm_after}). Deck {out_deck} ejected."


# ──────────────────────────────────────────────────────────────────────────
# E2 — Native FX Transition Engine
#
# Drives Mixxx's NATIVE effects via the EXISTING /api/control endpoint. No new
# C++ endpoints, no VST host (deferred to v11). We use EffectRack1's effect
# units, addressed by their standard control-object group names:
#   [EffectRack1_EffectUnitN]                 → unit-level: enabled, mix,
#                                                group_[ChannelM]_enable
#   [EffectRack1_EffectUnitN_EffectK]         → slot-level: enabled, metaknob,
#                                                parameterX, loaded
# To keep units from fighting each other we reserve:
#   Unit 1  → Echo (already used by do_echo_out)
#   Unit 2  → Delay throw   (do_delay_throw)
#   Unit 3  → Reverb tail   (do_reverb_tail)
# The EQ side-chain duck (do_sidechain_duck) is pure EQ/volume automation —
# no effect unit needed, so it always works regardless of which effects are
# loaded in the rack.
#
# Effect LOADING (which DSP sits in a slot) is the one part that varies by
# Mixxx build. `_fx_load_effect` attempts the documented `loaded` /
# `effect_selector` writes but treats failure as a no-op: if the named effect
# isn't loadable via /api/control on this build, the routine still runs the
# volume/EQ automation (so the transition completes) but the wet tail is
# silent — a clearly observable, non-fatal degrade. See FOLLOW-UPS in the
# module-level note for the integrator.
# ──────────────────────────────────────────────────────────────────────────


def _fx_unit(n: int) -> str:
    return f"[EffectRack1_EffectUnit{n}]"


def _fx_load_effect(unit_n: int, slot: int, effect_id: str) -> None:
    """Best-effort: load `effect_id` (e.g. "Echo", "Reverb") into a slot.

    Tries the control-object writes documented for Mixxx 2.4
    (`[EffectRack1_EffectUnitN_EffectK]` key `loaded` + an effect-group
    selector). Builds differ on the exact mechanism, so any failure is a
    silent no-op — the caller's volume/EQ automation still runs and we rely
    on a pre-loaded effect if the load didn't take. Documented as a follow-up.
    """
    slot_group = f"[EffectRack1_EffectUnit{unit_n}_Effect{slot}]"
    # Some builds expose a string control to pick the effect by id; others
    # only allow it via the GUI / a numeric next_effect cycling control. We
    # attempt the string write and move on regardless of the response.
    _mixxx_post("/api/control", {"group": slot_group, "key": "loaded", "value": 1})
    _mixxx_post("/api/control", {"group": slot_group, "key": "effect_selector",
                                 "value": effect_id})
    _mixxx_post("/api/control", {"group": slot_group, "key": "enabled", "value": 1})


def _fx_engage(unit_n: int, deck: int, slot: int = 1, effect_id: str = "") -> str:
    """Route `deck` through EffectUnit `unit_n`, mix=0 (silent insert).

    Returns the unit group string for convenience. Optionally tries to load
    `effect_id` into the slot first. Caller ramps `mix` up afterwards.
    """
    unit = _fx_unit(unit_n)
    if effect_id:
        _fx_load_effect(unit_n, slot, effect_id)
    _mixxx_post("/api/control", {"group": unit, "key": "mix", "value": 0.0})
    _mixxx_post("/api/control", {"group": unit, "key": "enabled", "value": 1})
    _mixxx_post("/api/control", {"group": unit,
                                 "key": f"group_[Channel{deck}]_enable", "value": 1})
    _mixxx_post("/api/control", {"group": f"[EffectRack1_EffectUnit{unit_n}_Effect{slot}]",
                                 "key": "enabled", "value": 1})
    return unit


def _fx_disengage(unit_n: int, deck: int) -> None:
    """Tear down FX routing for `deck` on unit `unit_n`. Idempotent."""
    unit = _fx_unit(unit_n)
    _mixxx_post("/api/control", {"group": unit, "key": "mix", "value": 0.0})
    _mixxx_post("/api/control", {"group": unit,
                                 "key": f"group_[Channel{deck}]_enable", "value": 0})


def _fx_set_wet(unit_n: int, value: float) -> None:
    _mixxx_post("/api/control", {"group": _fx_unit(unit_n), "key": "mix",
                                 "value": round(min(max(value, 0.0), 1.0), 3)})


def do_delay_throw(to_deck: int, duration: int = 24, bpm_after: str = "anchor",
                   glide_duration: int = 60, duration_bars: int = None,
                   throw_beats: int = 4) -> str:
    """E2 FX technique — DELAY THROW. A post-fader echo "throw" on the outgoing
    deck: at the bar boundary the outgoing is cut while its last `throw_beats`
    ring out through a wet delay tail, and the incoming opens underneath. The
    classic acapella/vocal-tag throw — momentum carries across the seam on the
    delay, not on a long blend.

    Best for: vocal tags, breakdown exits, energy lifts where you want a
    rhythmic echo to bridge the cut rather than a long volume fade.

    Shape (bar-quantized, master clock = outgoing deck):
      0%   : engage Delay unit on outgoing (mix=0). Wait for next bar downbeat.
      cut  : on the downbeat — outgoing wet snaps to ~0.85, outgoing fader
             drops to 0 over ~`throw_beats` beats (the tail keeps ringing),
             incoming fader rides 0 → 1 simultaneously.
      tail : wet decays 0.85 → 0 over the throw window; then disengage + eject.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Total seconds (8-48). Default 24.
        bpm_after: "anchor" | "keep" | "reset" | target-BPM string.
        glide_duration: Seconds for BPM glide (when bpm_after != anchor/keep).
        duration_bars: Optional — override duration from bars at outgoing BPM.
        throw_beats: Length of the throw tail in beats (default 4 = 1 bar).

    FOLLOW-UP: relies on a Delay/Echo effect being loadable into EffectUnit2
    via /api/control on this build; if not pre-loaded the fader automation
    still runs but the echo tail is silent. See module note.
    """
    import time as _time

    out_deck = 1 if to_deck == 2 else 2
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        ds = status.get(f"deck{to_deck}", {})
        if not ds.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        if float(ds.get("remaining_seconds", 0) or 0) < 20:
            return f"ABORTED: Deck {to_deck} track too short -- load a fresh track first."

    out_bpm = 120.0
    if status:
        try:
            out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 120.0) or 120.0) or 120.0
        except Exception:
            out_bpm = 120.0

    if duration_bars is not None:
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))
    duration = max(8, min(48, duration))

    # Start incoming silently + synced.
    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    _center_crossfader()
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)

    # Engage delay on the outgoing deck (Unit 2), then fire on a bar downbeat.
    _fx_engage(2, out_deck, slot=1, effect_id="Echo")
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

    # Throw: snap wet up, cut outgoing fader over the throw window while the
    # incoming rides up. Tail length = throw_beats at the outgoing tempo.
    throw_s = max(0.5, throw_beats * 60.0 / out_bpm)
    _fx_set_wet(2, 0.85)
    fps = 20
    steps = max(4, int(throw_s * fps))
    for i in range(steps + 1):
        t = i / steps
        _mixxx_post("/api/volume", {"deck": out_deck, "level": round(1.0 - t, 3)})
        _mixxx_post("/api/volume", {"deck": to_deck, "level": round(t, 3)})
        # Wet decays across the throw so the echo rings out instead of cutting.
        _fx_set_wet(2, 0.85 * (1.0 - t))
        _time.sleep(1.0 / fps)

    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _fx_set_wet(2, 0.0)
    _fx_disengage(2, out_deck)
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration)
    return (f"Delay-throw to Deck {to_deck} ({throw_beats}-beat tail, "
            f"bpm_after={bpm_after}). Deck {out_deck} ejected.")


def do_reverb_tail(to_deck: int, duration: int = 32, bpm_after: str = "anchor",
                   glide_duration: int = 60, duration_bars: int = None) -> str:
    """E2 FX technique — REVERB TAIL. The outgoing deck dissolves into a
    swelling reverb wash while the incoming opens underneath; the outgoing is
    then cut and its reverb tail rings out over the incoming. A smoother,
    "ambient" cousin of the echo-out — best for melodic/atmospheric handoffs
    and key changes where a wet space masks the seam.

    Shape (bar-quantized):
      A 0-40% : outgoing held up; reverb wet ramps 0 → 0.7. Incoming silent.
      B 40-85%: dual fade — outgoing 1→0, incoming 0→1, wet pinned at 0.7.
      C 85-100% + tail : outgoing paused; reverb wet decays 0.7 → 0 so the
                tail rings out over the now-solo incoming. Then disengage+eject.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Seconds (12-48). Default 32 (~16 bars at 120 BPM).
        bpm_after: "anchor" | "keep" | "reset" | target-BPM string.
        glide_duration: Seconds for BPM glide.
        duration_bars: Optional — override duration from bars at outgoing BPM.

    FOLLOW-UP: relies on a Reverb effect loadable into EffectUnit3 via
    /api/control; if not pre-loaded the fades still run but the tail is silent.
    """
    import time as _time

    out_deck = 1 if to_deck == 2 else 2
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        ds = status.get(f"deck{to_deck}", {})
        if not ds.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        if float(ds.get("remaining_seconds", 0) or 0) < 20:
            return f"ABORTED: Deck {to_deck} track too short -- load a fresh track first."

    out_bpm = 120.0
    if status:
        try:
            out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 120.0) or 120.0) or 120.0
        except Exception:
            out_bpm = 120.0
    if duration_bars is not None:
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))
    duration = max(12, min(48, duration))

    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    _center_crossfader()
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)

    _fx_engage(3, out_deck, slot=1, effect_id="Reverb")
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

    fps = 10
    total = int(duration * fps)
    A_END, B_END, WET_PEAK = 0.40, 0.85, 0.7
    for i in range(total + 1):
        t = i / total if total else 1.0
        if t <= A_END:
            a = t / A_END
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 1.0})
            _fx_set_wet(3, WET_PEAK * a)
        elif t <= B_END:
            b = (t - A_END) / (B_END - A_END)
            _mixxx_post("/api/volume", {"deck": out_deck, "level": round(1.0 - b, 3)})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": round(b, 3)})
            _fx_set_wet(3, WET_PEAK)
        else:
            _mixxx_post("/api/volume", {"deck": out_deck, "level": 0.0})
            _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
            _fx_set_wet(3, WET_PEAK)
        _time.sleep(1.0 / fps)

    # Pause outgoing; let the reverb tail ring out (~6 beats, clamped 2-6s).
    _mixxx_post("/api/pause", {"deck": out_deck})
    tail_s = max(2.0, min(6.0, (60.0 / out_bpm) * 6.0))
    tail_steps = max(6, int(tail_s * fps))
    for s in range(tail_steps + 1):
        _fx_set_wet(3, WET_PEAK * (1.0 - s / tail_steps))
        _time.sleep(tail_s / tail_steps)

    _fx_disengage(3, out_deck)
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration)
    return (f"Reverb-tail to Deck {to_deck} over {duration}s "
            f"(bpm_after={bpm_after}). Deck {out_deck} ejected.")


def do_sidechain_duck(to_deck: int, duration: int = 32, bpm_after: str = "anchor",
                      glide_duration: int = 60, duration_bars: int = None,
                      pump_beats: int = 1) -> str:
    """E2 FX technique — SIDE-CHAIN-STYLE EQ DUCK. Emulates LFO-Tool-style
    side-chain pumping without a VST: the incoming deck's volume is "ducked"
    on every downbeat (sharp drop, smooth recover over the beat) so it appears
    to breathe under the outgoing kick — the produced pump deadmau5 gets from a
    side-chain compressor. As the blend progresses the duck depth eases off and
    the incoming settles to steady. Pure volume/EQ automation — no effect unit,
    so it works on any build.

    Best for: house/techno energy lifts and layered intros where you want the
    incoming pad/bass to pulse with the outgoing groove before taking over.

    Shape (bar-quantized):
      Both decks audible. Incoming rides 0 → 1 over the duration; ON every beat
      its volume is ducked to (1 - depth) then recovers across the beat. `depth`
      starts at ~0.6 and eases to 0 by the end so the pump fades into a clean
      steady level as the outgoing drops away.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Seconds (12-48). Default 32.
        bpm_after: "anchor" | "keep" | "reset" | target-BPM string.
        glide_duration: Seconds for BPM glide.
        duration_bars: Optional — override duration from bars at outgoing BPM.
        pump_beats: Period of the pump in beats (default 1 = duck every beat).
    """
    import time as _time

    out_deck = 1 if to_deck == 2 else 2
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        ds = status.get(f"deck{to_deck}", {})
        if not ds.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        if float(ds.get("remaining_seconds", 0) or 0) < 20:
            return f"ABORTED: Deck {to_deck} track too short -- load a fresh track first."

    out_bpm = 120.0
    if status:
        try:
            out_bpm = float(status.get(f"deck{out_deck}", {}).get("bpm", 120.0) or 120.0) or 120.0
        except Exception:
            out_bpm = 120.0
    if duration_bars is not None:
        duration = int(round(_bars_to_seconds(duration_bars, out_bpm)))
    duration = max(12, min(48, duration))
    pump_beats = max(1, int(pump_beats))

    _mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.2)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    _center_crossfader()
    _ensure_quantize(to_deck)
    _ensure_quantize(out_deck)
    _wait_bar_boundary(out_deck, timeout_s=4.0, threshold=0.05)

    beat_s = 60.0 / out_bpm
    pump_period = beat_s * pump_beats
    fps = 20
    total = int(duration * fps)
    elapsed = 0.0
    for i in range(total + 1):
        t = i / total if total else 1.0
        base_in = t                      # incoming rises 0 → 1
        out_vol = 1.0 - t                # outgoing rides down
        depth = 0.6 * (1.0 - t)          # pump depth eases to 0 by the end
        # Phase within the current pump period (0 = just ducked).
        phase = (elapsed % pump_period) / pump_period
        # Sharp drop on the downbeat, exponential-ish recover over the period.
        duck = depth * (1.0 - phase) ** 2
        in_vol = max(0.0, base_in * (1.0 - duck))
        _mixxx_post("/api/volume", {"deck": to_deck, "level": round(in_vol, 3)})
        _mixxx_post("/api/volume", {"deck": out_deck, "level": round(max(0.0, out_vol), 3)})
        _time.sleep(1.0 / fps)
        elapsed += 1.0 / fps

    _mixxx_post("/api/volume", {"deck": to_deck, "level": 1.0})
    _finish_channel_fader(out_deck, to_deck, bpm_after, glide_duration)
    return (f"Side-chain duck to Deck {to_deck} over {duration}s "
            f"(pump every {pump_beats} beat[s], bpm_after={bpm_after}). "
            f"Deck {out_deck} ejected.")


def schedule_transition(to_deck: int, at_position: int = 0, technique: str = "crossfade", duration: int = 45, bpm_after: str = "anchor", glide_duration: int = 60, at_section_marker: str = "") -> str:
    """Schedule a transition at a specific track position. Returns immediately --
    Python executes the transition in the background when the track reaches at_position.

    Call this when you've decided the right moment to transition based on
    the track timeline (e.g., at a breakdown or outro). You can pass either
    `at_position` (seconds) OR `at_section_marker` — both are OPTIONAL. If you
    pass neither (or 0), it defaults to the active track's analyzed mix-out
    (the canonical outro), so a bare schedule_transition(to_deck=N) is valid.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        at_position: Track position in seconds to START the transition.
            OPTIONAL — defaults to the active track's mix-out if omitted.
            Server clamps to [current_position+5, duration-5-transition_duration]
            so a hallucinated or stale value can't fire past-end / in-the-past.
        technique: "crossfade" (smooth blend), "bass_swap" (EQ swap, techno), "filter_sweep" (progressive reveal), "echo_out" (fade with echo, mood shift), "hard_cut" (instant switch, genre change), "riser" (HP+resonance build then handoff), "dissolve" (short clean volume crossfade), "delay_throw" (E2 — post-fader echo throw on the cut, vocal tags/lifts), "reverb_tail" (E2 — ambient reverb wash + ring-out, melodic/key changes), "sidechain_duck" (E2 — side-chain-style pump on the incoming, house/techno lifts).
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

    # ── Mood-guard: continuous-energy moods (Patch C) ──
    # Belt-and-braces. Even if the DJ prompt rule slips and Flash schedules
    # echo_out for psy-trance / hard-techno / etc., coerce to bass_swap so
    # the listener never hears the energy-hole failure mode. Pulled from
    # session_state.mood_profile.canonical_slug; failure to read = no-op.
    _CONT_ENERGY = {
        "psy-trance", "psytrance", "psy_trance",
        "peak-time", "peak-time-techno", "peak_time_techno",
        "hard-techno", "hard_techno",
        "drum-n-bass", "drum-and-bass", "dnb", "drum_n_bass",
        "hardstyle",
        "big-room", "big_room",
    }
    if technique == "echo_out":
        try:
            from ..session_state import get_session
            _sess = get_session()
            _slug = ""
            if _sess is not None:
                _mp = getattr(_sess, "mood_profile", None) or {}
                if isinstance(_mp, dict):
                    _slug = (_mp.get("canonical_slug") or "").strip().lower()
            if _slug in _CONT_ENERGY:
                log.warning(
                    f"[MOOD-GUARD] active mood '{_slug}' is continuous-energy; "
                    f"coercing technique echo_out → bass_swap (echo_out creates "
                    f"audible energy hole, breaks kick wall)"
                )
                technique = "bass_swap"
        except Exception as _e:
            log.warning(f"[MOOD-GUARD] mood lookup failed, no coercion: {_e}")

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

    # If no usable at_position was given (and no marker resolved), default to
    # the active track's analyzed mix-out (the canonical outro), else a sensible
    # lead from the current position. This makes a bare schedule_transition()
    # call valid instead of erroring on a missing at_position — which was
    # causing the agent to retry the same call repeatedly.
    if at_position <= 0:
        fallback = None
        try:
            from ..db import get_track_by_path
            tinfo = _mixxx_get(f"/api/deck/{active_deck}/track_info") or {}
            fp = tinfo.get("file_path", "")
            meta = get_track_by_path(fp) if fp else None
            if meta and meta.get("mix_out_seconds") is not None:
                fallback = float(meta["mix_out_seconds"])
        except Exception:
            fallback = None
        at_position = int(fallback if fallback is not None else (current_pos + 30))

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

    # Capture the active track's file_path at scheduling time so the
    # executor can detect a stale schedule (planner force-load between
    # schedule and fire would leave at_position pointing at a track that
    # no longer exists on the deck — Patch B / overshoot-safety).
    active_track_path = ""
    try:
        _tinfo = _mixxx_get(f"/api/deck/{active_deck}/track_info") or {}
        active_track_path = _tinfo.get("file_path", "") or ""
    except Exception:
        active_track_path = ""

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
        "activeTrackPath": active_track_path,
        "activeTrackDuration": track_duration,
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
