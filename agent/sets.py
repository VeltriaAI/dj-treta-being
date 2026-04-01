"""Sets mixin — DJ set management, recording, and broadcast control."""

import logging
import time

import httpx

log = logging.getLogger("dj-treta")


class SetsMixin:

    def _start_set(self, mood=None, genre=None, duration=None, title=None):
        """Start a new DJ set. Auto-decides mood/duration/name if not provided."""
        from .db import insert_set, get_next_set_number
        from .config import load_config

        set_id = f"set-{time.strftime('%Y%m%d-%H%M%S')}"
        set_number = get_next_set_number()
        set_mood = mood or self.mood or "melodic-techno"

        # Let the AI name the set
        if not title:
            try:
                from litellm import completion
                cfg = load_config()
                resp = completion(
                    model=cfg.llm.model,
                    messages=[{"role": "user", "content":
                        f"Reply with ONLY a creative 2-4 word name for a {set_mood} DJ set. "
                        f"Examples: Midnight Signal, Dark Matter, Velvet Underground, Neural Drift. "
                        f"No explanation. No quotes. Just the name."}],
                    api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
                    temperature=0.9, timeout=10,
                )
                title = resp.choices[0].message.content.strip()[:50]
            except Exception:
                title = f"Set #{set_number}"

        self.current_set = {
            "id": set_id,
            "set_number": set_number,
            "title": title,
            "started_at": time.time(),
            "mood": set_mood,
            "genre": genre or set_mood or "melodic-techno",
            "target_duration": duration or self.config.sets.default_duration_minutes,
            "tracks": [],
            "energy_arc": [],
            "peak_energy": 0,
            "status": "live",
        }
        insert_set(self.current_set)
        self._start_recording()
        log.info(f"Set started: '{title}' ({set_mood}, {self.current_set['target_duration']}m)")

    def _end_set(self):
        """End current set, stop recording, auto-start new one."""
        if not self.current_set:
            return
        from .db import update_set
        self.current_set["status"] = "finished"
        self.current_set["ended_at"] = time.time()
        self.current_set["track_count"] = len(self.tracks_played)
        self._stop_recording()
        update_set(self.current_set)
        log.info(f"Set ended: {self.current_set['id']} ({len(self.tracks_played)} tracks)")
        # Store finished set for relay to pick up (one final push)
        self.last_finished_set = dict(self.current_set)
        # Auto-start new set
        self._start_set()

    def _check_set_duration(self):
        """Check if current set has reached target duration."""
        if not self.current_set:
            return
        elapsed = (time.time() - self.current_set["started_at"]) / 60
        if elapsed >= self.current_set["target_duration"]:
            self._end_set()

    def _start_recording(self):
        """Start Mixxx recording (if local_recording enabled)."""
        if not self.config.sets.local_recording:
            return
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Recording]", "key": "toggle_recording", "value": 1
            }, timeout=3)
            self._recording_active = True
            log.info("Recording started")
        except Exception as e:
            log.warning(f"Recording start failed: {e}")

    def _stop_recording(self):
        """Stop Mixxx recording (if local_recording enabled)."""
        if not self.config.sets.local_recording:
            return
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Recording]", "key": "toggle_recording", "value": 0
            }, timeout=3)
            self._recording_active = False
            log.info("Recording stopped")
        except Exception as e:
            log.warning(f"Recording stop failed: {e}")

    def _start_broadcast(self):
        """Enable Mixxx Shoutcast broadcast."""
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Shoutcast]", "key": "enabled", "value": 1
            }, timeout=3)
            self._broadcast_active = True
            log.info("Broadcast started")
        except Exception as e:
            log.warning(f"Broadcast start failed: {e}")

    def _stop_broadcast(self):
        """Disable Mixxx Shoutcast broadcast."""
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Shoutcast]", "key": "enabled", "value": 0
            }, timeout=3)
            self._broadcast_active = False
            log.info("Broadcast stopped")
        except Exception as e:
            log.warning(f"Broadcast stop failed: {e}")
