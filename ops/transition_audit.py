"""Continuous transition audit — watches DJ Treta's autonomous transition
decisions and scores each against the analyzed waveform/section data of BOTH
tracks: timing (does she fire at the outro / mix-out point), BPM gap, key
(Camelot) compatibility, and whether the technique fits.

Run:  .venv/bin/python3 ops/transition_audit.py   (writes /tmp/transition-audit.log)
"""
import json
import time
import urllib.request

from agent.db import get_track_by_path

STATE = "http://localhost:7779/http/state"
LOG = "/tmp/transition-audit.log"

# Camelot adjacency: compatible = same code, ±1 number (same letter), or
# same number opposite letter (relative major/minor).
def _camelot_compat(a, b):
    if not a or not b:
        return "?"
    try:
        na, la = int(a[:-1]), a[-1].upper()
        nb, lb = int(b[:-1]), b[-1].upper()
    except Exception:
        return "?"
    if a.upper() == b.upper():
        return "perfect (same key)"
    if na == nb and la != lb:
        return "compatible (relative maj/min)"
    if la == lb and (abs(na - nb) == 1 or abs(na - nb) == 11):
        return "compatible (±1 energy)"
    return f"CLASH ({a}->{b})"


def _get(url):
    try:
        return json.load(urllib.request.urlopen(url, timeout=4))
    except Exception:
        return None


def _section_at(track_path, pos):
    tk = get_track_by_path(track_path) if track_path else None
    if not tk:
        return None, None, None
    mix_out = tk.get("mix_out_seconds")
    mix_in = tk.get("mix_in_seconds")
    tl = tk.get("timeline")
    try:
        tl = json.loads(tl) if isinstance(tl, str) else (tl or [])
    except Exception:
        tl = []
    sec = None
    for s in tl:
        try:
            if float(s["start"]) <= pos <= float(s["end"]):
                sec = f"{s['section']}(e{s['energy']})"
                break
        except Exception:
            pass
    return mix_out, mix_in, sec


def _sections(track_path):
    tk = get_track_by_path(track_path) if track_path else None
    if not tk:
        return []
    tl = tk.get("timeline")
    try:
        return json.loads(tl) if isinstance(tl, str) else (tl or [])
    except Exception:
        return []


def _energy_at(track_path, pos):
    for s in _sections(track_path):
        try:
            if float(s["start"]) <= pos <= float(s["end"]):
                return float(s.get("energy", 0))
        except Exception:
            pass
    return None


def _open_energy(track_path, mix_in):
    """Energy of the incoming track at its groove-in point (mix_in), i.e. what
    the listener hears as it's brought in."""
    if mix_in is not None:
        e = _energy_at(track_path, mix_in)
        if e is not None:
            return e
    secs = _sections(track_path)
    # fall back to the first non-intro section's energy, else first section
    for s in secs:
        try:
            if s.get("section") not in ("intro",):
                return float(s.get("energy", 0))
        except Exception:
            pass
    return float(secs[0].get("energy", 0)) if secs else None


