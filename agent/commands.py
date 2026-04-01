"""Commands mixin — TUI/MCP command dispatch and conversation."""

import json
import logging
import threading
import time
from pathlib import Path

import httpx

from .agents import create_agents
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.runners import Runner

log = logging.getLogger("dj-treta")

COMMAND_FILE = Path("/tmp/dj-treta-command.json")


class CommandsMixin:

    def _check_commands(self):
        if not COMMAND_FILE.exists():
            return
        try:
            raw = json.loads(COMMAND_FILE.read_text())
            COMMAND_FILE.unlink()
        except Exception:
            return

        cmd = raw.get("command", "")
        args = raw.get("args", {})
        cmd_id = raw.get("id", "")
        log.info(f"Command: {cmd}")

        self._last_command = cmd
        self._last_command_id = cmd_id
        self._last_result = "processing..."
        self._write_state()

        try:
            result = self._handle_command(cmd, args, cmd_id)
        except Exception as e:
            result = f"Error: {e}"

        if result != "processing...":
            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Result: {result[:200]}")

    def _handle_command(self, cmd, args, cmd_id):
        if cmd == "talk":
            message = args.get("message", "")
            if not message:
                return "No message"
            if not self.agent:
                return "Brain not ready"

            # Extract mood + capture user intent for planner
            if any(w in message.lower() for w in ["play", "start", "baja", "shuru", "bajao", "switch", "change"]):
                for m in ["melodic", "techno", "deep", "dark", "progressive", "ambient",
                          "chill", "vocal", "house", "psychill", "minimal", "bhojpuri",
                          "trance", "lofi", "bollywood", "psytrance"]:
                    if m in message.lower():
                        self.mood = m
                        break
                if not self.mood:
                    self.mood = "deep"
                # Capture full user intent — planner will see this
                self.user_intent = message

            threading.Thread(target=self._agent_talk, args=(message, cmd_id), daemon=True).start()
            return "processing..."

        elif cmd == "skip":
            threading.Thread(target=self._agent_skip, daemon=True).start()
            return "processing..."

        elif cmd == "stop":
            if self.agent and not self._agent_busy:
                threading.Thread(
                    target=lambda: self._invoke_agent("Fade out the current track gracefully over 30 seconds."),
                    daemon=True
                ).start()
                return "Fading out..."
            return "Agent busy — try again in a moment"

        elif cmd == "change_mood":
            new_mood = args.get("mood", self.mood)
            self.mood = new_mood
            # Update current set's mood + genre
            if self.current_set:
                self.current_set["mood"] = new_mood
                self.current_set["genre"] = new_mood
            # Capture as user intent so planner picks it up immediately
            self.user_intent = f"Switch to {new_mood} — listener changed mood"
            # Force planner to replan
            self._tracks_since_plan = self.config.planner.replan_every_n_tracks
            return f"Mood changed to {new_mood}"

        elif cmd == "change_sources":
            source = args.get("source", "")
            enabled = args.get("enabled", True)
            if source == "youtube":
                self.config.sources.youtube = enabled
            elif source in ("treta_originals", "originals"):
                self.config.sources.treta_originals = enabled
            # Recreate agents with new tool access
            log.info(f"Source changed: {source} → {'on' if enabled else 'off'} — rebuilding agents")
            dj_agent, planner_agent = create_agents(self.config)
            self.agent = dj_agent
            self.planner_agent = planner_agent
            compaction = EventsCompactionConfig(compaction_interval=10, overlap_size=2)
            dj_app = App(name="dj_treta", root_agent=dj_agent, events_compaction_config=compaction)
            planner_app = App(name="dj_treta_planner", root_agent=planner_agent, events_compaction_config=compaction)
            self._dj_runner = Runner(app=dj_app, session_service=self._session_service)
            self._planner_runner = Runner(app=planner_app, session_service=self._session_service)
            async def _reinit():
                self._dj_session = await self._session_service.create_session(app_name="dj_treta", user_id="dj")
                self._planner_session = await self._session_service.create_session(app_name="dj_treta_planner", user_id="planner")
            self._run_async(_reinit())
            return f"Source {source} → {'on' if enabled else 'off'} (agents rebuilt)"

        else:
            return f"Unknown: {cmd}"

    def _agent_talk(self, message, cmd_id):
        """One agent, one personality. Always."""
        from .main import _get_status

        try:
            context = self._build_context(_get_status(self.config.mixxx.url))
            history = self._format_history()

            with self._talk_lock:
                result = self._invoke_agent(
                    f"{context}\n\n{history}\n\n"
                    f'The listener says: "{message}"\n\n'
                    f"Respond naturally. Use tools only if they asked you to DO something.",
                    timeout=120, max_calls=20,  # talk needs more room for tool use
                )

            # Update conversation memory
            self._chat_history.append((message, result))
            if len(self._chat_history) > 10:
                self._chat_history = self._chat_history[-10:]

            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Talk done: {result[:500]}")
        except Exception as e:
            self._last_command_id = cmd_id
            self._last_result = f"Error: {e}"
            self._write_state()

    def _agent_skip(self):
        """Skip — direct fast crossfade, no agent needed."""
        from .main import _get_status, _active_idle_decks

        try:
            from .tools import do_transition
            status = _get_status(self.config.mixxx.url)
            if not status:
                self._last_result = "Skip failed: Mixxx offline"
                self._write_state()
                return
            active, idle = _active_idle_decks(status)
            d_idle = status.get(f"deck{idle}", {})

            # Load idle deck if empty
            if not d_idle.get("track_loaded"):
                self._load_next_on_idle(status)
                time.sleep(2)
                status = _get_status(self.config.mixxx.url)
                d_idle = status.get(f"deck{idle}", {}) if status else {}
                if not d_idle.get("track_loaded"):
                    self._last_result = "Skip failed: no track to skip to"
                    self._write_state()
                    return

            # Direct fast crossfade — 15s, no agent decision needed
            result = do_transition(idle, 15)
            self._last_result = f"Skipped to deck {idle}: {str(result)[:100]}"
            self._record_playing_tracks()
            self._write_state()
            log.info(f"Skip: {self._last_result}")
        except Exception as e:
            self._last_result = f"Skip error: {e}"
            self._write_state()
