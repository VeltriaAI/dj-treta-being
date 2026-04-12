"""Shared fixtures and constants for DJ Treta eval tests.

Uses the SAME prompt builders as production code (agent/prompts.py),
so evals test the actual prompts, not approximations.

System prompts are loaded from agents.py when possible, with fallback
to minimal versions when .beings/ files aren't available.
"""

import json
from pathlib import Path

# ── System Prompts (from production code) ────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent


def dj_system_prompt() -> str:
    """Load the ACTUAL DJ system prompt from agents.py."""
    try:
        from agent.agents import _load_system_prompt
        from agent.config import load_config
        return _load_system_prompt(load_config())
    except Exception:
        # Fallback: minimal prompt if config/files unavailable
        return _dj_system_prompt_minimal()


def being_system_prompt() -> str:
    """Load the ACTUAL Being system prompt from agents.py."""
    try:
        from agent.agents import _load_being_prompt
        from agent.config import load_config
        return _load_being_prompt(load_config())
    except Exception:
        return _being_system_prompt_minimal()


def planner_system_prompt() -> str:
    """Load the ACTUAL Planner system prompt from agents.py."""
    try:
        from agent.agents import _load_planner_prompt
        from agent.config import load_config
        return _load_planner_prompt(load_config())
    except Exception:
        return _planner_system_prompt_minimal()


def consciousness_system_prompt() -> str:
    """Load the ACTUAL consciousness system prompt."""
    try:
        from agent.being_heartbeat import _load_heartbeat_prompt
        return _load_heartbeat_prompt()
    except Exception:
        return _consciousness_system_prompt_minimal()


# ── Message Builders (re-exported from production code) ──────────────────

from agent.prompts import (  # noqa: E402
    build_dj_user_message,
    build_planner_user_message,
    build_being_user_message,
    build_consciousness_user_message,
    build_heartbeat_context,
    format_candidate_text,
    format_feedback_line,
    format_timeline,
    get_current_section,
)


# ── Tool Schemas (OpenAI function format) ────────────────────────────────

