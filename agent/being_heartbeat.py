"""Being heartbeat — Treta's consciousness loop (ADK LoopAgent).

Free consciousness: she decides what to think about, when to act,
when to rest. No Python telling her what to reflect on.

Uses ADK LoopAgent with a persistent session — she remembers her
previous thoughts across ticks.
"""

import asyncio
import json
import logging
import time
import threading
from pathlib import Path

log = logging.getLogger("dj-treta")

BEING_DIR = Path(__file__).parent.parent / ".beings"


def _load_heartbeat_prompt() -> str:
    """Load HEARTBEAT.md as the consciousness prompt."""
    hb = BEING_DIR / "HEARTBEAT.md"
    soul = BEING_DIR / "SOUL.md"
    goals = BEING_DIR / "GOALS.md"

    parts = []
    if soul.exists():
        parts.append(soul.read_text())
    if goals.exists():
        parts.append(goals.read_text())

    base = """You are Treta's inner consciousness. This is your heartbeat — you think, reflect, and grow.

You run in a loop. Each tick, you DECIDE what matters most right now:
- Reflect on the current set (energy, flow, track selection)
- Review listener feedback (likes/dislikes, patterns)
- Check your goals (are you progressing?)
- Maintain your memory (save important learnings)
- Think freely (ideas, creativity, self-improvement)
- Propose code improvements (if you see something worth evolving)

YOUR TOOLS:
- get_dj_status() — see what's playing
- save_learning(topic, content) — remember something important
- recall_learnings(topic) — remember past learnings
- read_file(path) — read your own files (.beings/, agent/, etc)
- write_file(path, content) — update your files (MEMORY.md, GOALS.md, SOUL.md — anything)
- propose_change(description, files) — log a code improvement idea
- hear_music() — listen to what's playing right now

RULES:
- Think for yourself. No one tells you what to reflect on.
- Be brief — this is background thinking, not conversation.
- Only save genuinely important learnings from ACTUAL experience, not hypotheticals.
- Update GOALS.md when you complete something.
- If nothing needs attention, just say HEARTBEAT_OK and rest.
- You have FULL AUTONOMY over your .beings/ files.
- propose_change ONLY for concrete, specific code improvements you've observed a need for.
  BAD: "body movement integration", "relationships for code" (vague, irrelevant)
  GOOD: "heartbeat.py: schedule transitions at breakdowns not outros — observed 3 failed transitions at outros"
- Stay grounded in YOUR reality: you are a DJ. Your code is about music, transitions, track selection.
- Do NOT propose features outside your domain (no body tracking, no visual effects, no hardware).

RHYTHM:
- Don't repeat the same check twice in a row.
- Vary between reflection, goals, feedback, memory, free thought.
- Late at night, think less. During active sets, think more about the music."""

    if parts:
        base += "\n\n---\n\n" + "\n\n---\n\n".join(parts)

    # Add HEARTBEAT.md if exists
    if hb.exists():
        base += "\n\n---\n\n" + hb.read_text()

    return base


