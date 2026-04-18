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


# ── Tool Schemas — LIVE introspection from production agents ─────────────
#
# Previously these were hardcoded (and drifted badly post-v8 Phase 5/6).
# Now we build the OpenAI function schema by reflecting on the real
# functions registered on each LlmAgent in agents.create_agents(). When
# SOUL.md claims a tool DJ doesn't have, evals catch it immediately.

import inspect
from typing import get_type_hints
from unittest.mock import MagicMock, patch


def _py_type_to_json_type(t) -> str:
    """Map Python type hint to OpenAI JSON schema type."""
    origin = getattr(t, "__origin__", None)
    if t is int:
        return "integer"
    if t is float:
        return "number"
    if t is bool:
        return "boolean"
    if t is str:
        return "string"
    if origin in (list, tuple) or t is list or t is tuple:
        return "array"
    if origin is dict or t is dict:
        return "object"
    # Union / Optional / default fallback
    return "string"


def _function_to_openai_schema(func) -> dict:
    """Convert a Python function to OpenAI function-call schema format."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties: dict = {}
    required: list = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        py_type = hints.get(name, str)
        properties[name] = {"type": _py_type_to_json_type(py_type)}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    doc = (func.__doc__ or "").strip().split("\n")[0] or func.__name__
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": doc[:200],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _extract_agent_tools(agent) -> list:
    """Pull OpenAI schemas from an LlmAgent's FunctionTool list.

    ADK FunctionTool exposes the underlying function on `.func`. Some
    variants expose it on `.function` or `._func`. Handle all three.
    """
    out: list = []
    for t in getattr(agent, "tools", []) or []:
        func = (
            getattr(t, "func", None)
            or getattr(t, "function", None)
            or getattr(t, "_func", None)
        )
        if callable(func):
            schema = _function_to_openai_schema(func)
            if schema:
                out.append(schema)
    return out


_TOOLS_CACHE: dict = {}


class _ShimAgent:
    """Stand-in for google.adk LlmAgent during eval introspection.

    Real LlmAgent requires a live BaseLlm instance for pydantic validation;
    we don't need that — we only care about capturing `tools` and
    `sub_agents`. This shim stores whatever kwargs are passed.
    """

    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "")
        self.tools = kwargs.get("tools", []) or []
        self.sub_agents = kwargs.get("sub_agents", []) or []
        self.instruction = kwargs.get("instruction", "")
        self.description = kwargs.get("description", "")
        self.model = kwargs.get("model")


class _ShimFunctionTool:
    """Stand-in for google.adk FunctionTool; preserves the wrapped func."""

    def __init__(self, func=None, **_):
        self.func = func


class _ShimLongRunningTool(_ShimFunctionTool):
    pass


def _live_tools() -> dict:
    """Introspect tool schemas by building the real agent graph once.

    Cached per-process. On failure (e.g. agents module import error),
    returns {} and callers fall back to minimal schemas.
    """
    global _TOOLS_CACHE
    if _TOOLS_CACHE:
        return _TOOLS_CACHE

    try:
        with patch("agent.agents.LlmAgent", _ShimAgent), \
             patch("agent.agents.LiteLlm", return_value=MagicMock()), \
             patch("agent.agents.FunctionTool", _ShimFunctionTool), \
             patch("agent.agents.LongRunningFunctionTool", _ShimLongRunningTool):
            from agent.agents import create_agents
            from agent.config import load_config
            agents = create_agents(load_config())
        # v8: 5-tuple (being, dj, planner, library, producer)
        being, dj, planner = agents[0], agents[1], agents[2]
        library = agents[3] if len(agents) > 3 else None
        producer = agents[4] if len(agents) > 4 else None
        _TOOLS_CACHE = {
            "dj": _extract_agent_tools(dj),
            "being": _extract_agent_tools(being),
            "planner": _extract_agent_tools(planner),
            "library": _extract_agent_tools(library) if library else [],
            "producer": _extract_agent_tools(producer) if producer else [],
        }
        return _TOOLS_CACHE
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            f"eval_conftest live tool introspection failed: {exc!r} — "
            "using minimal fallback schemas"
        )
        return {}


_LIVE = _live_tools()

# Minimal fallbacks kept so tests can still run if introspection fails
# entirely (e.g. agents.py import error). Real tests should never see
# these — the live schemas above are authoritative.

_DJ_FALLBACK = [
    {"type": "function", "function": {"name": "schedule_transition",
     "description": "Schedule a transition",
     "parameters": {"type": "object", "properties": {
         "to_deck": {"type": "integer"},
         "at_position": {"type": "number"},
         "technique": {"type": "string"},
         "duration": {"type": "integer"},
     }, "required": ["to_deck"]}}},
]

DJ_TOOLS = _LIVE.get("dj") or _DJ_FALLBACK
BEING_TOOLS = _LIVE.get("being") or []
PLANNER_TOOLS = _LIVE.get("planner") or []
LIBRARY_TOOLS = _LIVE.get("library") or []
PRODUCER_TOOLS = _LIVE.get("producer") or []

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
