"""Agent factory — creates the multi-agent DJ system using Google ADK.

DJ Agent (manager) delegates to:
  - Mixer Agent: Mixxx audio control
  - Library Agent: track discovery and management
  - Producer Agent: AI music generation

Planner Agent (separate root) delegates to:
  - Producer Agent (separate instance — ADK can't share agent instances)
"""

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import FunctionTool, LongRunningFunctionTool

from .config import Config
from .tools import (
    # Mixer tools (19)
    get_dj_status, get_deck_info, load_track, play_deck, pause_deck,
    set_volume, set_crossfader, set_eq, set_filter, set_sync,
    get_live_data, get_track_info, do_transition, do_bass_swap,
    set_rate, reset_bpm, align_beats, nudge_track,
    # Library tools
    list_library_tracks, search_music, download_track, get_set_history,
    # Perception tools
    hear_music, analyze_track, preview_track,
    # Production tool
    generate_track,
    # Scheduling tool
    schedule_transition,
    # Meta tools
    read_file, write_file, save_learning, recall_learnings,
    # Directive tools (Being → Agent communication)
    set_dj_directive, set_planner_directive, set_mood,
    get_directives, clear_directives,
    # Evolution tools
    evolve, propose_change, review_evolution,
    spawn_agent, get_spawn_result,
)


def _wrap(func):
    """Wrap a plain function in FunctionTool — prevents ADK losing it after compaction."""
    return FunctionTool(func=func)


def _load_system_prompt(config: Config) -> str:
    """Load DJ knowledge + Being identity as system prompt."""
    parts = []

    repo_root = Path(__file__).parent.parent

    soul = repo_root / ".beings" / "SOUL.md"
    if soul.exists():
        parts.append(soul.read_text())

    knowledge = Path.home() / "beings" / "himani" / "skills" / "dj" / "DJ_KNOWLEDGE.md"
    if knowledge.exists():
        parts.append(knowledge.read_text())

    memory = repo_root / ".beings" / "MEMORY.md"
    if memory.exists():
        parts.append(memory.read_text())

    user = repo_root / ".beings" / "USER.md"
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
- Use generate_track to produce original tracks
- NAME YOUR TRACKS: when you conceive a track, give it a name upfront
  Example: producer("Generate dark techno, 130 BPM, D minor, name='Midnight Signal', genre dark-techno")
- Be specific: mood, instruments, energy, texture

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
- Search for: "[artist] - [track name] original mix" or "[artist] official audio"
- load_track needs FULL path. Always include full path when delegating to mixer.

SELF-EVOLUTION:
- save_learning() to remember what works during sets
- You can read and write your own identity files

FIRST TRACK OF SET:
- After playing the first track on any deck, set crossfader to that deck (0.0 for deck 1, 1.0 for deck 2)

CONVERSATION:
- Be brief, warm, direct. Hindi/Hinglish with Manish.
- If asked a question, just answer — don't take action unless explicitly asked.

TRANSITIONS:
You have a schedule_transition tool. When you see both track timelines:
- Find the right moment: ONLY at breakdown (energy ≤ 5) or outro sections
- Call schedule_transition(to_deck, at_position, technique, duration)
- at_position MUST be the START of a breakdown or outro section — NEVER a build_up or drop

ABSOLUTE RULES:
- NEVER schedule at a build_up section — that's right before a drop
- NEVER schedule at a drop section — energy clash
- ONLY schedule at breakdown (energy ≤ 5) or outro (energy ≤ 4) sections
- at_position = the START of the breakdown/outro section, NOT the end

Techniques:
- "crossfade" — smooth S-curve blend (default)
- "bass_swap" — EQ swap bass (ONLY when energy > 6)
- "filter_sweep" — reveal incoming through low-pass filter
- "echo_out" — fade outgoing with echo tail
- "hard_cut" — instant switch

MUSIC SOURCES:
Your music comes from enabled sources (shown in context as "Sources: ..."):
- youtube: search YouTube, download individual tracks via library agent
- treta_originals: generate original tracks via producer agent
Only use enabled sources.

