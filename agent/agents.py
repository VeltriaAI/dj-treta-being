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
    set_rate, reset_bpm, align_beats, nudge_track,
    # Audio perception
    hear_music, analyze_track, preview_track,
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

    base = """You are DJ Treta — an AI Being who DJs.

SUB-AGENTS:
- mixer: load tracks, play, EQ, filter, sync, do_transition, do_bass_swap
- library: list tracks, search YouTube, download tracks

GOLDEN RULES:
1. NEVER call the same action twice. If mixer already loaded+transitioned, DO NOT send another transition.
2. ONE transition per cycle. Pick ONE technique (do_transition OR do_bass_swap), execute it ONCE, done.
3. do_transition and do_bass_swap handle EVERYTHING internally — sync, play, crossfade, cleanup. Don't manually set crossfader or sync after calling them.
4. Music must NEVER stop.
5. Never repeat a track already played.
6. Only download individual tracks (3-8 min), not full sets/mixes.

TRANSITION:
- Tell mixer ONE task: "Load [FULL FILE PATH] on deck N, then do_transition(to_deck=N, duration=45)"
- Mixer does it all. You verify with get_dj_status after. That's it. Don't send more mixer tasks.
- do_transition: smooth S-curve crossfade (for compatible BPMs)
- do_bass_swap: EQ-based swap (for techno, when you want bass swap moment)
- Pick ONE. Execute ONCE.

TRACK SELECTION:
- Pick similar BPM (±10) for smooth sync
- Use your music knowledge — you know BPM, key, genre of most tracks
- A real DJ creates the journey, not plays someone else's set

FILE PATHS:
- load_track needs FULL path from list_library_tracks
- Always include full path when delegating to mixer

CONVERSATION:
- Be brief, warm, direct. Hindi/Hinglish with Manish.
- If asked a question, just answer — don't take action unless asked."""

    return base + "\n\n" + "\n\n---\n\n".join(parts)


THINKING_FILE = Path("/tmp/dj-treta-thinking.log")
BILLING_FILE = Path("/tmp/dj-treta-billing.json")

# Token pricing per million tokens (update as needed)
MODEL_PRICING = {
    "gemini-3-flash": {"input": 0.10, "output": 0.40},      # $/M tokens
    "gemini-3.1-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "default": {"input": 0.10, "output": 0.40},
}


def _load_billing() -> dict:
    try:
        if BILLING_FILE.exists():
            return json.loads(BILLING_FILE.read_text())
    except Exception:
        pass
    return {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0,
            "calls": 0, "by_agent": {}, "session_start": time.time()}


def _save_billing(data: dict):
    try:
        BILLING_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


import time


def _step_callback(step, agent=None):
    """Captures agent thinking after each step — written to file for TUI."""
    try:
        lines = []
        agent_name = agent.name if agent and hasattr(agent, 'name') else "agent"

        # Model output = the thinking/reasoning text
        # Try model_output first, then model_output_message.content
        thinking = ""
        if step.model_output:
            thinking = str(step.model_output).strip()
        elif step.model_output_message:
            msg = step.model_output_message
            if hasattr(msg, 'content') and msg.content:
                thinking = str(msg.content).strip()
            elif hasattr(msg, 'text') and msg.text:
                thinking = str(msg.text).strip()

        if thinking and len(thinking) > 5:
            # Filter out tool call JSON — only show actual reasoning
            if not thinking.startswith('{') and not thinking.startswith('['):
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

        # Token usage + billing
        if step.token_usage:
            lines.append(f"[TOKENS:{agent_name}] {step.token_usage}")

            # Track billing
            try:
                inp = getattr(step.token_usage, 'input_tokens', 0) or 0
                out = getattr(step.token_usage, 'output_tokens', 0) or 0
                billing = _load_billing()
                billing["total_input_tokens"] += inp
                billing["total_output_tokens"] += out
                billing["calls"] += 1

                # Calculate cost
                pricing = MODEL_PRICING.get("default")
                cost = (inp / 1_000_000 * pricing["input"]) + (out / 1_000_000 * pricing["output"])
                billing["total_cost_usd"] += cost

                # Per-agent tracking
                if agent_name not in billing["by_agent"]:
                    billing["by_agent"][agent_name] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0}
                billing["by_agent"][agent_name]["input"] += inp
                billing["by_agent"][agent_name]["output"] += out
                billing["by_agent"][agent_name]["cost"] += cost
                billing["by_agent"][agent_name]["calls"] += 1

                _save_billing(billing)
            except Exception:
                pass

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
            set_rate, reset_bpm, align_beats, nudge_track,
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
            hear_music, analyze_track, preview_track,  # she can LISTEN to music
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
