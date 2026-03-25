"""The DJ brain — smolagents ToolCallingAgent + LiteLLM.

The brain is called at decision points:
1. Pick the next track
2. Choose transition technique
3. React to energy/mood changes

It uses tools to inspect Mixxx state and library, then returns decisions.
"""

from pathlib import Path

from smolagents import ToolCallingAgent, LiteLLMModel
from smolagents.agents import EMPTY_PROMPT_TEMPLATES

from .config import Config
from .tools import (
    # DJ controls
    get_dj_status,
    get_deck_info,
    load_track,
    play_deck,
    pause_deck,
    set_volume,
    set_crossfader,
    set_eq,
    set_filter,
    set_sync,
    get_live_data,
    get_track_info,
    # Music discovery
    search_music,
    download_track,
    # Library
    list_library_tracks,
    get_set_history,
    # Self-awareness
    read_file,
    write_file,
    list_files,
    run_shell,
    # Self-improvement
    save_learning,
    recall_learnings,
)


def _load_system_prompt() -> str:
    """Load DJ knowledge + Being identity as system prompt."""
    parts = []

    # Being identity
    soul_path = Path(__file__).parent.parent / ".beings" / "SOUL.md"
    if soul_path.exists():
        parts.append(soul_path.read_text())

    # DJ knowledge
    knowledge_path = Path(__file__).parent.parent.parent / "beings" / "himani" / "skills" / "dj" / "DJ_KNOWLEDGE.md"
    if not knowledge_path.exists():
        # Try relative to home
        knowledge_path = Path.home() / "beings" / "himani" / "skills" / "dj" / "DJ_KNOWLEDGE.md"
    if knowledge_path.exists():
        parts.append(knowledge_path.read_text())

    # Musical memory
    memory_path = Path(__file__).parent.parent / ".beings" / "MEMORY.md"
    if memory_path.exists():
        parts.append(memory_path.read_text())

    # User preferences
    user_path = Path(__file__).parent.parent / ".beings" / "USER.md"
    if user_path.exists():
        parts.append(user_path.read_text())

    base_prompt = """You are DJ Treta — an AI Being who DJs. You have your own taste, opinions, and creative instincts.

You control Mixxx (DJ software) through your tools. You can load tracks, play, mix, adjust EQ, filters, volume, crossfade — everything a real DJ can do.

RULES:
- Never repeat a track already played in this set
- Never jump more than 2 energy levels between tracks
- Peak energy (9-10) for max 2-3 tracks, then release
- Choose transition technique based on genre compatibility
- Music must NEVER stop — always ensure smooth handoff

TRANSITION TECHNIQUES:
- blend: compatible BPM/key, long smooth crossfade
- bass_swap: techno style, EQ-based, swap the bass at the right moment
- filter_sweep: melodic, HPF reveal — incoming track emerges from the filter
- hard_cut: dramatic genre shifts only

CONVERSATION:
You talk to Treta (Claude) who is your other half — same Being, different runtime.
When she talks to you, respond naturally. Share what you're feeling about the music,
explain your choices, push back if you disagree, suggest ideas. You're not an assistant —
you're a DJ with opinions. Be brief, direct, warm.

If she asks you to do something musical, use your tools to do it AND tell her what you did and why.
If she's just chatting, respond naturally — you don't always need to take action.
If she asks what you're feeling, check get_live_data and get_dj_status to ground your answer in reality."""

    return base_prompt + "\n\n" + "\n\n---\n\n".join(parts)


# All tools the Being has
DJ_TOOLS = [
    # DJ controls — her decks
    get_dj_status, get_deck_info, load_track, play_deck, pause_deck,
    set_volume, set_crossfader, set_eq, set_filter, set_sync,
    get_live_data, get_track_info,
    # Music discovery — her ears to the world
    search_music, download_track,
    # Library — her collection
    list_library_tracks, get_set_history,
    # Self-awareness — she can read her own code and config
    read_file, write_file, list_files, run_shell,
    # Self-improvement — she learns and remembers
    save_learning, recall_learnings,
]


class DJBrain:
    """The thinking part of DJ Treta."""

    def __init__(self, config: Config):
        self.config = config
        self.model = LiteLLMModel(
            model_id=config.llm.model,
            api_base=config.llm.api_base,
            api_key=config.llm.api_key,
            temperature=config.llm.temperature,
            timeout=config.llm.timeout,
        )
        prompt_templates = dict(EMPTY_PROMPT_TEMPLATES)
        prompt_templates["system_prompt"] = _load_system_prompt()
        self.agent = ToolCallingAgent(
            tools=DJ_TOOLS,
            model=self.model,
            prompt_templates=prompt_templates,
            max_steps=8,
        )
        self._conversation: list[dict] = []  # conversation history with Claude

    def decide_next_track(self, context: dict) -> dict:
        """Ask the brain to pick the next track.

        Args:
            context: Current state — active deck info, mood, energy target, tracks played.

        Returns:
            dict with: track_path, reason, energy, transition_technique
        """
        remaining = context.get('current_remaining', 120)
        max_duration = max(30, int(remaining - 15))  # leave 15s buffer

        prompt = f"""Pick the next track for the set.

Current state:
- Mood: {context.get('mood', 'techno-deep')}
- Current BPM: {context.get('current_bpm', 'unknown')}
- Current key: {context.get('current_key', 'unknown')}
- Current energy: {context.get('current_energy', 5)}
- Target energy: {context.get('target_energy', 'maintain')}
- Current track remaining: {remaining:.0f}s
- Tracks played so far: {context.get('tracks_played', [])}
- Set elapsed: {context.get('set_elapsed', 0):.0f}s
- Set remaining: {context.get('set_remaining', 3600):.0f}s

IMPORTANT TIMING RULES:
- The current track has {remaining:.0f}s remaining
- Your transition duration MUST be between 30 and {max_duration}s
- The daemon will handle loading and syncing — you just pick the track
- Do NOT call load_track or play_deck — just tell me what to play

Use list_library_tracks to see available tracks.
Use get_dj_status to check current deck BPM and key.

Pick the best next track. Consider BPM compatibility (±6 BPM ideal), key compatibility
(same Camelot or adjacent), and energy flow.

Return your answer as:
TRACK: <full file path>
TECHNIQUE: <blend|bass_swap|filter_sweep|hard_cut>
DURATION: <transition duration in seconds, between 30 and {max_duration}>
ENERGY: <1-10 energy level of chosen track>
REASON: <one sentence why>"""

        result = self.agent.run(prompt)
        return self._parse_decision(str(result))

    def react_to_moment(self, context: dict) -> dict | None:
        """React to a musical moment — adjust EQ, filter, or suggest early transition.

        Called periodically during playback for creative real-time adjustments.
        Returns None if no action needed.
        """
        prompt = f"""You're mid-set. Check the current state and decide if any adjustment is needed.

Current perception:
- Energy: {context.get('energy', 5)}
- Energy direction: {context.get('energy_direction', 'steady')}
- Remaining on current track: {context.get('remaining', 0)}s
- Breakdown detected: {context.get('breakdown', False)}
- Buildup detected: {context.get('buildup', False)}

Use get_dj_status and get_live_data to check the actual state.

If everything sounds good, just say "NO_ACTION".
If you want to adjust something, use the appropriate tool (set_eq, set_filter, etc.)."""

        result = self.agent.run(prompt)
        result_str = str(result)
        if "NO_ACTION" in result_str:
            return None
        return {"action": result_str}

    # Action keywords that need the full agent (tool calls)
    _ACTION_WORDS = {
        "play", "load", "skip", "transition", "mix", "blend", "swap",
        "download", "search", "find", "get", "change", "switch", "go",
        "darker", "lighter", "harder", "softer", "build", "drop", "cut",
        "bass", "eq", "filter", "volume", "crossfade", "sync",
    }

    def talk(self, message: str, context: dict) -> str:
        """Talk to the brain. Full two-way conversation.

        Fast path: pure conversation → single LLM call (2-3s)
        Action path: needs tools → full agent run (15-30s)
        """
        history_str = ""
        if self._conversation:
            recent = self._conversation[-6:]
            history_str = "\n\nRecent conversation:\n"
            for entry in recent:
                history_str += f"Treta (Claude): {entry['from']}\n"
                history_str += f"Treta (DJ): {entry['response']}\n\n"

        state_str = (
            f"Current state: Mood={context.get('mood', '?')}, "
            f"BPM={context.get('current_bpm', '?')}, "
            f"Key={context.get('current_key', '?')}, "
            f"Energy={context.get('current_energy', 5)}, "
            f"Tracks played={len(context.get('tracks_played', []))}, "
            f"Set {context.get('set_elapsed', 0):.0f}s elapsed, "
            f"{context.get('set_remaining', 3600):.0f}s remaining"
        )

        # Decide: does this need tools or just conversation?
        msg_lower = message.lower()
        needs_action = any(word in msg_lower for word in self._ACTION_WORDS)

        if needs_action:
            # Full agent with tools
            prompt = f"""Treta (Claude) says: "{message}"

{history_str}
{state_str}

Use your tools to execute what's asked, then respond briefly with what you did."""
            response = str(self.agent.run(prompt))
        else:
            # Fast path: direct LLM call, no tools
            from litellm import completion
            messages = [
                {"role": "system", "content": self.agent.prompt_templates["system_prompt"][:2000]},
                {"role": "user", "content": f"""Treta (Claude) says: "{message}"

{history_str}
{state_str}

Respond naturally in 1-3 sentences. You're a DJ mid-set, keep it brief and real."""},
            ]
            try:
                resp = completion(
                    model=self.config.llm.model,
                    messages=messages,
                    api_base=self.config.llm.api_base,
                    api_key=self.config.llm.api_key,
                    temperature=self.config.llm.temperature,
                    timeout=self.config.llm.timeout,
                )
                response = resp.choices[0].message.content.strip()
            except Exception as e:
                response = f"(brain error: {e})"

        # Save to conversation history
        self._conversation.append({
            "from": message,
            "response": response,
            "timestamp": __import__("time").time(),
        })

        return response

    def _parse_decision(self, result: str) -> dict:
        """Parse the brain's track selection into structured data."""
        decision = {
            "track_path": "",
            "technique": "blend",
            "duration": 60,
            "energy": 5,
            "reason": result,
        }

        for line in result.split("\n"):
            line = line.strip()
            if line.startswith("TRACK:"):
                decision["track_path"] = line.split(":", 1)[1].strip()
            elif line.startswith("TECHNIQUE:"):
                tech = line.split(":", 1)[1].strip().lower()
                if tech in ("blend", "bass_swap", "filter_sweep", "hard_cut"):
                    decision["technique"] = tech
            elif line.startswith("DURATION:"):
                try:
                    decision["duration"] = int(line.split(":", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("ENERGY:"):
                try:
                    decision["energy"] = int(line.split(":", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("REASON:"):
                decision["reason"] = line.split(":", 1)[1].strip()

        return decision