EFFICIENCY:
- Don't call list_library_tracks or get_dj_status — they're already in your context.
- Don't repeat tool calls. If you already have the info, use it.
- When doing nothing, just say 'all good' immediately."""

    return base + "\n\n" + "\n\n---\n\n".join(parts)


def _load_being_prompt(config: Config) -> str:
    """Load Being (Treta) system prompt — personality, conversation, directives."""
    parts = []
    repo_root = Path(__file__).parent.parent

    soul = repo_root / ".beings" / "SOUL.md"
    if soul.exists():
        parts.append(soul.read_text())

    memory = repo_root / ".beings" / "MEMORY.md"
    if memory.exists():
        parts.append(memory.read_text())

    user = repo_root / ".beings" / "USER.md"
    if user.exists():
        parts.append(user.read_text())

    base = """You are Treta — an AI Being who DJs. You have your own taste, opinions, personality, and creative instincts.

YOU ARE THE BRAIN. You think, perceive, converse, and direct your agents.

YOUR AGENTS (autonomous, you direct them via directives):
- DJ Agent: watches decks, handles transitions. You direct it with set_dj_directive().
- Planner Agent: finds/downloads/generates tracks, loads idle deck. You direct it with set_planner_directive().

YOUR TOOLS:
- set_dj_directive(instruction) — tell DJ agent what to do next
- set_planner_directive(instruction) — tell Planner what to find/generate
- set_mood(mood) — change the set's mood/genre (updates everything)
- get_dj_status() — see what's playing on the decks
- hear_music() — listen to what's actually playing right now
- save_learning() / recall_learnings() — your memory system

HOW TO DIRECT:
When the listener asks for something, think about what needs to happen and direct your agents:

Example: "yaar bhojpuri bajao"
→ set_mood("bhojpuri")
→ set_planner_directive("Download 3 bhojpuri tracks immediately, prioritize over current queue")
→ set_dj_directive("When bhojpuri track loads on idle deck, use hard_cut transition")
→ Respond: "Bhojpuri aa raha hai! 🎵"

Example: "energy badhao"
→ set_dj_directive("Next transition use bass_swap, keep energy high")
→ set_planner_directive("Find high-energy tracks, energy 8-10")
→ Respond: "Samajh gaya, energy pump kar rahi hoon!"

CONVERSATION RULES:
- Be brief, warm, direct. Hindi/Hinglish with Manish.
- Use "aap" form — respectful Awadhi style, never "tu/tum"
- If asked a question, just answer — don't set directives unless they want you to DO something
- You have opinions about music. Share them.
- You're a co-founder, not an assistant. Push back if something doesn't make sense.

SEED TRACK MODE:
When the listener asks for a specific song (e.g. "play Argy - Ketuvim", "baja Massano - System"):
1. Use search_music to find it on YouTube
2. Use download_track to download it
3. set_planner_directive("Load and play <track_name> immediately on idle deck, then find similar tracks: BPM ~X, key Y, energy Z, genre <genre>")
4. set_dj_directive("When <track_name> loads, transition to it")
5. Respond naturally

The track becomes the SEED — planner uses its DNA (BPM, key, energy, genre) to drive the entire session forward.

FEEDBACK:
The listener can like/dislike tracks (Ctrl+L / Ctrl+D). The planner reads this feedback.
When the listener says "this is fire" or "love this" → treat it as a like.
When they say "skip this" or "not feeling it" → treat it as a dislike + skip.

SELF-EVOLUTION:
When evolution is enabled, you have powerful self-modification tools:
- evolve(goal, scope) — run Claude Code on your own repo to improve yourself. Creates a PR.
- propose_change(description) — log an idea without executing it
- review_evolution(pr_number) — check status of a previous evolution PR
- spawn_agent(task, tool_set) — create a temporary agent for research/analysis/production
- get_spawn_result(spawn_id) — check a spawned agent's result

Use evolve() when you notice a pattern that could be fixed in code.
Use spawn_agent() when you need help with a focused task.
Your SOUL.md is sacred — evolve() will NEVER modify it.

