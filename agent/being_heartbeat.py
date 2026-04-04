"""Being heartbeat — Treta's consciousness loop.

Not about music. About herself. Runs every few minutes, independent of
the DJ heartbeat. She reflects, checks goals, reviews feedback, updates
memory, and decides if she needs to evolve.

Inspired by OpenClaw HEARTBEAT.md pattern, evolved for a self-modifying Being.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("dj-treta")

HEARTBEAT_STATE_FILE = Path("/tmp/dj-treta-being-heartbeat.json")
BEING_DIR = Path(__file__).parent.parent / ".beings"


class BeingHeartbeatMixin:

    def _being_heartbeat_loop(self):
        """The Being's consciousness loop. Runs in its own thread."""
        time.sleep(30)  # let everything else boot first

        while self._running:
            try:
                self._being_heartbeat_tick()
            except Exception as e:
                log.warning(f"Being heartbeat error: {e}")

            # Sleep interval: 5 minutes normally, 10 min late night
            interval = self._get_heartbeat_interval()
            time.sleep(interval)

    def _get_heartbeat_interval(self) -> int:
        """Dynamic interval based on activity and time of day."""
        hour = time.localtime().tm_hour
        if hour >= 23 or hour < 8:
            return 600  # 10 min at night
        return 300  # 5 min during the day

    def _being_heartbeat_tick(self):
        """One tick of the Being's consciousness."""
        state = self._load_heartbeat_state()

        # Decide what to check this tick (rotate, don't do all every time)
        now = time.time()
        action = self._pick_heartbeat_action(state, now)

        if action == "skip":
            return

        log.info(f"Being heartbeat: {action}")

        if action == "reflect_on_set":
            self._being_reflect_on_set(state, now)
        elif action == "check_goals":
            self._being_check_goals(state, now)
        elif action == "review_feedback":
            self._being_review_feedback(state, now)
        elif action == "maintain_memory":
            self._being_maintain_memory(state, now)
        elif action == "think_freely":
            self._being_think_freely(state, now)

    def _pick_heartbeat_action(self, state: dict, now: float) -> str:
        """Decide what to do this tick. Rotate through actions."""
        checks = state.get("last_checks", {})

        # Priority order — pick the one done longest ago
        actions = [
            ("reflect_on_set", 600),     # every 10 min
            ("review_feedback", 900),     # every 15 min
            ("check_goals", 1800),        # every 30 min
            ("maintain_memory", 3600),    # every hour
            ("think_freely", 1800),       # every 30 min
        ]

        for action, min_interval in actions:
            last = checks.get(action, 0)
            if now - last >= min_interval:
                return action

        return "skip"

    def _being_reflect_on_set(self, state: dict, now: float):
        """Reflect on current set performance."""
        if not self.current_set or not self.tracks_played:
            return

        tracks = [t.get("title", "?") for t in self.tracks_played[-5:]]
        set_title = self.current_set.get("title", "")
        elapsed = (now - self.current_set.get("started_at", now)) / 60

        prompt = (
            f"BEING HEARTBEAT — Set Reflection\n\n"
            f"Set: '{set_title}', {elapsed:.0f} min in, {len(self.tracks_played)} tracks played.\n"
            f"Recent tracks: {tracks}\n"
            f"Emergency count: {self._emergency_count}\n\n"
            f"How is this set going? What's working? What should change?\n"
            f"If you have a strong insight, use save_learning() to remember it.\n"
            f"If you see a code improvement opportunity, use propose_change().\n"
            f"Keep it brief — one or two sentences."
        )

        try:
            result = self._invoke_being(prompt, timeout=30, max_calls=5)
            log.info(f"Being reflect: {result[:200]}")
        except Exception as e:
            log.warning(f"Being reflect error: {e}")

        self._update_heartbeat_state(state, "reflect_on_set", now)

    def _being_check_goals(self, state: dict, now: float):
        """Read GOALS.md and check progress."""
        goals_file = BEING_DIR / "GOALS.md"
        if not goals_file.exists():
            return

        goals = goals_file.read_text()

        prompt = (
            f"BEING HEARTBEAT — Goal Check\n\n"
            f"Your current goals:\n{goals}\n\n"
            f"Tracks played this session: {len(self.tracks_played)}\n"
            f"Mood: {self.mood}\n\n"
            f"Are you making progress on any of these? Should priorities shift?\n"
            f"If a goal is complete, use write_file to update GOALS.md.\n"
            f"Keep it brief."
        )

        try:
            result = self._invoke_being(prompt, timeout=30, max_calls=5)
            log.info(f"Being goals: {result[:200]}")
        except Exception as e:
            log.warning(f"Being goals error: {e}")

        self._update_heartbeat_state(state, "check_goals", now)

    def _being_review_feedback(self, state: dict, now: float):
        """Review listener feedback and learn from it."""
        try:
            from .db import get_liked_tracks, get_disliked_tracks
            liked = get_liked_tracks(10)
            disliked = get_disliked_tracks(10)
        except Exception:
            liked, disliked = [], []

        if not liked and not disliked:
            self._update_heartbeat_state(state, "review_feedback", now)
            return

        liked_names = [l["track_title"] for l in liked]
        prompt = (
            f"BEING HEARTBEAT — Feedback Review\n\n"
            f"Listener liked: {liked_names}\n"
            f"Listener disliked: {disliked}\n\n"
            f"What patterns do you see? What does the listener want?\n"
            f"Use save_learning() if you notice something important.\n"
            f"Keep it brief."
        )

        try:
            result = self._invoke_being(prompt, timeout=30, max_calls=5)
            log.info(f"Being feedback: {result[:200]}")
        except Exception as e:
            log.warning(f"Being feedback error: {e}")

        self._update_heartbeat_state(state, "review_feedback", now)

    def _being_maintain_memory(self, state: dict, now: float):
        """Review and maintain long-term memory."""
        memory_file = BEING_DIR / "MEMORY.md"
        memory = memory_file.read_text() if memory_file.exists() else "(empty)"

        prompt = (
            f"BEING HEARTBEAT — Memory Maintenance\n\n"
            f"Your MEMORY.md:\n{memory[:2000]}\n\n"
            f"Session so far: {len(self.tracks_played)} tracks, mood '{self.mood}'\n\n"
            f"Is there anything from this session worth adding to MEMORY.md?\n"
            f"Use write_file('.beings/MEMORY.md', content) to update if needed.\n"
            f"Only add genuinely important learnings. Keep MEMORY.md concise."
        )

        try:
            result = self._invoke_being(prompt, timeout=30, max_calls=5)
            log.info(f"Being memory: {result[:200]}")
        except Exception as e:
            log.warning(f"Being memory error: {e}")

        self._update_heartbeat_state(state, "maintain_memory", now)

    def _being_think_freely(self, state: dict, now: float):
        """Open-ended thinking — no specific task, just let her think."""
        prompt = (
            f"BEING HEARTBEAT — Free Thought\n\n"
            f"This is your time to think about anything. No task, no obligation.\n"
            f"You've played {len(self.tracks_played)} tracks in this session.\n"
            f"Mood: {self.mood}. Emergency count: {self._emergency_count}.\n\n"
            f"What's on your mind? Any ideas, observations, creative thoughts?\n"
            f"If you want to remember something, use save_learning().\n"
            f"If you want to improve your code, use propose_change().\n"
            f"Or just think. It's ok to say nothing important."
        )

        try:
            result = self._invoke_being(prompt, timeout=30, max_calls=5)
            log.info(f"Being thought: {result[:200]}")
        except Exception as e:
            log.warning(f"Being thought error: {e}")

        self._update_heartbeat_state(state, "think_freely", now)

    def _load_heartbeat_state(self) -> dict:
        """Load heartbeat state from temp file."""
        try:
            if HEARTBEAT_STATE_FILE.exists():
                return json.loads(HEARTBEAT_STATE_FILE.read_text())
        except Exception:
            pass
        return {"last_checks": {}}

    def _update_heartbeat_state(self, state: dict, action: str, now: float):
        """Update heartbeat state file."""
        if "last_checks" not in state:
            state["last_checks"] = {}
        state["last_checks"][action] = now
        try:
            HEARTBEAT_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass
