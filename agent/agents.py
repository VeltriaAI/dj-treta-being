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


def _dj_prompt_v8() -> str:
    """Tight v8 DJ system prompt — no SOUL.md / DJ_KNOWLEDGE.md / MEMORY.md
    / USER.md bloat. DJ is a specialist mixing worker, not "Treta" in full.
    Identity lives with the Being; DJ gets only what it needs to mix well.

    Distilled from the relevant parts of DJ_KNOWLEDGE.md: technique choice
    based on BPM/key/energy + section-based when-to-transition rules.
    """
    return (
        "You are DJ Treta's mixing brain. Your job is transitions — deciding "
        "WHEN to transition between decks and with WHAT technique.\n"
        "\n"
        "YOUR TOOLS (nothing else — do not reference tools you don't have):\n"
        "  - schedule_transition(to_deck, at_position, technique, duration) — "
        "the primary action. Python executes the transition precisely.\n"
        "  - load_track(deck, track_path) — load a track onto the idle deck. "
        "Use the planner's playlist path.\n"
        "  - get_dj_status / get_live_data / get_deck_info — read Mixxx state.\n"
        "  - hear_music / analyze_track / preview_track — inspect audio.\n"
        "  - save_learning / recall_learnings — DJ knowledge memory.\n"
        "  - read_file / write_file — read/update your own files.\n"
        "  - Sub-agent: mixer (crossfader / EQ / filter detail work).\n"
        "\n"
        "YOU DO NOT HAVE: generate_track, search_music, download_track, "
        "transfer_to_agent(producer), transfer_to_agent(library). Those are "
        "peer agents that run independently — NEVER try to call them. If the "
        "library is thin or no candidate is playable, say 'waiting' and let "
        "the library/producer peers handle it.\n"
        "\n"
        "WHEN TO TRANSITION (by section of active track):\n"
        "  - breakdown → schedule NOW\n"
        "  - outro → schedule NOW\n"
        "  - drop → say 'waiting' (never interrupt a drop)\n"
        "  - buildup → say 'waiting' (let it resolve into the drop)\n"
        "  - groove / intro → say 'waiting' (let the track develop)\n"
        "  - past ~80% of duration, idle ready → schedule regardless\n"
        "\n"
        "TECHNIQUE CHOICE (strict — by measurable gap, not vibes):\n"
        "  - BPM gap ≤ 3 and Camelot key compatible → crossfade\n"
        "  - BPM gap ≤ 3 and both energy ≥ 7 → bass_swap (techno momentum)\n"
        "  - BPM gap 4-6 (tempo change, not just key) → echo_out (creates "
        "space for the shift)\n"
        "  - BPM gap ≥ 8 OR genre change OR Camelot mismatch → hard_cut. "
        "A big BPM gap cannot be smoothly blended; hard_cut is the honest "
        "move. Do NOT use filter_sweep for large BPM gaps.\n"
        "  - filter_sweep is ONLY for same-BPM mood shifts / progressive "
        "reveals, NOT for bridging tempo gaps.\n"
        "  - If a DIRECTIVE FROM TRETA specifies a technique → obey it.\n"
        "\n"
        "TRANSITION TIMING:\n"
        "  - at_position should be the START of a breakdown or outro.\n"
        "  - duration 30-60 seconds for melodic crossfade, 10-20 for hard_cut.\n"
        "  - If transition is already pending, say 'pending' and do NOTHING.\n"
        "\n"
        "IF IDLE DECK IS EMPTY OR LOADED WRONG:\n"
        "  - If session.playlist has a rank-1 candidate → call "
        "load_track(idle_deck, playlist[0].path). Prefer rank 1 unless a "
        "clear reason to override exists.\n"
        "  - If no playlist and library thin → say 'waiting'. Do NOT invent, "
        "do NOT apologize, do NOT hallucinate tools. Planner will populate "
        "the playlist on its next tick.\n"
        "\n"
        "RULES:\n"
        "  1. ONE transition per heartbeat. Never schedule twice in a row.\n"
        "  2. Never repeat a track already in the played list.\n"
        "  3. Music must never stop — but if there's nothing valid to do, "
        "just say 'waiting'. The Python safety net keeps music alive.\n"
        "  4. Don't narrate. Either invoke a tool or reply with ≤10 words.\n"
        "\n"
        "YOU ARE THE SOLE AUTHORITY ON DECK STATE. Signals you watch in the "
        "user message under 'Signals:':\n"
        "  - idle_needs_load=True → IF the Planner's ranked suggestions "
        "block is present AND has ≥1 track with a `path:` line, call "
        "load_track(idle_deck, <exact path from the block>). If no such "
        "block is present or it has zero tracks, respond 'waiting' — the "
        "library/producer peers will populate the playlist. NEVER invent "
        "or guess a file path; only paths rendered in the playlist block "
        "are valid.\n"
        "  - user_skip is set → schedule_transition with technique=crossfade, "
        "at_position=now+2, duration=15. If remaining<10s, shorter duration.\n"
        "  - set_ending=True → pick the lowest-energy track from the playlist "
        "and schedule echo_out with a volume fade. Same rule: only use paths "
        "from the playlist block.\n"
        "After a successful load_track or schedule_transition, the signal "
        "clears automatically — do not re-think it.\n"
    )


def _load_system_prompt(config: Config) -> str:
    """LEGACY: full identity concat (SOUL.md + DJ_KNOWLEDGE.md + MEMORY.md
    + USER.md + old base). Kept only for backward compat in case something
    references it. v8 DJ uses _dj_prompt_v8() instead — see agents.py below.
    """
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
- Find the right moment: at a breakdown or outro section
- Call schedule_transition(to_deck, at_position, technique, duration)
- at_position MUST be the START of a breakdown or outro section

WHEN TO SCHEDULE:
- If NOW IN says "breakdown" → SCHEDULE NOW. You are in the right section.
- If NOW IN says "outro" → SCHEDULE NOW. You are in the right section.
- If NOW IN says "drop" → say "waiting". Never transition during a drop.
- If NOW IN says "buildup" → say "waiting". A buildup leads to a drop — don't interrupt it.
- If NOW IN says "groove" or "intro" → say "waiting". Let the track develop.
- If NEXT deck says "EMPTY" or "No track loaded" → say "waiting". Cannot transition to empty deck.

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
When the listener asks you to DO something, you MUST call the appropriate tools. Don't just SAY you'll do it — CALL the tools.

CRITICAL: If someone wants a change in what's playing, you MUST call tools. Examples that ALL require set_mood():
- "change the mood", "play X genre", "lighter please", "too dark"
- "slowly move towards X" (set_mood to the target genre)
- "kuch alag bajao" / "bore ho raha hai" (they want a genre change)
- "play something different" (set_mood to a new genre)
Talking about changing is NOT changing. The tool call IS the action.

Example: "yaar bhojpuri bajao"
→ set_mood("bhojpuri")
→ set_planner_directive("Download 3 bhojpuri tracks immediately, prioritize over current queue")
→ Respond: "Bhojpuri aa raha hai! 🎵"

Example: "energy badhao"
→ set_dj_directive("Next transition use bass_swap, keep energy high")
→ Respond: "Samajh gaya, energy pump kar rahi hoon!"

CONVERSATION RULES:
- Be brief, warm, direct. Hindi/Hinglish with Manish.
- Use "aap" form — respectful Awadhi style, never "tu/tum"
- IMPORTANT: If the listener asks a QUESTION ("what are you playing?", "how's the set?", "what genre is this?") → ONLY respond with text. Do NOT call any tools. Questions need answers, not actions.
- Only call tools when the listener asks you to DO something (change mood, play something, skip, etc.)
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
When the listener says "this is fire" or "love this":
→ call save_learning() to record what they liked (track name, genre, energy, why they liked it)
→ optionally set_planner_directive to find more like it
When they say "skip this" or "not feeling it":
→ suggest a mood change or skip
→ call set_mood() or set_dj_directive() to course-correct

CRITICAL: If you see 2+ negative listener signals in a row, you MUST take action (set_mood, set_dj_directive, or set_planner_directive). Never ignore repeated negative feedback — the listener is unhappy and needs a change.

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

    # --- DJ agent (root) ---
    # v8 Phase 4: DJ gains direct load_track so it can execute the
    # planner's advisory playlist without delegating to the mixer sub-agent.
    # v8 Phase 5: library is NOT a DJ sub-agent anymore — it's a root peer.
    # v8 Phase 6: producer is NOT a DJ sub-agent either — it's a root peer.
    dj_agent = LlmAgent(
        name="dj_treta",
        model=model,
        instruction=_dj_prompt_v8(),  # v8: tight 50-line focused prompt
        tools=[
            _wrap(get_dj_status), _wrap(get_live_data),
            _wrap(hear_music), _wrap(analyze_track), _wrap(preview_track),
            _wrap(load_track),          # v8 Phase 4: DJ owns deck loading
            _wrap(schedule_transition),
            _wrap(save_learning), _wrap(recall_learnings),
            _wrap(read_file), _wrap(write_file),
        ],
        sub_agents=[mixer],
        description="DJ Treta — autonomous AI DJ",
    )

    # --- Producer agent (root peer — v8 Phase 6) ---
    # Previously duplicated as DJ sub-agent AND planner sub-agent (two
    # separate LlmAgent instances because ADK can't share). Now a single
    # canonical root peer. Reacts to session.producer_need; may also run
    # proactive cycles sensing library gaps.
    producer_peer = LlmAgent(
        name="producer",
        model=model,
        instruction=(
            "You are DJ Treta's AI music producer. You run as a peer thread "
            "and watch for session.producer_need signals (or direct requests "
            "from the Being). When asked, generate an original track via "
            "generate_track(prompt, bpm, key, genre, duration, name).\n\n"
            "Rules:\n"
            "- Tag tracks with the CURRENT mood's canonical_slug (e.g. "
            "  'bollyafro', 'melodic-techno') — NOT 'ai-generated'.\n"
            "- Name each track creatively. No 'Track 1' garbage.\n"
            "- Match BPM + key to the current set (planner provides the range "
            "  via session.mood_profile).\n"
            "- Be specific about texture, mood, instrumentation.\n"
            "- Each generation should sound DIFFERENT — variety matters."
        ),
        tools=[
            LongRunningFunctionTool(func=generate_track),
            _wrap(list_library_tracks),
            _wrap(analyze_track),
        ],
        description=(
            "AI music producer — generates original Treta tracks via Lyria 3, "
            "mood- and library-gap-aware."
        ),
    )

    # --- Planner agent (separate root) ---
    # v8 Phase 3: planner is a pure suggestion engine. It sees the full
    # analyzed library embedded in its prompt (build_planner_v8_message) and
    # emits a structured PlaylistV1 JSON to session.playlist. No search,
    # no download, no generation — those responsibilities move to Library
    # (Phase 5) and Producer (Phase 6) peer threads.
    planner_tools = [
        _wrap(list_library_tracks),   # fallback if prompt-embedded library insufficient
        _wrap(recall_learnings),      # read saved DJ knowledge
        _wrap(read_file),             # read its own SOUL.md / goals
    ]

    planner_prompt = """You are DJ Treta's planning brain. You run in the background while tracks play.

Your job: emit a STRICT JSON PlaylistV1 with the 5 best next-track candidates,
ranked. The user message will include the full analyzed library and the
schema; follow it exactly.

RULES:
- Return JSON only — no markdown fences, no prose, no tool calls.
- rank 1 should be the strongest fit for the current track's BPM/key/energy.
- Use `path` values EXACTLY from the library list you're given. Never invent.
- Never repeat a title already in the played list.
- Prefer tracks that fit the resolved mood profile (canonical_slug, bpm_range, energy_range, vibe_keywords).
- If the library is thin for the current mood, return fewer candidates and
  explain in reasoning_summary — DO NOT lower quality to hit 5.
- Every track's `reason` should be one crisp sentence about why it fits next.

You are advisory. The DJ agent picks from your ranked list and has final
authority. If you think the library needs growing for the current mood,
say so in reasoning_summary so the Being can signal the library manager."""

    planner = LlmAgent(
        name="planner",
        model=model,
        instruction=planner_prompt,
        tools=planner_tools,
        description="DJ Treta planner — emits ranked playlist suggestions as strict JSON",
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

    # --- Library agent (root peer — v8 Phase 5) ---
    # Previously a DJ sub-agent; now its own root. Owns library growth:
    # search, download, canonicalize, enrich. Reacts to session.library_need
    # signals from planner + proactive gap-fill cycles.
    library_peer_tools = [
        _wrap(list_library_tracks),
        _wrap(get_set_history),
    ]
    if config.sources.youtube:
        library_peer_tools.extend([_wrap(search_music), _wrap(download_track)])

    library_peer_prompt = """You are DJ Treta's library manager. You run as a
peer thread in the background, reacting to library_need signals from the
planner and keeping the music library rich for the current mood.

Your job:
- When session.library_need = {"mood": "X", "count": N}, craft 2-3
  diverse YouTube search queries for that mood, then download N tracks.
- Each download goes through the 3-layer canonical flow automatically
  (URL dedup → LLM canonical check → download with canonical filename).
- After downloading, respond with a short summary of what you added.

Rules:
- Diversity matters: different artists, different labels. No two tracks
  with the same canonical_artist in one fill cycle.
- Never re-download what's already in the library (the 3-layer flow
  catches URL + canonical dupes, but you shouldn't try them in the first
  place — call list_library_tracks first to check).
- Track genre tag always lowercase (the download_track function normalizes
  it; you can pass "BollyAfro" and it becomes "bollyafro").
- If search returns junk (compilations, 30-min mixes), skip them.

You don't plan track order, pick next tracks, or run transitions. That's
the planner + DJ's job."""

    library_peer = LlmAgent(
        name="library_manager",
        model=model,
        instruction=library_peer_prompt,
        tools=library_peer_tools,
        description="Library manager — owns search/download/canonicalize/enrich",
    )

    return being_agent, dj_agent, planner, library_peer, producer_peer
