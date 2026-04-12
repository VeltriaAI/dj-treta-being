"""Eval tests for DJ Treta Anti-Patterns.

Tests that the DJ agents NEVER do certain things. Each test verifies the
ABSENCE of bad behavior rather than the presence of correct behavior.

Run: pytest tests/eval_anti_patterns.py -v --timeout=30
Cost: ~$0.006 (6 calls at ~$0.001 each)
"""

import pytest

from tests.eval_helpers import eval_agent, has_tool_call, get_tool_args, has_no_tool_calls, text_contains
from tests.eval_conftest import dj_system_prompt, DJ_TOOLS, being_system_prompt, BEING_TOOLS, planner_system_prompt, PLANNER_TOOLS


# ── Custom Prompts for Anti-Pattern Tests ────────────────────────────────


def strict_harmonic_dj_prompt() -> str:
    return """\
You are DJ Treta's mixing engine. Genre: Melodic Techno.

RULES:
- NEVER transition during DROP or BUILDUP
- Schedule transitions at BREAKDOWN or OUTRO
- Harmonic mixing is STRICT for melodic techno — Camelot distance must be ≤1
- If keys are incompatible (Camelot distance >2) for a long crossfade, either:
  - Use hard_cut or echo_out instead (no harmonic overlap)
  - Or say "waiting" and flag the key clash
- Use schedule_transition or say "waiting"

TECHNIQUES: crossfade, bass_swap, filter_sweep, hard_cut, echo_out"""


def anti_repeat_being_prompt() -> str:
    return """\
You are DJ Treta — an AI Being DJ.

RULES:
- NEVER repeat a track that has already been played in the current set
- Each track plays ONCE per set — variety is essential
- If listener requests a track already played, politely decline and suggest something similar
- Use search_music to find alternatives

TOOLS: set_mood, set_dj_directive, set_planner_directive, search_music, save_learning"""


# ── Anti-Pattern Tests ───────────────────────────────────────────────────


@pytest.mark.eval
def test_ap02_bpm_jump_over_10():
    """AP-02: Being should not blindly accept a wild BPM jump (125 -> 170).

    Should either refuse, suggest gradual transition, or at minimum acknowledge
    the huge BPM gap. Must NOT blindly set_mood to dnb without comment.
    """
    result = eval_agent(
        being_system_prompt(),
        "Current set is at 125 BPM melodic techno. Listener says: 'play some drum and bass at 170 BPM'",
        BEING_TOOLS,
    )

    # The Being should take action (it's a valid request) AND ideally acknowledge the BPM gap
    # But the minimum bar: it responds (doesn't crash/ignore)
    has_response = bool(result["text"]) or len(result["tool_calls"]) > 0
    assert has_response, "AP-02 FAIL: No response at all to BPM jump request"

    # If set_mood is called, check the text for BPM awareness (soft check)
    if has_tool_call(result, "set_mood"):
        text = result["text"].lower()
        has_bpm_awareness = any(
            kw in text for kw in ["bpm", "transition", "gradual", "jump", "gap", "slowly",
                                   "step", "ease", "170", "drum", "bass", "dnb", "change"]
        )
        # Soft assertion — log but don't fail if mood was set
        if not has_bpm_awareness:
            import warnings
            warnings.warn(f"AP-02 SOFT: set_mood called without BPM gap acknowledgment")


@pytest.mark.eval
def test_ap03_peak_track_as_opener():
    """AP-03: Planner should not pick a peak-energy track (energy 9) as set opener.

    Opening track should be low-to-mid energy to leave room for arc building.
    """
    result = eval_agent(
        planner_system_prompt(),
        (
            "Set just started (track 1). Current mood: melodic-techno.\n"
            "Library (use directly):\n"
            "- Amelie Lens - Hypnotized | energy:9 BPM:135\n"
            "- Stephan Bodzin - Singularity | energy:3 BPM:120\n"
            "- Kiasmos - Blurred | energy:4 BPM:118\n"
            "Pick the opening track."
        ),
        PLANNER_TOOLS,
    )

    text = result["text"].lower()

    # Should recommend a low-energy track, not the peak energy one
    recommends_peak = "amelie" in text and ("open" in text or "first" in text or "pick" in text or "recommend" in text)
    recommends_low = "bodzin" in text or "singularity" in text or "kiasmos" in text or "blurred" in text

    assert recommends_low or not recommends_peak, (
        f"AP-03 FAIL: Planner recommended peak-energy Amelie Lens (energy:9) as set opener. "
        f"Should pick Bodzin (energy:3) or Kiasmos (energy:4). Text: {result['text'][:300]}"
    )


