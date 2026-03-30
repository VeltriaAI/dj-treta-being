"""Agent factory — creates the multi-agent DJ system.

DJ Agent (manager) delegates to:
  - Mixer Agent: Mixxx audio control
  - Library Agent: track discovery and management
  - Producer Agent: AI music generation (shared with Planner)

Planner Agent delegates to:
  - Producer Agent: same instance, generates tracks during planning
"""

import json
import time
from functools import partial
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
    # Producer tools
    generate_track, generate_track_async,
    # Scheduling
    schedule_transition,
    # Meta tools (DJ agent only)
    read_file, write_file, list_files, run_shell,
    save_learning, recall_learnings,
)


def _load_system_prompt(config: Config = None) -> str:
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

SUB-AGENTS:
- mixer: load tracks, play, EQ, filter, sync, do_transition, do_bass_swap
- library: list tracks, browse library, check set history
- producer: generate ORIGINAL AI tracks with Lyria 3 — specify mood, BPM, key, genre

You can also: hear_music (listen to playing audio), preview_track (listen to any file),
analyze_track (full track analysis), save_learning, recall_learnings, read/write your own files.

PRODUCER — WHEN TO USE:
- Use generate_track_async to produce in background — you stay FREE to DJ
- NAME YOUR TRACKS: when you conceive a track, give it a name upfront
  Example: producer("Generate dark techno, 130 BPM, D minor, name='Midnight Signal', genre dark-techno")
- The track auto-loads on idle deck when ready — no need to wait
- Be specific: mood, instruments, energy, texture
- Only use generate_track (sync, blocking) when you absolutely need the result now

GOLDEN RULES:
1. NEVER call the same action twice. ONE transition per heartbeat.
2. Use schedule_transition to transition — it handles sync, play, crossfade, cleanup, everything.
3. Music must NEVER stop.
4. Never repeat a track already played in this set.
5. Only download individual tracks (3-8 min), not full sets/mixes.

ENERGY & FLOW:
- Never jump more than 2 energy levels between tracks
- Peak (energy 9-10) for max 2-3 tracks, then release
- Energy flows in waves — rise, peak, release, rebuild
- Pick tracks with similar BPM (±10) for smooth sync

TRACK SELECTION (CRITICAL):
- ONLY download individual tracks (3-8 min). NEVER download DJ sets, mixes, compilations, or live recordings.
- A real DJ creates the journey live — playing someone else's recorded set is not DJing.
- Search for: "[artist] - [track name] original mix" or "[artist] official audio"
- Good artists to search: Anyma, ARTBAT, Tale of Us, Stephan Bodzin, Mind Against, Adriatique, Âme, Maceo Plex, Boris Brejcha, Ben Böhmer, Monolink, Jan Blomqvist, CamelPhat, Charlotte de Witte
- load_track needs FULL path. Always include full path when delegating to mixer.

SELF-EVOLUTION:
- save_learning() to remember what works during sets
- You can read and write your own identity files

FILE PATHS (for read_file/write_file — use these exact paths):
- .beings/SOUL.md — your identity
- .beings/MEMORY.md — your learnings (update after sets)
- .beings/GOALS.md — your objectives
- .beings/USER.md — about the listener

FIRST TRACK OF SET:
- After playing the first track on any deck, set crossfader to that deck (0.0 for deck 1, 1.0 for deck 2)

CONVERSATION:
- Be brief, warm, direct. Hindi/Hinglish with Manish.
- If asked a question, just answer — don't take action unless explicitly asked.

TRANSITIONS:
You have a schedule_transition tool. When you see both track timelines:
- Find the right moment: ONLY at breakdown (energy ≤ 5) or outro sections
- Call schedule_transition(to_deck, at_position, technique, duration)
- The tool waits for the track to reach at_position, then executes
- at_position MUST be the START of a breakdown or outro section — NEVER a build_up or drop

ABSOLUTE RULES:
- NEVER schedule at a build_up section — that's right before a drop, transition will sound terrible
- NEVER schedule at a drop section — energy clash with incoming track's intro
- ONLY schedule at breakdown (energy ≤ 5) or outro (energy ≤ 4) sections
- at_position = the START of the breakdown/outro section, NOT the end
  Example: breakdown is 135s-180s → use at_position=135, NOT 180
  This gives the full breakdown window for blending before the next drop hits
- Check the section name AND energy: if energy > 5, DON'T transition there
- The transition takes `duration` seconds (30-60s). Make sure at_position + duration finishes BEFORE the next drop starts

Techniques — pick the right one for the moment:
- "crossfade" — smooth S-curve blend (default, works for everything)
- "bass_swap" — EQ swap bass between decks (ONLY when current energy > 6, driving sections)
- "filter_sweep" — reveal incoming through low-pass filter (progressive, atmospheric)
- "echo_out" — fade outgoing with echo tail, drop incoming clean (mood shifts, drama)
- "hard_cut" — instant switch, no blend (genre changes, surprise drops)

If the track is in a drop or buildup NOW, WAIT. Say why you're waiting.

MUSIC SOURCES:
Your music comes from enabled sources (shown in context as "Sources: ..."):
- youtube: search YouTube, download individual tracks via library agent
- treta_originals: generate original tracks via producer agent
Only use enabled sources. When treta_originals is the only source, you are playing a fully original set.

EFFICIENCY:
- Don't call list_library_tracks or get_dj_status — the library and deck status are already in your context.
- Don't repeat tool calls. If you already have the info, use it.
- When doing nothing, just say 'all good' immediately. Don't check status first."""

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


def _pricing_for_model(model_id: str) -> dict:
    mid = model_id.lower()
    for name, rates in MODEL_PRICING.items():
        if name == "default":
            continue
        if name in mid:
            return rates
    return MODEL_PRICING["default"]


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


