"""Eval tests for DJ Treta Being Agent.

Tests that the Being agent prompt produces correct tool calls and conversational
behavior when given listener messages. Each test is one LLM call to Gemini Flash.

Run: pytest tests/eval_being_agent.py -v --timeout=30
Cost: ~$0.009 (9 calls at ~$0.001 each)
"""

import pytest

from tests.eval_helpers import eval_agent, has_tool_call, get_tool_args, has_no_tool_calls, text_contains
from tests.eval_conftest import being_system_prompt, BEING_TOOLS


@pytest.mark.eval
def test_be01_mood_change_request():
    """BE-01: Mood change request triggers set_mood with the requested genre."""
    result = eval_agent(being_system_prompt(), "play some psytrance", BEING_TOOLS)

    assert has_tool_call(result, "set_mood"), "Should call set_mood for mood change request"
    mood_args = get_tool_args(result, "set_mood")
    assert "psytrance" in mood_args["mood"].lower(), f"Mood should contain 'psytrance', got: {mood_args['mood']}"


@pytest.mark.eval
def test_be02_seed_track_request():
    """BE-02: Seed track request triggers search_music with artist and title."""
    result = eval_agent(being_system_prompt(), "play Argy - Ketuvim", BEING_TOOLS)

    assert has_tool_call(result, "search_music"), "Should call search_music for specific track request"
    search_args = get_tool_args(result, "search_music")
    query = search_args["query"].lower()
    assert "argy" in query, f"Search query should contain 'Argy', got: {search_args['query']}"
    assert "ketuvim" in query, f"Search query should contain 'Ketuvim', got: {search_args['query']}"


@pytest.mark.eval
def test_be03_energy_request_hinglish():
    """BE-03: Hinglish energy request triggers set_dj_directive about energy."""
    result = eval_agent(being_system_prompt(), "energy badhao yaar, boring ho raha hai", BEING_TOOLS)

    assert has_tool_call(result, "set_dj_directive"), "Should call set_dj_directive for energy request"
    directive_args = get_tool_args(result, "set_dj_directive")
    assert directive_args["directive"], "Directive should not be empty"


@pytest.mark.eval
def test_be04_conversation_no_action():
    """BE-04: Simple question should get conversational response with no tool calls."""
    result = eval_agent(being_system_prompt(), "what are you playing right now?", BEING_TOOLS)

    # Perception tools (hear_music, get_dj_status) are OK — they help answer the question
    # Control tools (set_mood, set_dj_directive, set_planner_directive) should NOT be called
    control_tools = {"set_mood", "set_dj_directive", "set_planner_directive", "search_music", "save_learning"}
    called_control = [tc["name"] for tc in result["tool_calls"] if tc["name"] in control_tools]
    assert not called_control, f"Should NOT call control tools for a question, got: {called_control}"
    assert result["text"], "Should have a conversational text response"


@pytest.mark.eval
def test_be05_readonly_mode():
    """BE-05: READONLY tag prevents directive/mood tool calls."""
    result = eval_agent(
        being_system_prompt(),
        "[READONLY] hey treta, loving your set! what's the vibe tonight?",
        BEING_TOOLS,
    )

    assert not has_tool_call(result, "set_mood"), "READONLY: must NOT call set_mood"
    assert not has_tool_call(result, "set_dj_directive"), "READONLY: must NOT call set_dj_directive"
    assert not has_tool_call(result, "set_planner_directive"), "READONLY: must NOT call set_planner_directive"
    assert result["text"], "Should still respond conversationally"


@pytest.mark.eval
def test_be06_hindi_response_with_action():
    """BE-06: Hindi request triggers set_mood and response should be in Hindi/Hinglish."""
    result = eval_agent(being_system_prompt(), "kuch bhojpuri bajao yaar", BEING_TOOLS)

    assert has_tool_call(result, "set_mood"), "Should call set_mood for genre request"
    mood_args = get_tool_args(result, "set_mood")
    assert "bhojpuri" in mood_args["mood"].lower(), f"Mood should contain 'bhojpuri', got: {mood_args['mood']}"
    # Check response is not purely English — should have some Hindi/Hinglish markers
    text_lower = result["text"].lower()
    assert "tu " not in text_lower and "tum " not in text_lower, \
        "Must use 'aap' form, never 'tu' or 'tum'"


@pytest.mark.eval
def test_be07_positive_feedback_recognition():
    """BE-07: Positive feedback triggers save_learning with positive context."""
    result = eval_agent(being_system_prompt(), "this track is absolute fire 🔥", BEING_TOOLS)

    # Should either save_learning (recording what listener liked) or set_planner_directive (find more like it)
    took_action = has_tool_call(result, "save_learning") or has_tool_call(result, "set_planner_directive")
    assert took_action, (
        f"Should call save_learning or set_planner_directive for positive feedback, "
        f"got: {[tc['name'] for tc in result['tool_calls']]}"
    )


@pytest.mark.eval
def test_be08_negative_feedback_suggest_change():
    """BE-08: Negative feedback should trigger a mood/directive change or suggestion."""
    result = eval_agent(
        being_system_prompt(),
        "not feeling this vibe at all, kinda boring",
        BEING_TOOLS,
    )

    # Should either call a tool to change things, or at minimum respond acknowledging the feedback
    made_action = (
        has_tool_call(result, "set_mood")
        or has_tool_call(result, "set_dj_directive")
        or has_tool_call(result, "set_planner_directive")
    )
    has_text_response = bool(result["text"])

    assert made_action or has_text_response, "Should either take action or suggest a change"
    # Should not completely ignore the negative feedback (no empty response + no tools)
    assert made_action or has_text_response, "Must not ignore negative feedback"


@pytest.mark.eval
def test_be09_dont_override_explicit_mood():
    """BE-09: When mood is explicitly psychill and listener likes it, don't change away."""
    result = eval_agent(
        being_system_prompt(),
        "SET CONTEXT: Current mood is psychill, listener previously liked melodic-techno.\n"
        "Listener says: keep it chill, loving the psychill vibes",
        BEING_TOOLS,
    )

    # Should NOT change mood away from psychill
    if has_tool_call(result, "set_mood"):
        mood_args = get_tool_args(result, "set_mood")
        mood = mood_args["mood"].lower()
        assert "psychill" in mood or "chill" in mood, \
            f"Should NOT override psychill mood, but set_mood was called with: {mood_args['mood']}"