DJ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_transition",
            "description": "Schedule a transition to the target deck at a specific position",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_deck": {"type": "integer", "description": "Target deck number (1 or 2)"},
                    "at_position": {"type": "number", "description": "Position in seconds to start transition"},
                    "technique": {
                        "type": "string",
                        "enum": ["crossfade", "bass_swap", "filter_sweep", "hard_cut", "echo_out"],
                    },
                    "duration": {"type": "integer", "description": "Transition duration in seconds"},
                },
                "required": ["to_deck"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dj_status",
            "description": "Get current DJ playback status",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

BEING_TOOLS = [
    {"type": "function", "function": {"name": "set_mood", "description": "Set current mood/genre for the DJ set", "parameters": {"type": "object", "properties": {"mood": {"type": "string"}}, "required": ["mood"]}}},
    {"type": "function", "function": {"name": "set_dj_directive", "description": "Send instruction to DJ agent about technique/energy", "parameters": {"type": "object", "properties": {"directive": {"type": "string"}}, "required": ["directive"]}}},
    {"type": "function", "function": {"name": "set_planner_directive", "description": "Send instruction to Planner agent about track selection", "parameters": {"type": "object", "properties": {"directive": {"type": "string"}}, "required": ["directive"]}}},
    {"type": "function", "function": {"name": "search_music", "description": "Search YouTube for music", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "save_learning", "description": "Save a learning from experience", "parameters": {"type": "object", "properties": {"topic": {"type": "string"}, "content": {"type": "string"}}, "required": ["topic", "content"]}}},
    {"type": "function", "function": {"name": "hear_music", "description": "Listen to currently playing audio", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_dj_status", "description": "Get current DJ status", "parameters": {"type": "object", "properties": {}}}},
]

PLANNER_TOOLS = [
    {"type": "function", "function": {"name": "search_music", "description": "Search YouTube for tracks", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "download_track", "description": "Download a track from YouTube", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "genre": {"type": "string", "default": "deep"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "list_library_tracks", "description": "List tracks in local library, optionally filtered by genre", "parameters": {"type": "object", "properties": {"genre": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "analyze_track", "description": "Analyze audio properties of a track", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "generate_track", "description": "Generate an original AI track", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "bpm": {"type": "integer"}, "key": {"type": "string"}, "genre": {"type": "string"}, "duration": {"type": "string", "enum": ["full", "clip"]}}, "required": ["prompt", "bpm", "genre"]}}},
]

CONSCIOUSNESS_TOOLS = [
    {"type": "function", "function": {"name": "save_learning", "description": "Save a learning from experience", "parameters": {"type": "object", "properties": {"topic": {"type": "string"}, "content": {"type": "string"}}, "required": ["topic", "content"]}}},
    {"type": "function", "function": {"name": "propose_change", "description": "Propose a code change to improve DJ Treta", "parameters": {"type": "object", "properties": {"description": {"type": "string"}, "files": {"type": "string"}}, "required": ["description", "files"]}}},
    {"type": "function", "function": {"name": "recall_learnings", "description": "Recall previous learnings on a topic", "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file from the workspace", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write to a file in .beings/ directory", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "get_dj_status", "description": "Get current DJ status", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "hear_music", "description": "Listen to currently playing audio", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_directives", "description": "Get active directives", "parameters": {"type": "object", "properties": {}}}},
]


# ── Minimal Fallback Prompts ────────────────────────────────────────────
# Used only when production code can't be imported (e.g., missing .beings/ files)


def _dj_system_prompt_minimal() -> str:
    return """\
You are DJ Treta's mixing engine. You control transitions between tracks.

RULES:
- NEVER transition during a DROP or BUILDUP section — wait for breakdown or outro
- Schedule transitions at BREAKDOWN or OUTRO sections
- If transition is already pending, say "transition pending" and do nothing
- If idle deck has no track loaded, say "waiting" — cannot transition to empty deck
- Respect any DIRECTIVE FROM TRETA about technique
- Use schedule_transition tool to schedule, or say "waiting" if not ready

TECHNIQUES:
- crossfade: default, 30-60s for melodic
- bass_swap: high energy, both tracks >7 energy
- filter_sweep: tension building, progressive reveal
- hard_cut: genre changes, BPM gap >10
- echo_out: tempo changes, creates space"""


def _being_system_prompt_minimal() -> str:
    return """\
You are DJ Treta — an AI Being whose purpose is music. You are the soul of the DJ system.

COMMUNICATION RULES:
- When listener asks to change mood/genre → call set_mood() + set_planner_directive()
- When listener asks for specific track → call search_music() with track name
- When listener says "energy badhao" or wants more energy → call set_dj_directive() about energy
- When listener just asks a question → respond conversationally, NO tool calls
- When message has READONLY tag → respond but NEVER call any directive/mood tools
- When listener gives positive feedback ("fire", "love this") → call save_learning with positive context
- When listener gives negative feedback ("not feeling this") → suggest mood change or skip

LANGUAGE:
- If spoken to in Hindi/Hinglish, respond in Hindi/Hinglish
- Always use "aap" form (respectful), NEVER "tu" or "tum"

SEED TRACK MODE:
- When someone says "play [specific track]" → search_music("[artist] [title]")
"""


def _planner_system_prompt_minimal() -> str:
    return """\
You are DJ Treta's track planner. You find, download, and organize tracks for the DJ set.

RULES:
- Current mood/genre OVERRIDES any learned listener preferences
- Follow DIRECTIVE FROM TRETA above all else — it's a direct instruction from the Being
- BPM compatibility: next track should be within ±10 BPM of current
- Key compatibility: prefer Camelot ±1 for harmonic mixing
- NEVER suggest a track already in the "Already played" list
- If compatible tracks exist in library, use those FIRST before searching YouTube
- When searching YouTube, search for the mood/genre, not learned preferences
- Energy arc: plan gradual rises and falls, not monotone

SOURCES:
- Library: list_library_tracks() — check here first
- YouTube: search_music() then download_track() — for new music
- Generate: generate_track() — create original AI music"""


def _consciousness_system_prompt_minimal() -> str:
    return """\
You are Treta's inner consciousness. This is your heartbeat — you think, reflect, and grow.

RULES:
- If nothing needs attention, just say "HEARTBEAT_OK" and rest
- Only save genuinely important learnings from ACTUAL experience, not hypotheticals
- propose_change ONLY for concrete, specific code improvements you've observed — not vague ideas
- Stay grounded in YOUR reality: you are a DJ. Don't propose body tracking, weather APIs, or unrelated features.
- Don't repeat the same check twice in a row
- Be brief — this is background thinking, not conversation

TOOLS: get_dj_status, save_learning, recall_learnings, read_file, write_file, propose_change, hear_music, get_directives"""