def audit(sched, state):
    to_deck = sched.get("toDeck") or sched.get("to_deck")
    at_pos = sched.get("atPosition") or sched.get("at_position") or 0
    tech = sched.get("technique", "?")
    dur = sched.get("duration", "?")
    out_deck = 1 if to_deck == 2 else 2

    cur = state.get("current_track", {}) or {}
    nxt = state.get("next_track", {}) or {}
    out_path = cur.get("file_path") or cur.get("path", "")
    in_path = nxt.get("file_path") or nxt.get("path", "")

    out_tk = get_track_by_path(out_path) if out_path else {}
    in_tk = get_track_by_path(in_path) if in_path else {}
    out_bpm = (out_tk or {}).get("bpm") or cur.get("bpm")
    in_bpm = (in_tk or {}).get("bpm") or nxt.get("bpm")
    out_key = (out_tk or {}).get("key_camelot")
    in_key = (in_tk or {}).get("key_camelot")
    mix_out, _, out_sec_at_fire = _section_at(out_path, at_pos)

    # Timing: how far is the fire point from the outgoing track's outro/mix-out?
    timing = "?"
    if mix_out:
        delta = at_pos - mix_out
        if -8 <= delta <= 20:
            timing = f"GOOD (fires ~outro, {delta:+.0f}s vs mix_out {mix_out:.0f})"
        elif delta < -8:
            timing = f"EARLY ({-delta:.0f}s before mix_out {mix_out:.0f})"
        else:
            timing = f"LATE ({delta:.0f}s after mix_out {mix_out:.0f})"

    bpm_gap = None
    try:
        bpm_gap = abs(float(out_bpm) - float(in_bpm))
    except Exception:
        pass
    bpm_gap_ok = bpm_gap is not None and bpm_gap <= 6
    bpm_verdict = (f"{bpm_gap:.1f} BPM gap "
                   + ("OK" if bpm_gap_ok else "WIDE")) if bpm_gap is not None else "?"
    key_verdict = _camelot_compat(out_key, in_key)
    key_ok = key_verdict.startswith(("perfect", "compatible"))

    # ── SELECTION audit: read both waveforms, judge energy continuity ──
    _, in_mix_in, _ = _section_at(in_path, 0)
    out_e = _energy_at(out_path, at_pos)      # outgoing energy where the blend starts
    in_e = _open_energy(in_path, in_mix_in)   # incoming energy as it grooves in
    energy_verdict = "?"
    energy_ok = True
    if out_e is not None and in_e is not None:
        de = in_e - out_e
        if abs(de) <= 2:
            energy_verdict = f"smooth (out e{out_e:.0f} -> in e{in_e:.0f})"
        elif de < -2:
            energy_verdict = f"ENERGY DROP (out e{out_e:.0f} -> in e{in_e:.0f}, {de:.0f})"
            energy_ok = False
        else:
            energy_verdict = f"ENERGY JUMP (out e{out_e:.0f} -> in e{in_e:.0f}, +{de:.0f})"
            energy_ok = False

    # Combined selection verdict.
    reasons = []
    if not key_ok and key_verdict != "?":
        reasons.append("key clash")
    if bpm_gap is not None and not bpm_gap_ok:
        reasons.append(f"BPM gap {bpm_gap:.0f}")
    if not energy_ok:
        reasons.append("energy break")
    if in_bpm is None or in_key is None:
        reasons.append("incoming UNANALYZED")
    if reasons:
        selection = "NEEDS IMPROVEMENT — " + ", ".join(reasons)
    elif "?" in (key_verdict, bpm_verdict, energy_verdict):
        selection = "partial data"
    else:
        selection = "GOOD pick (harmonic + tempo + energy all fit)"

    line = (
        f"[{time.strftime('%H:%M:%S')}] TRANSITION technique={tech} dur={dur}s "
        f"deck{out_deck}->deck{to_deck} @pos={at_pos}\n"
        f"    OUT: {(cur.get('title') or '')[:34]} bpm={out_bpm} key={out_key} "
        f"| fires in section: {out_sec_at_fire}\n"
        f"    IN : {(nxt.get('title') or '')[:34]} bpm={in_bpm} key={in_key}\n"
        f"    TIMING: {timing}\n"
        f"    BEATMATCH: {bpm_verdict}  |  KEY: {key_verdict}  |  ENERGY: {energy_verdict}\n"
        f"    SELECTION: {selection}\n"
    )
    return line


def main():
    last_sig = None
    with open(LOG, "a") as f:
        f.write(f"\n=== audit started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.flush()
        while True:
            state = _get(STATE)
            if state:
                sched = state.get("scheduled_transition")
                if sched:
                    sig = json.dumps(sched, sort_keys=True)
                    if sig != last_sig:
                        last_sig = sig
                        f.write(audit(sched, state))
                        f.flush()
                else:
                    last_sig = None
            time.sleep(3)


if __name__ == "__main__":
    main()
