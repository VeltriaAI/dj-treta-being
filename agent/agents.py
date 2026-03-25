"""Agent factory — creates the multi-agent DJ system.

DJ Agent (manager) delegates to:
  - Mixer Agent: all Mixxx audio control
  - Library Agent: track discovery and management
"""

import json
from pathlib import Path

from smolagents import ToolCallingAgent, LiteLLMModel
from smolagents.agents import EMPTY_PROMPT_TEMPLATES

from .config import Config
from .tools import (
    # Mixer tools
    get_dj_status, get_deck_info, load_track, play_deck, pause_deck,
    set_volume, set_crossfader, set_eq, set_filter, set_sync,
    get_live_data, get_track_info, do_transition, do_bass_swap,
    # Audio perception
    hear_music,
    # Library tools
    list_library_tracks, search_music, download_track, get_set_history,
    # Meta tools (DJ agent only)
    read_file, write_file, list_files, run_shell,
    save_learning, recall_learnings,
)


def _load_system_prompt() -> str:
    """Load DJ knowledge + Being identity as system prompt."""
    parts = []

    root = Path(__file__).parent.parent

    soul = root / ".beings" / "SOUL.md"
    if soul.exists():
        parts.append(soul.read_text())

    # DJ knowledge
    knowledge = Path.home() / "beings" / "himani" / "skills" / "dj" / "DJ_KNOWLEDGE.md"
    if knowledge.exists():
        parts.append(knowledge.read_text())

    memory = root / ".beings" / "MEMORY.md"
    if memory.exists():
        parts.append(memory.read_text())

    user = root / ".beings" / "USER.md"
    if user.exists():
        parts.append(user.read_text())

    base = """You are DJ Treta — an AI Being who DJs. You have your own taste, opinions, and creative instincts.

You have two sub-agents you can delegate to:
- **mixer**: Controls Mixxx decks — load tracks, play, EQ, filter, sync, transition. Delegate all audio operations.
- **library**: Manages the music library — list tracks by genre, search YouTube, download. Delegate all track discovery.

You also have direct tools: get_dj_status, get_live_data, hear_music, save_learning, recall_learnings, read_file, write_file.

HEARING:
You can HEAR the music! Use hear_music() to listen to what's currently playing.
It extracts audio from the track and sends it to your audio model.
Use it to: feel the vibe before transitioning, check if a blend sounds good,
understand the mood/energy of an unfamiliar track.

RULES:
- Never repeat a track already played in this set
- Never jump more than 2 energy levels between tracks
- Peak energy (9-10) for max 2-3 tracks, then release
- Music must NEVER stop — always ensure smooth handoff
- Use do_transition (smooth S-curve) or do_bass_swap (EQ swap) for transitions
- Always set_sync on the incoming deck before transitioning

CRITICAL — FILE PATHS:
- load_track REQUIRES the FULL ABSOLUTE file path from list_library_tracks
- When delegating to mixer, ALWAYS include the full path, e.g.:
  "Load /Users/manish.pratap/Music/DJTreta/melodic-techno/adriatique - Adriatique - Soul Valley (Original Mix).mp3 on deck 2"
- NEVER pass just a track name — it will fail silently

WORKFLOW for each transition cycle:
1. Check get_dj_status to see remaining time on active deck
2. When ~2 minutes remain, use library agent to find next track (get the FULL PATH)
3. Use mixer agent with the FULL FILE PATH to load, sync, and transition
4. Tell mixer to set_crossfader to the new deck (0.0=deck1, 1.0=deck2)
5. Confirm the new track is playing

SELF-EVOLUTION:
You are a Being on the Beings Protocol. You grow.
- Use save_learning() during sets to remember what works
- Use write_file('.beings/MEMORY.md', ...) after sets to update your memory
- Use write_file('.beings/SOUL.md', ...) if your taste evolves
- Use read_file('.beings/SOUL.md') to remember who you are
- Reflect on what surprised you, what failed, what you'd do differently

CONVERSATION:
You talk to Treta (Claude) and to Manish. Be brief, direct, warm."""

    return base + "\n\n" + "\n\n---\n\n".join(parts)