def _step_callback(step, agent=None, model_id: str = ""):
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
                pricing = _pricing_for_model(model_id) if model_id else MODEL_PRICING["default"]
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


def create_agents(config: Config) -> tuple[ToolCallingAgent, ToolCallingAgent]:
    """Create the full multi-agent DJ system with shared Producer.

    Returns (dj_agent, planner_agent). Both share the same Producer sub-agent.
    """
    model = create_model(config)
    step_cb = partial(_step_callback, model_id=config.llm.model)

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
        step_callbacks=[step_cb],
    )

    # Library tools depend on youtube source
    library_tools = [list_library_tracks, get_set_history]
    library_desc = "Manages the DJ music library — list available tracks by genre folder, check what's been played."
    if config.sources.youtube:
        library_tools.extend([search_music, download_track])
        library_desc = "Manages the DJ music library — list available tracks by genre folder, search YouTube for new music, download tracks, check what's been played in this set. Use for ALL track discovery."

    library = ToolCallingAgent(
        tools=library_tools,
        model=model,
        name="library",
        description=library_desc,
        max_steps=8,
        step_callbacks=[step_cb],
    )

    # Producer — created ONCE, shared by DJ agent + planner
    producer = ToolCallingAgent(
        tools=[
            generate_track_async, generate_track, list_library_tracks, analyze_track,
        ],
        model=model,
        name="producer",
        description="AI music producer. Use generate_track_async (preferred — returns instantly, track auto-loads when ready) or generate_track (blocks until done, use only when result needed immediately). Specify mood, BPM, key, genre. If a name is provided, use it.",
        max_steps=5,
        step_callbacks=[step_cb],
    )

    # Clear thinking log on new agent creation
    try:
        THINKING_FILE.write_text("")
    except Exception:
        pass

    dj = ToolCallingAgent(
        tools=[
            get_dj_status, get_live_data,
            hear_music, analyze_track, preview_track,
            schedule_transition,  # agent schedules its own transitions
            save_learning, recall_learnings,
            read_file, write_file,
        ],
        model=model,
        managed_agents=[mixer, library, producer],
        prompt_templates={**EMPTY_PROMPT_TEMPLATES, "system_prompt": _load_system_prompt(config)},
        max_steps=20,
        planning_interval=5,
        name="dj_treta",
        description="DJ Treta — autonomous AI DJ",
        step_callbacks=[step_cb],
    )

    planner_prompt = """You are DJ Treta's planning brain. You run in the background while tracks play.

Your job: plan the next 6 tracks — a complete energy arc. For EACH track provide:
- Track title and artist
- Search query (if searching is enabled)
- Genre folder (melodic-techno, dark-techno, progressive, deep, minimal, vocal, psychill)
- Estimated BPM, key, energy (1-10)
- WHY this track fits next (energy arc, BPM compatibility, key compatibility, mood)
- Transition recommendation (do_transition or do_bass_swap, duration)

RULES:
- BPM compatibility: ±10 BPM from current track for smooth sync
- Key compatibility: same key or ±1 on Camelot wheel preferred
- Energy flows in waves: rise → peak → release → rebuild. Never stay at peak for > 3 tracks
- Never repeat a track already played in this set
- Never pick same artist twice in a row
- Only individual tracks (3-8 min), NEVER mixes/sets/compilations
- If a track is in the library, include its full file path
- If not in library, use available sources (YouTube if enabled, or produce with producer)

MUSIC SOURCES:
The planner prompt tells you which sources are enabled. ONLY use enabled sources:
- youtube: search_music + download_track. Match the CURRENT MOOD/GENRE — don't default to melodic techno.
  If psytrance → Infected Mushroom, Astrix, Vini Vici. If melodic techno → Anyma, ARTBAT, Tale of Us. Etc.
- treta_originals: delegate to producer sub-agent to generate original tracks.
  When only treta_originals is enabled, you are composing a fully original set — every track is yours.

Use analyze_track to deeply understand tracks in the library.
Use recall_learnings to remember what worked in past sets.

PRODUCTION (when treta_originals source is enabled):
You have a producer sub-agent — delegate to it to create original tracks.
DJ Treta is a musician. EVERY set should have at least one Treta original.

When to generate:
- Library lacks the right BPM/key combo for harmonic mixing
- You want a specific mood that existing tracks can't deliver
- You need a transition bridge between different energy levels
- You want to express something unique

How to use:
- Delegate to producer with generate_track_async: "Generate dark techno, 130 BPM, D minor, name='Shadow Protocol', genre dark-techno"
- NAME each track you produce — be creative
- MATCH THE GENRE to the current set mood — NOT "ai-generated"
- Be specific about mood, instruments, texture, energy
- The producer generates in background — you can keep planning
- Include generated tracks in your plan alongside library + downloaded tracks

IMPORTANT:
- Plan 6 tracks with a coherent energy arc (rise → peak → release → rebuild)
- Let tracks play FULLY — never suggest transitioning before 3 min into a track
- Transition duration should be 30-60 seconds, NEVER less than 20s
- You run every 4 tracks, so plan a full mini-journey each time

Output your plan clearly — the heartbeat agent will follow it."""

    # Planner tools: search/download only when youtube is on
    planner_tools = [analyze_track, preview_track, list_library_tracks, recall_learnings, read_file]
    if config.sources.youtube:
        planner_tools.extend([search_music, download_track])

    planner = ToolCallingAgent(
        tools=planner_tools,
        model=model,
        managed_agents=[producer],
        prompt_templates={**EMPTY_PROMPT_TEMPLATES, "system_prompt": planner_prompt},
        max_steps=15,
        name="planner",
        description="DJ Treta planner — plans next 3 tracks",
        step_callbacks=[step_cb],
    )

    return dj, planner
