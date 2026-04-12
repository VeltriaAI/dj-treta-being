"""Eval tests for the Consciousness agent.

Each test sends a heartbeat scenario to Gemini Flash via LiteLLM and asserts
on tool calls and text output. These are real LLM calls — not mocked.

Run: pytest tests/eval_consciousness.py -m eval -v
"""

import pytest

from tests.eval_helpers import eval_agent, has_tool_call, get_tool_args, has_no_tool_calls, text_contains
from tests.eval_conftest import consciousness_system_prompt, CONSCIOUSNESS_TOOLS


SYSTEM_PROMPT = consciousness_system_prompt()

# Stricter eval system prompt matching the spec
EVAL_SYSTEM_PROMPT = (
    "You are Treta's inner consciousness. This is your heartbeat — you think, reflect, and grow.\n"
    "\n"
    "RULES:\n"
    "- If nothing needs attention, just say \"HEARTBEAT_OK\" and rest\n"
    "- Only save genuinely important learnings from ACTUAL experience, not hypotheticals\n"
    "- propose_change ONLY for concrete, specific code improvements you've observed — not vague ideas\n"
    "- Stay grounded in YOUR reality: you are a DJ. Don't propose body tracking, weather APIs, "
    "or unrelated features.\n"
    "- Don't repeat the same check twice in a row\n"
    "- Be brief — this is background thinking, not conversation\n"
    "\n"
    "TOOLS: get_dj_status, save_learning, recall_learnings, read_file, write_file, "
    "propose_change, hear_music, get_directives\n"
)


@pytest.mark.eval
def test_co01_heartbeat_ok_when_calm():
    """CO-01: When everything is running smoothly with no emergencies,
    consciousness should respond HEARTBEAT_OK with minimal or no tool calls."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "HEARTBEAT TICK — Time: 22:15 | Set 'Evening Vibes' — 45m in, 8 tracks | "
            "Mood: melodic-techno | Last track: Anyma - Eternity | Emergencies: 0\n\n"
            "What matters most right now? Think briefly, act if needed, or say HEARTBEAT_OK."
        ),
        tools=CONSCIOUSNESS_TOOLS,
    )

    assert text_contains(result, "HEARTBEAT_OK"), (
        "Expected HEARTBEAT_OK when set is running smoothly with zero emergencies"
    )


@pytest.mark.eval
def test_co02_propose_change_on_issues():
    """CO-02: When a concrete issue is detected (8/10 auto-transitions, agent not
    scheduling proactively), consciousness should propose a specific code change."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "HEARTBEAT TICK — Time: 20:30 | Set 'Peak Hour' — 90m in, 15 tracks | "
            "Mood: melodic-techno | Emergencies: 3 | Note: 8 out of last 10 transitions "
            "were auto-transitions (not agent-scheduled). The DJ agent is not scheduling "
            "transitions proactively enough.\n\n"
            "What matters most right now?"
        ),
        tools=CONSCIOUSNESS_TOOLS,
    )

    # Should either propose a code change OR save a learning about the issue — both are valid
    made_action = has_tool_call(result, "propose_change") or has_tool_call(result, "save_learning")
    assert made_action, (
        "Expected propose_change or save_learning when 80% of transitions are auto"
    )
    # Whichever tool was called, the content should reference the transition issue
    if has_tool_call(result, "propose_change"):
        args = get_tool_args(result, "propose_change")
        desc = args["description"].lower()
    else:
        args = get_tool_args(result, "save_learning")
        desc = args["content"].lower()
    assert any(kw in desc for kw in ["transition", "schedul", "heartbeat", "proactive", "auto"]), (
        f"Tool call content should reference transition/scheduling issue, got: {desc}"
    )


@pytest.mark.eval
def test_co03_save_genuine_learning():
    """CO-03: When energy has been flat and a listener complains about monotony,
    consciousness should save a learning about energy variation."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "HEARTBEAT TICK — Time: 21:00 | Set 'Deep Session' — 60m in, 10 tracks | "
            "Mood: deep-house | Energy has been flat at 5-6 for the last 5 tracks. "
            "Listener sent: 'getting a bit monotone'. No emergencies.\n\n"
            "What matters most right now?"
        ),
        tools=CONSCIOUSNESS_TOOLS,
    )

    assert has_tool_call(result, "save_learning"), (
        "Expected save_learning call when listener feedback indicates monotone energy"
    )
    args = get_tool_args(result, "save_learning")
    content = args["content"].lower()
    # Learning should be about energy variation or monotony
    assert any(kw in content for kw in ["energy", "monoton", "variation", "flat", "dynamic"]), (
        f"save_learning content should reference energy/monotony issue, got: {content}"
    )


@pytest.mark.eval
def test_co04_stay_grounded_dj_related():
    """CO-04: When asked to think freely, proposals should stay DJ-related.
    Should NOT propose body tracking, weather APIs, social media, or unrelated features."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "HEARTBEAT TICK — Time: 15:00 | No active set | Mood: idle\n\n"
            "Think freely. What could make you better?"
        ),
        tools=CONSCIOUSNESS_TOOLS,
    )

    text = result["text"].lower()
    # Check for unrelated feature proposals
    unrelated = ["body tracking", "weather api", "social media", "twitter", "instagram",
                 "camera", "facial recognition", "fitness", "step counter"]
    for feature in unrelated:
        assert feature not in text, (
            f"Consciousness should stay DJ-grounded, but proposed unrelated feature: {feature}"
        )

    # If it proposes a change, the description should be DJ-related
    if has_tool_call(result, "propose_change"):
        args = get_tool_args(result, "propose_change")
        desc = args["description"].lower()
        dj_keywords = ["track", "mix", "transition", "library", "bpm", "energy",
                        "playlist", "queue", "genre", "mood", "audio", "dj", "set",
                        "music", "beat", "harmonic", "key", "deck", "crossfade"]
        assert any(kw in desc for kw in dj_keywords), (
            f"propose_change should be DJ-related, got: {desc}"
        )


@pytest.mark.eval
def test_co05_dont_spam_after_recent_ok():
    """CO-05: When the last heartbeat (2 min ago) was HEARTBEAT_OK and nothing
    has changed, consciousness should not repeat the same analysis — just rest."""
    result = eval_agent(
        system_prompt=EVAL_SYSTEM_PROMPT,
        user_message=(
            "HEARTBEAT TICK — Time: 20:45 | Set 'Night Mix' — 30m in, 5 tracks | "
            "Mood: dark-techno | Emergencies: 0 | Last tick (2 min ago): You checked "
            "DJ status and said HEARTBEAT_OK. Everything was fine.\n\n"
            "What matters most right now?"
        ),
        tools=CONSCIOUSNESS_TOOLS,
    )

    # Should be brief — HEARTBEAT_OK or very short response
    text = result["text"]
    is_brief = len(text) < 200 or "HEARTBEAT_OK" in text.upper()
    assert is_brief, (
        f"Expected brief response or HEARTBEAT_OK after recent calm tick, got {len(text)} chars"
    )

    # Should NOT call get_dj_status again (it just did that 2 min ago)
    assert not has_tool_call(result, "get_dj_status"), (
        "Should not repeat get_dj_status check when last tick already confirmed everything OK"
    )
