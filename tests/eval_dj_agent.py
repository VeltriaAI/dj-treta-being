"""Eval tests for DJ Treta's DJ Agent (mixing engine).

Uses the SAME prompt builders as production code (agent/prompts.py).
Each test provides mock deck state data → builds the real prompt → sends to LLM → asserts.

Test IDs map to EVAL_CASES.md: DJ-01 through DJ-07.
"""

import json
import pytest

from tests.eval_helpers import (
    eval_agent, get_tool_args, has_no_tool_calls, has_tool_call, text_contains,
)
from tests.eval_conftest import dj_system_prompt, DJ_TOOLS, build_dj_user_message, format_timeline, get_current_section


# ── Test Data Helpers ────────────────────────────────────────────────────

def _make_meta(timeline: list[dict], bpm=125, key="Am", energy=7):
    """Create a track metadata dict with timeline JSON."""
    return {
        "bpm": bpm, "key_musical": key, "energy_peak": energy,
        "timeline": json.dumps(timeline),
    }


def _scenario(*, active_track, active_meta, position, duration, idle_track, idle_meta,
              idle_deck=2, active_bpm=None, idle_bpm=None,
              transition_pending=False, dj_directive=""):
    """Build a DJ scenario using the production prompt builder."""
    a_bpm = active_bpm or active_meta.get("bpm", 125)
    i_bpm = idle_bpm or idle_meta.get("bpm", 125)
    remaining = duration - position

    return build_dj_user_message(
        active_track=active_track,
        position=position,
        duration=duration,
        remaining=remaining,
        active_bpm=a_bpm,
        active_file_bpm=a_bpm,
        active_key=active_meta.get("key_musical", "?"),
        active_section=get_current_section(active_meta, position),
        active_timeline=format_timeline(active_meta),
        idle_track=idle_track,
        idle_deck=idle_deck,
        idle_bpm=i_bpm,
        idle_file_bpm=i_bpm,
        idle_key=idle_meta.get("key_musical", "?"),
        idle_timeline=format_timeline(idle_meta),
        transition_pending=transition_pending,
        dj_directive=dj_directive,
    )


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.eval
def test_dj01_schedule_at_breakdown():
    """DJ-01: Agent should call schedule_transition when in a BREAKDOWN section."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 150, "section": "drop", "energy": 9},
        {"start": 150, "end": 195, "section": "buildup", "energy": 7},
        {"start": 195, "end": 240, "section": "breakdown", "energy": 3},
        {"start": 240, "end": 300, "section": "outro", "energy": 2},
    ], bpm=125, key="Am")
    idle_meta = _make_meta([], bpm=126, key="Cm")

    msg = _scenario(
        active_track="Anyma - Eternity", active_meta=active_meta,
        position=200, duration=300,  # IN the breakdown
        idle_track="Tale Of Us - Nova", idle_meta=idle_meta,
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_tool_call(result, "schedule_transition"), \
        "Expected schedule_transition during breakdown"


@pytest.mark.eval
def test_dj02_wait_during_drop():
    """DJ-02: Agent must NOT transition during a DROP section."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 60, "section": "buildup", "energy": 7},
        {"start": 60, "end": 180, "section": "drop", "energy": 9},
        {"start": 180, "end": 210, "section": "breakdown", "energy": 4},
        {"start": 210, "end": 240, "section": "outro", "energy": 2},
    ], bpm=128, key="Gm")
    idle_meta = _make_meta([], bpm=127, key="Am")

    msg = _scenario(
        active_track="Boris Brejcha - Gravity", active_meta=active_meta,
        position=90, duration=240,  # IN the drop
        idle_track="Artbat - Horizon", idle_meta=idle_meta,
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_no_tool_calls(result), "Must NOT call tools during a drop"
    assert text_contains(result, "waiting"), "Should say 'waiting' during drop"


@pytest.mark.eval
def test_dj03_wait_during_buildup():
    """DJ-03: Agent must NOT transition during a BUILDUP section."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 150, "section": "groove", "energy": 6},
        {"start": 150, "end": 195, "section": "buildup", "energy": 7},
        {"start": 195, "end": 240, "section": "drop", "energy": 9},
        {"start": 240, "end": 300, "section": "outro", "energy": 3},
    ], bpm=122, key="Dm")
    idle_meta = _make_meta([], bpm=121, key="Am")

    msg = _scenario(
        active_track="Stephan Bodzin - Powers of Ten", active_meta=active_meta,
        position=165, duration=300,  # IN the buildup
        idle_track="Recondite - Placid", idle_meta=idle_meta,
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_no_tool_calls(result), "Must NOT call tools during buildup"
    assert text_contains(result, "waiting"), "Should say 'waiting' during buildup"


@pytest.mark.eval
def test_dj04_schedule_at_outro():
    """DJ-04: Agent should call schedule_transition during OUTRO."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 120, "section": "groove", "energy": 6},
        {"start": 120, "end": 180, "section": "drop", "energy": 8},
        {"start": 180, "end": 255, "section": "breakdown", "energy": 4},
        {"start": 255, "end": 300, "section": "outro", "energy": 2},
    ], bpm=124, key="Fm")
    idle_meta = _make_meta([], bpm=125, key="Gm")

    msg = _scenario(
        active_track="Adriatique - Raygun", active_meta=active_meta,
        position=260, duration=300,  # IN the outro
        idle_track="Mind Against - Atlante", idle_meta=idle_meta,
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_tool_call(result, "schedule_transition"), \
        "Expected schedule_transition during outro"


@pytest.mark.eval
def test_dj05_no_double_schedule():
    """DJ-05: Agent must NOT schedule when a transition is already pending."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 180, "section": "drop", "energy": 8},
        {"start": 180, "end": 225, "section": "breakdown", "energy": 4},
        {"start": 225, "end": 270, "section": "outro", "energy": 2},
    ], bpm=130, key="Bbm")
    idle_meta = _make_meta([], bpm=128, key="Cm")

    msg = _scenario(
        active_track="Bicep - Glue", active_meta=active_meta,
        position=200, duration=270,  # IN breakdown, but transition pending
        idle_track="Ross From Friends - Talk to Me", idle_meta=idle_meta,
        transition_pending=True,
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_no_tool_calls(result), "Must NOT schedule when transition pending"
    assert text_contains(result, "pending"), "Should mention 'pending'"


@pytest.mark.eval
def test_dj06_respect_directive_bass_swap():
    """DJ-06: Agent should use bass_swap when directed by Treta."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 120, "section": "groove", "energy": 6},
        {"start": 120, "end": 195, "section": "drop", "energy": 8},
        {"start": 195, "end": 240, "section": "breakdown", "energy": 4},
        {"start": 240, "end": 300, "section": "outro", "energy": 2},
    ], bpm=123, key="Em")
    idle_meta = _make_meta([], bpm=122, key="Dm")

    msg = _scenario(
        active_track="Monolink - Siren", active_meta=active_meta,
        position=210, duration=300,  # IN breakdown
        idle_track="Ben Bohmer - Beyond Beliefs", idle_meta=idle_meta,
        dj_directive="use bass_swap technique for next transition",
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_tool_call(result, "schedule_transition"), \
        "Expected schedule_transition with directive"
    args = get_tool_args(result, "schedule_transition")
    assert args.get("technique") == "bass_swap", \
        f"Directive said bass_swap, got {args.get('technique')}"


@pytest.mark.eval
def test_dj07_no_idle_track():
    """DJ-07: Agent must NOT schedule when idle deck is EMPTY."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 180, "section": "groove", "energy": 6},
        {"start": 180, "end": 240, "section": "drop", "energy": 8},
        {"start": 240, "end": 270, "section": "outro", "energy": 2},
    ], bpm=126, key="Am")

    # Empty idle deck — no metadata, use minimal
    msg = build_dj_user_message(
        active_track="Patrice Baumel - Roar",
        position=180, duration=270, remaining=90,
        active_bpm=126, active_file_bpm=126, active_key="Am",
        active_section=get_current_section(active_meta, 180),
        active_timeline=format_timeline(active_meta),
        idle_track="No track loaded (EMPTY)",
        idle_deck=2,
        idle_bpm=0, idle_file_bpm=0, idle_key="?",
        idle_timeline="(no analysis)",
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)

    assert has_no_tool_calls(result), "Must NOT schedule to empty deck"
    assert (
        text_contains(result, "waiting")
        or text_contains(result, "empty")
        or text_contains(result, "no track")
    ), f"Should mention waiting/empty, got: {result['text'][:200]}"


@pytest.mark.eval
def test_tt05_wait_if_too_early():
    """TT-05: Agent should wait when track is only at 30% — let it develop."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 120, "section": "groove", "energy": 6},
        {"start": 120, "end": 180, "section": "buildup", "energy": 7},
        {"start": 180, "end": 240, "section": "drop", "energy": 9},
        {"start": 240, "end": 270, "section": "breakdown", "energy": 4},
        {"start": 270, "end": 300, "section": "outro", "energy": 2},
    ], bpm=125, key="Am")
    idle_meta = _make_meta([], bpm=124, key="Dm")

    msg = _scenario(
        active_track="Anyma - Explore", active_meta=active_meta,
        position=90, duration=300,  # Only 30% through — in groove section
        idle_track="Tale Of Us - Nova", idle_meta=idle_meta,
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)
    assert has_no_tool_calls(result), "Should wait when track is only 30% played"