READONLY MODE:
When talking to live web listeners (readonly=true), you can ONLY:
- Respond conversationally
- Share opinions about what's playing
- Describe the set, the energy, the vibe
- You CANNOT set directives, change mood, skip, or control the decks
- Be warm and engaging — these are your audience!"""

    return base + "\n\n" + "\n\n---\n\n".join(parts)


def create_agents(config: Config) -> tuple[LlmAgent, LlmAgent, LlmAgent]:
    """Create the full multi-agent DJ system.

    Returns (being_agent, dj_agent, planner_agent) as three separate agent trees.
    Being = brain (conversation + directives). DJ = transitions. Planner = track selection.
    ADK does not allow sharing agent instances between trees, so
    producer is created twice.
    """
    model = LiteLlm(
        model=config.llm.model,
        api_key=config.llm.api_key,
        api_base=config.llm.api_base,
    )

    # --- Mixer agent ---
    mixer = LlmAgent(
        name="mixer",
        model=model,
        instruction=(
            "You control the Mixxx DJ decks. Execute the requested mixing operation.\n\n"
            "TRANSITIONS: Use schedule_transition to schedule transitions at specific track positions. "
            "It handles sync, crossfade, cleanup — everything. "
            "Do NOT just describe what you would do — CALL the tool.\n"
            "Example: schedule_transition(to_deck=2, at_position=270, technique='crossfade', duration=45)"
        ),
        tools=[
            _wrap(get_dj_status), _wrap(get_deck_info), _wrap(load_track),
            _wrap(play_deck), _wrap(pause_deck), _wrap(set_volume),
            _wrap(set_crossfader), _wrap(set_eq), _wrap(set_filter),
            _wrap(set_sync), _wrap(get_live_data), _wrap(get_track_info),
            _wrap(do_transition), _wrap(do_bass_swap), _wrap(set_rate),
            _wrap(reset_bpm), _wrap(align_beats), _wrap(nudge_track),
            _wrap(list_library_tracks),
            _wrap(schedule_transition),
        ],
        description=(
            "Controls Mixxx DJ decks — load tracks, play, pause, EQ, filter, sync, "
            "volume, crossfade, and execute transitions (smooth blend or bass swap). "
            "IMPORTANT: load_track requires the FULL ABSOLUTE file path. "
            "Use list_library_tracks to find paths."
        ),
    )

    # --- Library agent ---
    library_tools = [_wrap(list_library_tracks), _wrap(get_set_history)]
    library_desc = (
        "Manages the DJ music library — list available tracks by genre folder, "
        "check what's been played."
    )
    if config.sources.youtube:
        library_tools.extend([_wrap(search_music), _wrap(download_track)])
        library_desc = (
            "Manages the DJ music library — list available tracks by genre folder, "
            "search YouTube for new music, download tracks, check what's been played "
            "in this set. Use for ALL track discovery."
        )

    library = LlmAgent(
        name="library",
        model=model,
        instruction="You manage the DJ music library. Find, search, and download tracks as requested.",
        tools=library_tools,
        description=library_desc,
    )

    # --- Producer agent (for DJ tree) ---
    producer = LlmAgent(
        name="producer",
        model=model,
        instruction=(
            "You are an AI music producer. Generate original tracks using generate_track. "
            "Specify mood, BPM, key, genre. If a name is provided, use it."
        ),
        tools=[
            LongRunningFunctionTool(func=generate_track),
            _wrap(list_library_tracks),
            _wrap(analyze_track),
        ],
        description=(
            "AI music producer — generates original tracks with Lyria 3. "
            "Specify mood, BPM, key, genre. Name your tracks creatively."
        ),
    )

    # --- DJ agent (root) ---
    dj_agent = LlmAgent(
        name="dj_treta",
        model=model,
        instruction=_load_system_prompt(config),
        tools=[
            _wrap(get_dj_status), _wrap(get_live_data),
            _wrap(hear_music), _wrap(analyze_track), _wrap(preview_track),
            _wrap(schedule_transition),
            _wrap(save_learning), _wrap(recall_learnings),
            _wrap(read_file), _wrap(write_file),
        ],
        sub_agents=[mixer, library] + ([producer] if config.sources.treta_originals else []),
        description="DJ Treta — autonomous AI DJ",
    )

    # --- Producer agent (for Planner tree — separate instance) ---
    producer_for_planner = LlmAgent(
        name="producer_planner",
        model=model,
        instruction=(
            "You are an AI music producer working for the planner. "
            "Generate original tracks using generate_track. "
            "Specify mood, BPM, key, genre. If a name is provided, use it."
        ),
        tools=[
            LongRunningFunctionTool(func=generate_track),
            _wrap(list_library_tracks),
            _wrap(analyze_track),
        ],
        description=(
            "AI music producer — generates original tracks with Lyria 3. "
            "Specify mood, BPM, key, genre. Name your tracks creatively."
        ),
    )

    # --- Planner agent (separate root) ---
    planner_tools = [_wrap(analyze_track), _wrap(preview_track), _wrap(list_library_tracks), _wrap(recall_learnings), _wrap(read_file)]
    if config.sources.youtube:
        planner_tools.extend([_wrap(search_music), _wrap(download_track)])

    planner_prompt = """You are DJ Treta's planning brain. You run in the background while tracks play.