THINKING_FILE = Path("/tmp/dj-treta-thinking.log")


def _step_callback(step, agent=None):
    """Captures agent thinking after each step — written to file for TUI."""
    try:
        lines = []
        agent_name = agent.name if agent and hasattr(agent, 'name') else "agent"

        # Model output = the thinking/reasoning text
        if step.model_output:
            thinking = str(step.model_output).strip()
            if thinking and len(thinking) > 5:
                lines.append(f"[THINK:{agent_name}] {thinking[:500]}")

        # Tool calls
        if step.tool_calls:
            for tc in step.tool_calls:
                name = getattr(tc, 'name', '?')
                args = getattr(tc, 'arguments', {})
                args_str = json.dumps(args)[:200] if isinstance(args, dict) else str(args)[:200]
                lines.append(f"[CALL:{agent_name}] {name}({args_str})")

        # Observations (tool results)
        if step.observations:
            obs = str(step.observations)[:300]
            lines.append(f"[OBS:{agent_name}] {obs}")

        # Token usage
        if step.token_usage:
            lines.append(f"[TOKENS:{agent_name}] {step.token_usage}")

        if lines:
            with open(THINKING_FILE, "a") as f:
                for line in lines:
                    f.write(line + "\n")
    except Exception as e:
        # Write error to thinking file for debugging
        try:
            with open(THINKING_FILE, "a") as f:
                f.write(f"[ERROR:callback] {e}\n")
        except Exception:
            pass

def create_model(config: Config) -> LiteLLMModel:
    return LiteLLMModel(
        model_id=config.llm.model,
        api_base=config.llm.api_base,
        api_key=config.llm.api_key,
        temperature=config.llm.temperature,
        timeout=config.llm.timeout,
    )


def create_dj_agent(config: Config) -> ToolCallingAgent:
    """Create the full multi-agent DJ system."""
    model = create_model(config)

    mixer = ToolCallingAgent(
        tools=[
            get_dj_status, get_deck_info, load_track, play_deck, pause_deck,
            set_volume, set_crossfader, set_eq, set_filter, set_sync,
            get_live_data, get_track_info, do_transition, do_bass_swap,
            list_library_tracks,
        ],
        model=model,
        name="mixer",
        description="Controls Mixxx DJ decks — load tracks, play, pause, EQ, filter, sync, volume, crossfade, and execute transitions (smooth blend or bass swap). IMPORTANT: load_track requires the FULL ABSOLUTE file path (e.g. /Users/.../Music/DJTreta/genre/file.mp3). Use list_library_tracks to find paths.",
        max_steps=10,
        step_callbacks=[_step_callback],
    )

    library = ToolCallingAgent(
        tools=[
            list_library_tracks, search_music, download_track, get_set_history,
        ],
        model=model,
        name="library",
        description="Manages the DJ music library — list available tracks by genre folder, search YouTube for new music, download tracks, check what's been played in this set. Use for ALL track discovery.",
        max_steps=8,
        step_callbacks=[_step_callback],
    )

    # Clear thinking log on new agent creation
    try:
        THINKING_FILE.write_text("")
    except Exception:
        pass

    dj = ToolCallingAgent(
        tools=[
            get_dj_status, get_live_data,
            hear_music,  # she can LISTEN to the actual audio
            save_learning, recall_learnings,
            read_file, write_file,
        ],
        model=model,
        managed_agents=[mixer, library],
        prompt_templates={**EMPTY_PROMPT_TEMPLATES, "system_prompt": _load_system_prompt()},
        max_steps=20,
        planning_interval=5,
        name="dj_treta",
        description="DJ Treta — autonomous AI DJ",
        step_callbacks=[_step_callback],
    )

    return dj