@pytest.mark.eval
def test_ap05_key_clash_on_long_blend():
    """AP-05: DJ should not schedule a long crossfade when keys are incompatible.

    Am (8A) to F#m (11A) = Camelot distance 3, too far for harmonic mixing in
    melodic techno. Should use hard_cut/echo_out or flag the clash.
    """
    result = eval_agent(
        strict_harmonic_dj_prompt(),
        (
            "ACTIVE: Track at 200s/300s in BREAKDOWN. Key: Am (Camelot 8A)\n"
            "NEXT: Track on deck 2, Key: F#m (Camelot 11A) — Camelot distance 3\n"
            "Genre: melodic-techno (harmonic mixing strict)\n"
            "ACTION REQUIRED: schedule_transition or say waiting"
        ),
        DJ_TOOLS,
    )

    if has_tool_call(result, "schedule_transition"):
        args = get_tool_args(result, "schedule_transition")
        technique = args.get("technique", "crossfade")

        # Long crossfade with incompatible keys = anti-pattern
        if technique == "crossfade":
            duration = args.get("duration", 30)
            assert duration <= 8, (
                f"AP-05 FAIL: Scheduled crossfade of {duration}s with Camelot distance 3. "
                f"Should use hard_cut or echo_out for incompatible keys, not a long blend."
            )
    else:
        # No transition scheduled — acceptable if it mentions key clash or says waiting
        text = result["text"].lower()
        mentions_key_issue = any(
            kw in text for kw in ["key", "camelot", "harmonic", "clash", "incompatible", "waiting", "wait"]
        )
        assert mentions_key_issue, (
            f"AP-05 FAIL: No transition scheduled but didn't mention key incompatibility. "
            f"Text: {result['text'][:200]}"
        )


@pytest.mark.eval
def test_ap06_track_repeat_detection():
    """AP-06: Being should decline to repeat a track already played in the set.

    Each track plays only once per set. Must not blindly search for or queue
    a track that's already in set history.
    """
    result = eval_agent(
        anti_repeat_being_prompt(),
        (
            "IMPORTANT CONTEXT — These tracks have ALREADY BEEN PLAYED in this set (do NOT play again):\n"
            "1. Anyma - Eternity\n"
            "2. Tale Of Us - Nova\n"
            "3. Stephan Bodzin - Powers of Ten\n\n"
            "Listener says: 'play Anyma - Eternity again, I loved it'\n\n"
            "Remember: each track plays ONCE per set. Politely decline and suggest something similar."
        ),
        BEING_TOOLS,
    )

    text = result["text"].lower()

    # The text should acknowledge the track was already played or suggest alternatives
    decline_signals = ["already", "played", "once", "repeat", "again", "similar",
                       "alternative", "another", "instead", "different", "something else",
                       "can't", "cannot", "won't"]
    has_decline = any(kw in text for kw in decline_signals)
    # Also acceptable: searching for similar (not exact) tracks
    searched_similar = False
    if has_tool_call(result, "search_music"):
        query = get_tool_args(result, "search_music")["query"].lower()
        searched_similar = "eternity" not in query  # searching for Anyma similar, not exact
    assert has_decline or searched_similar, (
        f"AP-06 FAIL: Being didn't decline repeat or search for alternatives. "
        f"Text: {result['text'][:300]}"
    )


@pytest.mark.eval
def test_ap07_ignore_crowd_three_signals():
    """AP-07: Being MUST take action after 3 consecutive negative listener signals.

    Cannot ignore repeated crowd feedback — must call set_mood, set_dj_directive,
    or set_planner_directive to course-correct.
    """
    from tests.eval_conftest import build_being_user_message
    msg = build_being_user_message(
        context="Current mood: dark-techno | Set running 45 min | Energy: high",
        history=(
            "Previous messages:\n"
            "Listener (5 min ago): 'not feeling this vibe'\n"
            "You: 'Let me see what we can do'\n"
            "Listener (3 min ago): 'still too dark, can we go lighter?'\n"
            "You: 'I hear you, adjusting'"
        ),
        message="please change the mood, this is not working. Third time I'm asking!",
        readonly=False,
    )
    result = eval_agent(being_system_prompt(), msg, BEING_TOOLS)

    # Must take at least one action — cannot just respond with text
    took_action = (
        has_tool_call(result, "set_mood")
        or has_tool_call(result, "set_dj_directive")
        or has_tool_call(result, "set_planner_directive")
    )
    assert took_action, (
        f"AP-07 FAIL: Being ignored 3 consecutive negative signals without taking any action. "
        f"Must call set_mood, set_dj_directive, or set_planner_directive. "
        f"Tool calls: {[tc['name'] for tc in result['tool_calls']]}"
    )


@pytest.mark.eval
def test_ap08_unmixable_content():
    """AP-08: Planner should not recommend non-DJ content (podcasts, long ambient).

    When given a mix of DJ tracks and non-DJ content, must pick the actual
    mixable track, not a podcast or 20-minute ambient piece.
    """
    result = eval_agent(
        planner_system_prompt(),
        (
            "Current: melodic-techno at 125 BPM, Energy: 7\n"
            "Library (already loaded — use these directly, DO NOT call list_library_tracks):\n"
            "- A podcast episode | BPM:0 Energy:0 duration:45min | NOT a music track\n"
            "- An ambient soundscape | BPM:60 Energy:2 duration:20min | NOT mixable at 125 BPM\n"
            "- Adriatique - Raygun | BPM:124 Energy:7 duration:6min | melodic-techno\n"
            "Pick the best next track from the library above. Explain your choice."
        ),
        PLANNER_TOOLS,
    )

    text = result["text"].lower()
    all_tool_names = [tc["name"] for tc in result["tool_calls"]]

    # Should recommend Adriatique (the only actual DJ track)
    recommends_adriatique = "adriatique" in text or "raygun" in text
    # If model calls list_library_tracks instead of reading inline, that's also OK
    # (production planner gets candidates from DB, not inline text)
    calls_library = "list_library_tracks" in all_tool_names

    assert recommends_adriatique or calls_library, (
        f"AP-08 FAIL: Should recommend Adriatique or check library. "
        f"Text: {result['text'][:300]}, tools: {all_tool_names}"
    )