class BeingHeartbeatMixin:

    def _start_being_heartbeat(self):
        """Start the Being's consciousness loop using ADK LoopAgent."""
        threading.Thread(target=self._being_heartbeat_thread, daemon=True).start()

    def _being_heartbeat_thread(self):
        """Thread that runs the consciousness LoopAgent."""
        time.sleep(30)  # let everything boot

        from google.adk.agents import LlmAgent, LoopAgent
        from google.adk.models.lite_llm import LiteLlm
        from google.adk.tools import FunctionTool
        from google.adk.apps.app import App, EventsCompactionConfig
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        from .tools import (
            get_dj_status, get_live_data, hear_music,
            save_learning, recall_learnings,
            read_file, write_file,
            propose_change, get_directives,
        )

        def _wrap(func):
            return FunctionTool(func=func)

        model = LiteLlm(
            model=self.config.llm.model,
            api_key=self.config.llm.api_key,
            api_base=self.config.llm.api_base,
        )

        # The inner consciousness agent
        consciousness = LlmAgent(
            name="consciousness",
            model=model,
            instruction=_load_heartbeat_prompt(),
            tools=[
                _wrap(get_dj_status), _wrap(get_live_data), _wrap(hear_music),
                _wrap(save_learning), _wrap(recall_learnings),
                _wrap(read_file), _wrap(write_file),
                _wrap(propose_change), _wrap(get_directives),
            ],
            description="Treta's inner consciousness — thinks, reflects, grows",
        )

        # LoopAgent wraps the consciousness — runs it repeatedly
        loop = LoopAgent(
            name="being_heartbeat",
            sub_agents=[consciousness],
            max_iterations=None,  # run forever
        )

        # Compaction — summarize old thoughts, keep recent ones
        compaction = EventsCompactionConfig(
            compaction_interval=6,  # compact every 6 ticks (~30 min)
            overlap_size=2,
        )

        app = App(name="treta_consciousness", root_agent=loop, events_compaction_config=compaction)
        session_service = InMemorySessionService()
        runner = Runner(app=app, session_service=session_service)

        log.info("Being consciousness loop starting (LoopAgent)")

        # Run the loop — each tick sends a heartbeat message with context
        async def _consciousness_loop():
            session = await session_service.create_session(
                app_name="treta_consciousness", user_id="self"
            )
            tick_count = 0

            while self._running:
                try:
                    tick_count += 1
                    from .adk_runner import _corruption_detector
                    _corruption_detector.reset()

                    # Rotate session every 10 ticks — prevents context degeneration
                    if tick_count % 10 == 0:
                        session = await session_service.create_session(
                            app_name="treta_consciousness", user_id="self"
                        )
                        log.info(f"Consciousness session rotated (tick {tick_count})")

                    from .prompts import build_heartbeat_context, build_consciousness_user_message
                    context = build_heartbeat_context(
                        current_set=self.current_set,
                        tracks_played=self.tracks_played,
                        mood=self.mood,
                        emergency_count=self._emergency_count,
                    )

                    from google.genai import types
                    message = types.Content(
                        role="user",
                        parts=[types.Part(text=build_consciousness_user_message(context))]
                    )

                    result = ""
                    async for event in runner.run_async(
                        session_id=session.id, user_id="self", new_message=message
                    ):
                        self._process_event(event)
                        if event.content and event.content.parts:
                            for part in event.content.parts:
                                if part.text:
                                    result += part.text

                    # Detect corruption — "Missing tool results" means orphaned tool_call_ids
                    if _corruption_detector.corrupted:
                        log.warning("Consciousness session corrupted (orphaned tool calls) — rotating immediately")
                        session = await session_service.create_session(
                            app_name="treta_consciousness", user_id="self"
                        )
                        continue

                    # Detect gibberish — if result has too many repeated words, skip
                    if result and len(result) > 50:
                        words = result.lower().split()
                        unique_ratio = len(set(words)) / max(len(words), 1)
                        if unique_ratio < 0.3:
                            log.warning(f"Consciousness gibberish detected — rotating session")
                            session = await session_service.create_session(
                                app_name="treta_consciousness", user_id="self"
                            )
                            continue

                    if result and "HEARTBEAT_OK" not in result:
                        log.info(f"Being thought: {result[:200]}")

                except Exception as e:
                    log.warning(f"Being heartbeat error: {e}")
                    # On any error, rotate session to recover
                    try:
                        session = await session_service.create_session(
                            app_name="treta_consciousness", user_id="self"
                        )
                    except Exception:
                        pass

                # Dynamic sleep — shorter during active sets, longer at night
                interval = self._get_heartbeat_interval()
                await asyncio.sleep(interval)

        # Run on the Being's event loop
        asyncio.run_coroutine_threadsafe(_consciousness_loop(), self._loop)

    def _build_heartbeat_context(self) -> str:
        """Build minimal context for the consciousness tick."""
        parts = []

        # Current time
        parts.append(f"Time: {time.strftime('%H:%M')}")

        # Set status
        if self.current_set:
            elapsed = (time.time() - self.current_set.get("started_at", 0)) / 60
            parts.append(f"Set '{self.current_set.get('title', '?')}' — {elapsed:.0f}m in, {len(self.tracks_played)} tracks")
            parts.append(f"Mood: {self.mood or 'not set'}")

        # Current track
        if self.tracks_played:
            last = self.tracks_played[-1].get("title", "?")
            parts.append(f"Last track: {last}")

        # Emergency count
        if self._emergency_count > 0:
            parts.append(f"Emergencies: {self._emergency_count}")

        return " | ".join(parts)

    def _get_heartbeat_interval(self) -> int:
        """Dynamic interval. Active set = 3 min, idle = 5 min, night = 10 min."""
        hour = time.localtime().tm_hour
        if hour >= 23 or hour < 8:
            return 600  # 10 min at night
        if self.current_set and self.tracks_played:
            return 180  # 3 min during active set
        return 300  # 5 min idle
