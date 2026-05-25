"""Sets mixin — DJ set management, recording, and broadcast control."""

import logging
import time
from typing import Optional

import httpx

# --- E4: lazy import so state_sequence doesn't slow cold starts ---
_StateSequence: Optional[type] = None

def _get_state_sequence_class():
    global _StateSequence
    if _StateSequence is None:
        from .state_sequence import StateSequence as _SS
        _StateSequence = _SS
    return _StateSequence

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
        # --- E4: initialise a fresh StateSequence for this set ---
        self._e4_state_sequence = _get_state_sequence_class()()
        # Mirror into session so the TUI / relay can observe it.
        from .session_state import get_session
        s = get_session()
        if s is not None:
            s.current_state_sequence = []
        log.info(f"Set started: '{title}' ({set_mood}, {self.current_set['target_duration']}m)")

    def _end_set(self):
        """End current set, stop recording, auto-start new one."""
        if not self.current_set:
            return
        from .db import update_set
        self.current_set["status"] = "finished"
        ended_at = time.time()
        self.current_set["ended_at"] = ended_at
        self.current_set["track_count"] = len(self.tracks_played)
        self._stop_recording()
        update_set(self.current_set)
        log.info(f"Set ended: {self.current_set['id']} ({len(self.tracks_played)} tracks)")

        # --- E4: persist state sequence to archive ---
        seq = getattr(self, "_e4_state_sequence", None)
        if seq is None:
            seq = _get_state_sequence_class()()
        recording_path = getattr(self, "_recording_path", "")
        try:
            from .state_sequence import archive_set as _archive_set
            archive_path = _archive_set(
                set_id=self.current_set["id"],
                started_at=self.current_set["started_at"],
                ended_at=ended_at,
                mood=self.current_set.get("mood", ""),
                state_sequence=seq,
                tracks_played=list(self.tracks_played),
                recording_path=recording_path,
            )
            # Update session so TUI / relay can surface the archive path.
            from .session_state import get_session
            s = get_session()
            if s is not None:
                s.set_archive_path = str(archive_path)
                s.last_archived_set_id = self.current_set["id"]
                s.current_state_sequence = None   # clear — set is over
        except Exception as exc:
            log.warning(f"E4 archive_set failed: {exc}")
        self._e4_state_sequence = None

        # Store finished set for relay to pick up (one final push)
        self.last_finished_set = dict(self.current_set)
        # Auto-start new set
        self._start_set()

    # --- E4: mixer-state recording helper (called by heartbeat or transitions) --

    def _e4_record_state(
        self,
        status_dict: Optional[dict] = None,
        bar_duration: int = 4,
        label: str = "",
        force: bool = False,
    ) -> None:
        """Snapshot the current mixer state into the live StateSequence.

        Called by the heartbeat mixin after every meaningful mixer change
        (transition fire, EQ adjustment, etc.).  If `status_dict` is None,
        fetches /api/status directly.

        Args:
            status_dict: pre-fetched /api/status dict, or None to fetch now.
            bar_duration: how many bars this state holds (planner/E1 fills in).
            label: annotation for the TUI / archive viewer.
            force: record even if below thresholds.
        """
        seq = getattr(self, "_e4_state_sequence", None)
        if seq is None:
            return   # no set active

        try:
            if status_dict is not None:
                new_state = seq.record(
                    status_dict, bar_duration=bar_duration, label=label, force=force,
                )
            else:
                new_state = seq.record_now(
                    bar_duration=bar_duration, label=label, force=force,
                )
            if new_state is not None:
                # Mirror serialized form into session so observers see it.
                from .session_state import get_session
                s = get_session()
                if s is not None:
                    s.current_state_sequence = seq.to_dict()
        except Exception as exc:
            log.warning(f"_e4_record_state failed: {exc}")

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
