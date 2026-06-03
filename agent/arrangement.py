"""Dynamic real-time arrangement authoring — the E5 headline leapfrog.

deadmau5's Autopilot lets a human hand-author a *static* timeline of mixer
"States" and drag them onto a grid offline. DJ Treta's leapfrog: the planner
constructs a **rolling sequence of future musical intents** LIVE, toward a
high-level goal ("build energy for 16 bars → drop into a breakdown → 8-bar
loop roll → next track"), and re-derives it every cycle as the room changes.

LAYERING (important for the integration boundary):

  * `ArrangementIntent` (this module, Agent B / E5) is a PLANNER-LEVEL object:
    *what* should happen musically next — a target track, a transition
    *technique*, an energy target, a bar duration, and (optionally) a loop-cue
    to exploit. It is intentionally NOT a mixer snapshot.

  * Agent C owns the concrete mixer "State" model (`agent/state_sequence.py`):
    deck volumes, EQ, filter, xfader, tempo. At integration, each
    `ArrangementIntent` MAPS ONTO one or more of C's States (an intent's
    `energy_target` + `technique` + `bars` parameterize the State snapshots C
    records/replays). This module never imports or constructs C's State — it
    only emits intent. The mapping function lives at the integration seam.

  * Agent A owns transition execution (`agent/tools/transitions.py`). An
    intent's `technique` is a hint drawn from A's vocabulary (filter_sweep,
    bass_swap, echo_out, riser, dissolve, hard_cut, loop_roll). Treta's DJ
    agent realizes the intent by calling A's `schedule_transition` /
    `do_transition`. This module never calls A directly.

So: E3 gives us the analysis + cues; E5 reasons over them to emit a short
ROLLING PLAN of intents; Agents A and C realize the plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# Transition techniques the planner may request. Mirrors the vocabulary Agent A
# exposes via do_transition(technique=...). Kept as a tuple of strings so this
# module has zero import dependency on transitions.py (owned by Agent A).
ARRANGEMENT_TECHNIQUES = (
    "blend",        # long EQ/volume blend (default, safe)
    "bass_swap",    # swap basslines on the downbeat
    "filter_sweep", # HPF/LPF sweep across the blend
    "echo_out",     # delay throw on the outgoing track
    "riser",        # energy build into a drop
    "dissolve",     # soft reverb-tail dissolve
    "hard_cut",     # cut on the one (high-energy)
    "loop_roll",    # exploit a LOOP cue as creative material (E5 loop reasoning)
)

# High-level arrangement goals the planner can pursue. Free-form goals are also
# allowed (the LLM may phrase its own); these are the canonical shapes.
ARRANGEMENT_GOALS = (
    "build",        # ramp energy up over N bars
    "peak",         # hold/raise toward peak energy
    "breakdown",    # drop energy for a breather / breakdown
    "loop_roll",    # extend the current phrase with a loop before moving on
    "coast",        # maintain energy, smooth blends
    "reset",        # bring energy down to re-build
)


@dataclass
class ArrangementIntent:
    """One planner-level step toward a musical goal.

    Fields:
      step:          position in the rolling sequence (0 = up next).
      goal:          the musical objective for this step (ARRANGEMENT_GOALS or
                     free text).
      track_path:    target track for this step (relative library path), or
                     None when the step operates on the *current* track (e.g. a
                     loop roll before the next track loads).
      track_title:   display title (best-effort).
      technique:     transition technique hint (ARRANGEMENT_TECHNIQUES).
      energy_target: 1-10 target energy at the END of this step.
      bars:          duration of this step in BARS (the unit deadmau5 authors
                     in). Maps to seconds at execution via the deck's bar
                     length (db: beatgrid_bar_seconds, or 4*60/bpm).
      loop_cue:      optional loop-cue descriptor exploited this step, e.g.
                     {"start_seconds": 120.0, "length_beats": 16, "color": ...}.
      use_grid_start: PHANTOM CUE — start playback from the beatgrid downbeat
                      (bar 1) regardless of numbered cues. Always-available
                      action; lets Treta cleanly enter a track from "the one".
      reason:        one-line rationale (surfaced to the DJ agent / logs).
    """
    step: int
    goal: str
    track_path: Optional[str] = None
    track_title: str = ""
    technique: str = "blend"
    energy_target: int = 5
    bars: int = 16
    loop_cue: Optional[dict] = None
    use_grid_start: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def estimated_seconds(self, bpm: float) -> float:
        """Bars → seconds for this step given a deck BPM (4/4 assumed)."""
        if not bpm:
            return 0.0
        return self.bars * (60.0 / bpm) * 4.0


@dataclass
class ArrangementPlan:
    """A short rolling sequence of ArrangementIntents toward `goal`.

    This is the object the planner emits ALONGSIDE the existing PlaylistV1. The
    playlist answers "what track next"; the arrangement answers "what musical
    shape over the next few phrases, and how do we get there".
    """
    goal: str
    intents: list[ArrangementIntent] = field(default_factory=list)
    created_at: float = 0.0
    horizon_bars: int = 0  # total bars covered by the sequence

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "created_at": self.created_at,
            "horizon_bars": self.horizon_bars,
            "intents": [i.to_dict() for i in self.intents],
        }


def loop_cues_from_track(track_meta: dict) -> list[dict]:
    """Extract LOOP cues from a track's `cue_points` JSON (E3 import) so the
    planner can reason about loop material ("16-bar vocal loop").

    Returns a list of {start_seconds, length_beats, color, name}. Robust to a
    missing/None/garbled cue_points field.
    """
    import json
    raw = (track_meta or {}).get("cue_points")
    if not raw:
        return []
    try:
        cues = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    out = []
    for c in cues or []:
        if c.get("is_loop"):
            out.append({
                "start_seconds": c.get("start_seconds"),
                "length_beats": c.get("loop_length_beats"),
                "color": c.get("color"),
                "name": c.get("name") or "",
            })
    return out


def phantom_grid_cue(track_meta: dict) -> dict:
    """The always-available "play from grid start" phantom cue.

    deadmau5 can always drop a track from bar 1 of its beatgrid even with no
    numbered cue there. We expose the same: returns the downbeat anchor so the
    DJ agent can enter a track cleanly on "the one".
    """
    anchor = (track_meta or {}).get("beatgrid_anchor_seconds")
    return {
        "start_seconds": anchor if anchor is not None else 0.0,
        "is_phantom": True,
        "name": "grid-start",
    }