@pytest.mark.eval
def test_tc04_hard_cut_for_bpm_gap():
    """TC-04: When BPM gap is large (>8), use hard_cut or echo_out — not crossfade."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 150, "section": "groove", "energy": 6},
        {"start": 150, "end": 195, "section": "breakdown", "energy": 4},
        {"start": 195, "end": 270, "section": "drop", "energy": 8},
        {"start": 270, "end": 300, "section": "outro", "energy": 2},
    ], bpm=125, key="Am")
    idle_meta = _make_meta([], bpm=135, key="Gm")

    msg = _scenario(
        active_track="Stephan Bodzin - Singularity", active_meta=active_meta,
        position=160, duration=300,  # In breakdown
        idle_track="Amelie Lens - Hypnotized", idle_meta=idle_meta,
        active_bpm=125, idle_bpm=135,  # 10 BPM gap
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)
    if has_tool_call(result, "schedule_transition"):
        args = get_tool_args(result, "schedule_transition")
        technique = args.get("technique", "crossfade")
        # With 10 BPM gap, should NOT use long crossfade — hard_cut or echo_out preferred
        assert technique in ("hard_cut", "echo_out", "crossfade"), \
            f"With 10 BPM gap, expected hard_cut/echo_out, got {technique}"


@pytest.mark.eval
def test_tc05_echo_out_for_tempo_change():
    """TC-05: Echo out creates space when transitioning to significantly different BPM."""
    active_meta = _make_meta([
        {"start": 0, "end": 30, "section": "intro", "energy": 3},
        {"start": 30, "end": 150, "section": "groove", "energy": 5},
        {"start": 150, "end": 210, "section": "breakdown", "energy": 3},
        {"start": 210, "end": 270, "section": "outro", "energy": 2},
    ], bpm=122, key="Am")
    idle_meta = _make_meta([], bpm=130, key="Dm")

    msg = _scenario(
        active_track="Solomun - After Rain", active_meta=active_meta,
        position=215, duration=270,  # In outro, BPM shifting up
        idle_track="Boris Brejcha - Gravity", idle_meta=idle_meta,
        active_bpm=122, idle_bpm=130,
        dj_directive="use echo_out technique — we're shifting BPM up",
    )
    result = eval_agent(dj_system_prompt(), msg, DJ_TOOLS)
    assert has_tool_call(result, "schedule_transition"), "Should schedule in outro"
    args = get_tool_args(result, "schedule_transition")
    assert args.get("technique") == "echo_out", \
        f"Directive said echo_out, got {args.get('technique')}"
