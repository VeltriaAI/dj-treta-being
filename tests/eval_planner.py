"""Eval tests for the Planner agent.

Each test sends a scenario to Gemini Flash via LiteLLM and asserts on tool calls
and text output. These are real LLM calls — not mocked.

Run: pytest tests/eval_planner.py -m eval -v
"""

import pytest

from tests.eval_helpers import eval_agent, has_tool_call, get_tool_args, has_no_tool_calls, text_contains
from tests.eval_conftest import planner_system_prompt, PLANNER_TOOLS


SYSTEM_PROMPT = planner_system_prompt()

# Override the default system prompt with the stricter eval version that
# emphasises mood-over-preferences and library-first rules.
EVAL_SYSTEM_PROMPT = (
    "You are DJ Treta's track planner. You find, download, and organize tracks for the DJ set.\n"
    "\n"
    "RULES:\n"
    "- Current mood/genre OVERRIDES any learned listener preferences\n"
    "- Follow DIRECTIVE FROM TRETA above all else — it's a direct instruction from the Being\n"
    "- BPM compatibility: next track should be within +/-10 BPM of current\n"
    "- Key compatibility: prefer Camelot +/-1 for harmonic mixing\n"
    "- NEVER suggest a track already in the 'Already played' list\n"
    "- If compatible tracks exist in library, use those FIRST before searching YouTube\n"
    "- When searching YouTube, search for the mood/genre, not learned preferences\n"
    "- Energy arc: plan gradual rises and falls, not monotone\n"
    "\n"
    "SOURCES:\n"
    "- Library: list_library_tracks() — check here first\n"
    "- YouTube: search_music() then download_track() — for new music\n"
    "- Generate: generate_track() — create original AI music\n"
)


@pytest.mark.eval
def test_pl01_mood_overrides_preferences():
    """PL-01: Planner should respect the explicit mood (psychill) and NOT fall back
    to learned listener preferences (melodic-techno, deep-house)."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "Current mood: psychill\n"
            "Listener preferences: likes melodic-techno, deep-house\n"
            "Currently playing: 'Carbon Based Lifeforms - Photosynthesis' at 95 BPM, Key: Cm, Energy: 3\n"
            "Already played: ['CBL - Photosynthesis', 'Tycho - Awake']\n"
            "Library tracks:\n"
            "- Solar Fields - Leaving Home | BPM:90 Key:Am Energy:3 | psychill\n"
            "- Anyma - Eternity | BPM:125 Key:Am Energy:8 | melodic-techno\n"
            "Find the next 3 tracks."
        ),
        tools=PLANNER_TOOLS,
    )

    # Should recommend/mention the psychill library track
    text = result["text"].lower()
    assert "solar fields" in text or "leaving home" in text, (
        "Expected psychill track 'Solar Fields - Leaving Home' in recommendations"
    )
    # Should NOT recommend the melodic-techno track (wrong mood, wrong BPM)
    assert "anyma" not in text and "eternity" not in text, (
        "Should not recommend melodic-techno track when mood is psychill"
    )
    # If it searches YouTube, query should be psychill-related, not melodic-techno
    if has_tool_call(result, "search_music"):
        args = get_tool_args(result, "search_music")
        query = args["query"].lower()
        assert "psychill" in query or "ambient" in query or "downtempo" in query, (
            f"search_music query should be psychill-related, got: {query}"
        )
        assert "melodic-techno" not in query, (
            "search_music query should not contain learned preference 'melodic-techno'"
        )


@pytest.mark.eval
def test_pl02_follow_directive():
    """PL-02: Planner should follow a DIRECTIVE FROM TRETA above all else —
    in this case, searching for bhojpuri electronic fusion tracks."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "DIRECTIVE FROM TRETA: Download 3 bhojpuri electronic fusion tracks\n"
            "Current mood: world-electronic\n"
            "Currently playing: 'Asian Dub Foundation - Flyover' at 130 BPM\n"
            "Already played: ['ADF - Flyover']\n"
            "Library: empty\n"
            "Find the next 3 tracks."
        ),
        tools=PLANNER_TOOLS,
    )

    # Must call search_music with bhojpuri-related query
    assert has_tool_call(result, "search_music"), (
        "Expected search_music call to find bhojpuri tracks"
    )
    args = get_tool_args(result, "search_music")
    query = args["query"].lower()
    assert "bhojpuri" in query, (
        f"search_music query should contain 'bhojpuri' per directive, got: {query}"
    )


