"""Scenario loader — reads tracks.yaml + transitions.yaml, resolves track
references, and renders each Scenario into the production DJ user message.

Scenarios are loaded once at pytest collection time; any validation error
aborts the suite (fail-loud).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from tests.fixtures.schema import (
    Scenario,
    Track,
    load_scenarios_yaml,
    load_tracks_yaml,
)

FIXTURE_DIR = Path(__file__).parent
TRACKS_PATH = FIXTURE_DIR / "tracks.yaml"
SCENARIOS_PATH = FIXTURE_DIR / "transitions.yaml"


@lru_cache(maxsize=1)
def _load_all() -> tuple[dict[str, Track], dict[str, Scenario]]:
    """Load and validate tracks + scenarios. Cached per process."""
    tracks = load_tracks_yaml(TRACKS_PATH)
    scenarios = load_scenarios_yaml(SCENARIOS_PATH, tracks)
    return tracks, scenarios


def load_tracks() -> dict[str, Track]:
    return _load_all()[0]


def load_scenarios() -> dict[str, Scenario]:
    return _load_all()[1]


def get_scenario(scenario_id: str) -> Scenario:
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        raise KeyError(f"unknown scenario: {scenario_id}")
    return scenarios[scenario_id]


def get_track(track_id: str) -> Track:
    tracks = load_tracks()
    if track_id not in tracks:
        raise KeyError(f"unknown track: {track_id}")
    return tracks[track_id]


def _format_timeline(track: Track) -> str:
    """Render a track's timeline like the production DJ sees it."""
    parts = [
        f"{e.start:.0f}s-{e.end:.0f}s {e.section}(energy:{e.energy})"
        for e in track.timeline
    ]
    return " → ".join(parts)


def _section_label(track: Track, position: float) -> str:
    entry = track.section_at(position)
    if entry is None:
        return f"past end (position {position:.0f}s)"
    return f"{entry.section} (energy:{entry.energy}, {entry.start:.0f}s-{entry.end:.0f}s)"


def scenario_to_dj_message(sc: Scenario) -> str:
    """Render a scenario into the exact text the production DJ would see
    via `build_dj_user_message`. Reuses that function so prompt drift is
    impossible — eval hits the same code path as live daemon."""
    from agent.prompts import build_dj_user_message

    tracks = load_tracks()
    active = tracks[sc.active_track]
    idle = tracks[sc.idle_track]

    # Match production build_dj_user_message args. Idle's remaining time
    # from its own duration (simulating a freshly-loaded idle deck).
    # Special handling: the "idle empty" scenario sets context_note but
    # we still pass the idle track's data — the context_note carries the
    # is-empty signal to the LLM.

    active_section = _section_label(active, sc.active_position_s)
    active_timeline = _format_timeline(active)
    idle_timeline = _format_timeline(idle)
    remaining = max(0.0, active.duration_seconds - sc.active_position_s)

    # Scenario overrides the production signals
    idle_track_name = idle.canonical_song if "idle deck is EMPTY" not in sc.context_note else ""
    idle_bpm = idle.bpm if idle_track_name else 0
    idle_key = idle.key_camelot if idle_track_name else ""

    msg = build_dj_user_message(
        active_track=f"{active.canonical_artist} - {active.canonical_song}",
        position=sc.active_position_s,
        duration=active.duration_seconds,
        remaining=remaining,
        active_bpm=active.bpm,
        active_file_bpm=active.bpm,
        active_key=active.key_camelot,
        active_section=active_section,
        active_timeline=active_timeline,
        idle_track=idle_track_name,
        idle_deck=2,
        idle_bpm=idle_bpm,
        idle_file_bpm=idle_bpm,
        idle_key=idle_key,
        idle_timeline=idle_timeline if idle_track_name else "(EMPTY — no track loaded)",
        transition_pending=sc.pending,
        dj_directive=sc.directive or "",
    )

    # Surface the scenario context_note at the top so the LLM has the
    # narrative framing (e.g. "idle is empty", "both techno peak energy").
    if sc.context_note:
        msg = f"SCENARIO CONTEXT: {sc.context_note}\n\n" + msg
    return msg


def scenarios_by_category(prefix: str = "") -> list[Scenario]:
    """Filter scenarios by category prefix ('positive', 'negative',
    'timing', 'edge', 'positive_bass_swap', etc.)."""
    return [
        sc for sc in load_scenarios().values()
        if sc.category.startswith(prefix)
    ]