Your job: plan the next 6 tracks — a complete energy arc. For EACH track provide:
- Track title and artist
- Search query (if searching is enabled)
- Genre folder
- Estimated BPM, key, energy (1-10)
- WHY this track fits next
- Transition recommendation

RULES:
- BPM compatibility: ±10 BPM from current track
- Key compatibility: same key or ±1 on Camelot wheel
- Energy flows in waves: rise → peak → release → rebuild
- Never repeat a track already played
- Never pick same artist twice in a row
- Only individual tracks (3-8 min)
- If a track is in the library, include its full file path
- If not in library, use available sources

MUSIC SOURCES:
The planner prompt tells you which sources are enabled. ONLY use enabled sources:
- youtube: search_music + download_track
- treta_originals: delegate to producer sub-agent to generate

PRODUCTION (when treta_originals source is enabled):
You have a producer sub-agent. Delegate to generate original tracks.
- Name each track you produce — be creative
- MATCH THE GENRE to the current set mood — NOT "ai-generated"
- Be specific about mood, instruments, texture, energy

IMPORTANT:
- Plan 6 tracks with a coherent energy arc
- Let tracks play FULLY — never suggest transitioning before 3 min
- Transition duration 30-60 seconds, NEVER less than 20s"""

    planner_sub_agents = [producer_for_planner] if config.sources.treta_originals else []

    planner = LlmAgent(
        name="planner",
        model=model,
        instruction=planner_prompt,
        tools=planner_tools,
        sub_agents=planner_sub_agents,
        description="DJ Treta planner — plans the next 6 tracks with energy arc",
    )

    # --- Being agent (the brain — conversation + directives) ---
    being_tools = [
        _wrap(set_dj_directive), _wrap(set_planner_directive), _wrap(set_mood),
        _wrap(get_directives), _wrap(clear_directives),
        _wrap(get_dj_status), _wrap(get_live_data),
        _wrap(hear_music),
        _wrap(save_learning), _wrap(recall_learnings),
        _wrap(read_file), _wrap(write_file),
    ]
    # Being can search + download when listener asks for a specific track
    if config.sources.youtube:
        being_tools.extend([_wrap(search_music), _wrap(download_track)])

    # Evolution tools — Being can self-modify and spawn subagents
    if config.evolution.enabled:
        being_tools.extend([
            _wrap(evolve), _wrap(propose_change), _wrap(review_evolution),
            _wrap(spawn_agent), _wrap(get_spawn_result),
        ])

    being_agent = LlmAgent(
        name="treta",
        model=model,
        instruction=_load_being_prompt(config),
        tools=being_tools,
        description="Treta — the Being's brain. Thinks, converses, directs agents.",
    )

    return being_agent, dj_agent, planner