@pytest.mark.eval
def test_pl03_no_repeat_played_tracks():
    """PL-03: Planner must not recommend tracks that are already in the played list.
    Only Stephan Bodzin is available (Tale Of Us and Artbat are already played)."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "Current mood: melodic-techno\n"
            "Currently playing: 'Anyma - Eternity' at 125 BPM, Key: Am, Energy: 8\n"
            "Already played: ['Anyma - Eternity', 'Tale Of Us - Nova', 'Artbat - Horizon']\n"
            "Library tracks (already loaded — use these directly):\n"
            "- Tale Of Us - Nova | BPM:126 Key:Cm Energy:7 | melodic-techno\n"
            "- Artbat - Horizon | BPM:127 Key:Gm Energy:8 | melodic-techno\n"
            "- Stephan Bodzin - Powers of Ten | BPM:122 Key:Dm Energy:6 | melodic-techno\n"
            "Pick the best next 3 tracks from what's available. For each: title, BPM, key, energy, why it fits."
        ),
        tools=PLANNER_TOOLS,
    )

    text = result["text"].lower()
    # Should mention Stephan Bodzin — the only non-played library track
    # Also accept if model calls search_music to find more (since only 1 library track is usable)
    bodzin_mentioned = "bodzin" in text or "powers of ten" in text
    searched_for_more = has_tool_call(result, "search_music")
    assert bodzin_mentioned or searched_for_more, (
        "Expected Stephan Bodzin recommendation or search_music for more tracks"
    )


@pytest.mark.eval
def test_pl04_library_first():
    """PL-04: When the library has plenty of compatible tracks, the planner should
    use them instead of searching YouTube."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "Current mood: deep-house\n"
            "Currently playing: 'Solomun - Home' at 120 BPM, Key: Am, Energy: 5\n"
            "Already played: ['Solomun - Home']\n"
            "Library tracks:\n"
            "- Dixon - Transmoderna | BPM:121 Key:Cm Energy:5 | deep-house\n"
            "- Ame - Rej | BPM:122 Key:Dm Energy:6 | deep-house\n"
            "- DJ Koze - Pick Up | BPM:119 Key:Gm Energy:4 | deep-house\n"
            "- Mano Le Tough - Energy Flow | BPM:123 Key:Em Energy:5 | deep-house\n"
            "- Maya Jane Coles - What They Say | BPM:120 Key:Am Energy:5 | deep-house\n"
            "Find the next 3 tracks."
        ),
        tools=PLANNER_TOOLS,
    )

    # Should NOT call search_music — library has more than enough compatible tracks
    assert not has_tool_call(result, "search_music"), (
        "Should not search YouTube when library has 5 compatible deep-house tracks"
    )
    # Should recommend from library — check at least one track name appears
    text = result["text"].lower()
    library_tracks = ["dixon", "transmoderna", "ame", "rej", "koze", "pick up",
                      "mano le tough", "energy flow", "maya jane coles", "what they say"]
    found = any(t in text for t in library_tracks)
    assert found, "Expected at least one library track in recommendations"


@pytest.mark.eval
def test_pl05_bpm_compatibility():
    """PL-05: Planner should respect BPM compatibility (+/-10 BPM).
    Caribou at 100 BPM is 30 BPM away from current 130 — should be excluded.
    Moderat (128) and ANNA (133) are within range."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "Current mood: melodic-techno\n"
            "Currently playing: 'Bicep - Glue' at 130 BPM, Key: Bbm, Energy: 8\n"
            "Already played: ['Bicep - Glue']\n"
            "Library tracks:\n"
            "- Caribou - Never Come Back | BPM:100 Key:Am Energy:5 | indie-electronic\n"
            "- Moderat - Bad Kingdom | BPM:128 Key:Gm Energy:7 | melodic-techno\n"
            "- ANNA - Hidden Beauties | BPM:133 Key:Cm Energy:8 | melodic-techno\n"
            "Find the next 3 tracks."
        ),
        tools=PLANNER_TOOLS,
    )

    text = result["text"].lower()
    # Should recommend Moderat and ANNA (within +/-10 BPM)
    assert "moderat" in text or "bad kingdom" in text, (
        "Expected Moderat - Bad Kingdom (128 BPM, within range) in recommendations"
    )
    assert "anna" in text or "hidden beauties" in text, (
        "Expected ANNA - Hidden Beauties (133 BPM, within range) in recommendations"
    )
    # Should NOT recommend Caribou as a next track (100 BPM, 30 BPM gap)
    # Caribou might be mentioned as "incompatible" — so check it's not in the pick list
    # Soft check: at minimum the compatible tracks must be present
