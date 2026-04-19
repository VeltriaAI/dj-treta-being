"""Session mixin — state persistence, context building, and TUI state file."""

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")

STATE_FILE = Path("/tmp/dj-treta-state.json")
PERSIST_FILE = Path(__file__).parent.parent / ".beings" / "session.json"


class SessionMixin:

    def _write_state(self):
        try:
            from .main import _get_status, _active_idle_decks, _count_tracks

            status = _get_status(self.config.mixxx.url)
            current = {"title": "", "bpm": 0, "key": "", "remaining": 0, "file_path": ""}

            if status:
                for dk in [1, 2]:
                    d = status.get(f"deck{dk}", {})
                    if d.get("playing"):
                        try:
                            tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2).json()
                            if tinfo and not tinfo.get("error"):
                                current["title"] = tinfo.get("title", "")
                                current["file_path"] = tinfo.get("file_path", "")
                        except Exception:
                            pass
                        current["bpm"] = d.get("bpm", 0)
                        current["key"] = d.get("key", 0)
                        current["remaining"] = d.get("remaining_seconds", 0)
                        current["position"] = d.get("position_seconds", 0)
                        current["duration"] = d.get("duration", 0)
                        current["file_bpm"] = d.get("file_bpm", 0)
                        current["deck"] = dk
                        break

            # Next track (idle deck)
            next_track = None
            if status:
                _, idle_dk = _active_idle_decks(status)
                d_idle = status.get(f"deck{idle_dk}", {})
                if d_idle.get("track_loaded"):
                    try:
                        tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{idle_dk}/track_info", timeout=2).json()
                        if tinfo and not tinfo.get("error"):
                            next_track = {"title": tinfo.get("title", ""), "deck": idle_dk,
                                          "file_path": tinfo.get("file_path", "")}
                    except Exception:
                        pass

            # Set info
            set_data = {}
            if self.current_set:
                s = self.current_set
                elapsed_secs = time.time() - s["started_at"]
                target_secs = s["target_duration"] * 60
                # Energy arc — compact: last 60 samples (10 min) for TUI sparkline
                arc = s.get("energy_arc", [])
                compact_arc = [{"t": a["t"], "e": a.get("energy", 0)} for a in arc[-60:]]
                set_data = {
                    "id": s["id"],
                    "number": s.get("set_number", 0),
                    "title": s.get("title", ""),
                    "mood": s.get("mood", ""),
                    "genre": s.get("genre", ""),
                    "elapsed": elapsed_secs,
                    "remaining": max(0, target_secs - elapsed_secs),
                    "target_minutes": s["target_duration"],
                    "peak_energy": s.get("peak_energy", 0),
                    "energy_arc": compact_arc,
                }

            # Read billing
            billing_str = ""
            try:
                bf = Path("/tmp/dj-treta-billing.json")
                if bf.exists():
                    b = json.loads(bf.read_text())
                    total_tok = b.get("total_input_tokens", 0) + b.get("total_output_tokens", 0)
                    cost = b.get("total_cost_usd", 0)
                    if total_tok > 1_000_000:
                        billing_str = f"{total_tok/1_000_000:.1f}M tokens ${cost:.3f}"
                    elif total_tok > 0:
                        billing_str = f"{total_tok//1000}K tokens ${cost:.4f}"
            except Exception:
                pass

            phase = "idle"
            if status and (status.get("deck1", {}).get("playing") or status.get("deck2", {}).get("playing")):
                phase = "playing"

            STATE_FILE.write_text(json.dumps({
                "phase": phase,
                "mood": self.mood,
                "tracks_played": len(self.tracks_played),
                "current_track": current,
                "next_track": next_track,
                "set": set_data,
                "planner_status": "busy" if self._planner_busy else "idle",
                "planner_tracks_since": getattr(self, '_tracks_since_plan', 0),
                "agent_busy": self._agent_busy,
                "relay_enabled": self.config.relay.enabled,
                "relay_connected": hasattr(self, 'relay'),
                "recording": self._recording_active,
                "broadcasting": self._broadcast_active,
                "emergency_count": self._emergency_count,
                "last_command": self._last_command,
                "last_command_id": self._last_command_id,
                "last_command_result": self._last_result,
                "billing": billing_str,
                "sources": {
                    "youtube": self.config.sources.youtube,
                    "treta_originals": self.config.sources.treta_originals,
                },
                "producing": self._generation_status,
            }, indent=2))

            # Broadcast state via WebSocket
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("state", {
                    "phase": phase,
                    "mood": self.mood,
                    "tracks_played": len(self.tracks_played),
                    "current_track": current,
                    "next_track": next_track,
                    "set": set_data,
                    "agent_busy": self._agent_busy,
                    "planner_status": "busy" if self._planner_busy else "idle",
                    "emergency_count": self._emergency_count,
                    "billing": billing_str,
                    "sources": {
                        "youtube": self.config.sources.youtube,
                        "treta_originals": self.config.sources.treta_originals,
                    },
                })
        except Exception:
            pass

    def _state_loop(self):
        save_counter = 0
        while self._running:
            self._write_state()
            save_counter += 1
            if save_counter % 5 == 0:
                self._save_session()
            time.sleep(2)

    def _save_session(self):
        """Force a Session flush. Session auto-persists on every mutation;
        this is a belt-and-suspenders checkpoint called every ~10s from
        _state_loop."""
        if hasattr(self, "session"):
            self.session.flush()

    def _restore_session(self):
        """No-op: Session.load() in DJTretaBeing.__init__ already restored
        state from .beings/session.json. Kept for backward compat with
        any external caller."""
        return

    def _build_context(self, status):
        from .main import _count_tracks

        if not status:
            return "Mixxx not responding."

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        src_parts = []
        if self.config.sources.youtube:
            src_parts.append("youtube")
        if self.config.sources.treta_originals:
            src_parts.append("treta_originals")
        parts = [f"Mood: {self.mood or 'not set'}  Sources: {', '.join(src_parts) or 'none'}"]
        parts.append(f"Tracks played: {len(self.tracks_played)}")

        for dk, d in [(1, d1), (2, d2)]:
            if d.get("track_loaded"):
                state = "PLAYING" if d.get("playing") else "LOADED (paused)"
                parts.append(
                    f"Deck {dk}: {state}, {d.get('remaining_seconds', 0):.0f}s remaining, "
                    f"{d.get('bpm', 0):.0f} BPM (file: {d.get('file_bpm', 0):.0f})"
                )
            else:
                parts.append(f"Deck {dk}: empty")

        xf = status.get("crossfader", 0)
        parts.append(f"Crossfader: {xf:.2f} ({'Deck 1' if xf < -0.3 else 'Deck 2' if xf > 0.3 else 'center'})")

        # Compact library listing — saves ~5K tokens vs agent calling list_library_tracks
        parts.append(f"\nLibrary ({_count_tracks(self.config.library.music_path)} tracks):")
        parts.append(self._get_library_summary())

        return "\n".join(parts)

    def _get_library_summary(self) -> str:
        """Compact library: genre/: track1, track2, ..."""
        music_dir = self.config.library.music_path
        if not music_dir.exists():
            return "  (empty)"
        lines = []
        for genre_dir in sorted(music_dir.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            tracks = [f.stem[:40] for f in sorted(genre_dir.iterdir())
                      if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a')]
            if tracks:
                lines.append(f"  {genre_dir.name}/: {', '.join(tracks)}")
        return "\n".join(lines) if lines else "  (empty)"

    def _format_history(self) -> str:
        """Format recent conversation for agent context."""
        if not self._chat_history:
            return ""
        lines = ["Recent conversation:"]
        for user_msg, response in self._chat_history[-5:]:
            lines.append(f"Listener: {user_msg}")
            lines.append(f"DJ Treta: {response[:500]}")
        return "\n".join(lines)

    def _record_playing_tracks(self):
        """Track what's playing for set history + deck start times."""
        from .main import _get_status

        try:
            status = _get_status(self.config.mixxx.url)
            if not status:
                return
            for dk in [1, 2]:
                if status.get(f"deck{dk}", {}).get("playing"):
                    tinfo = httpx.get(
                        f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=3
                    ).json()
                    if tinfo and not tinfo.get("error"):
                        title = tinfo.get("title", "")
                        path = tinfo.get("file_path", "")
                        # Track deck start time — reset when track changes
                        if path and path != self._deck_track.get(dk, ""):
                            self._deck_track[dk] = path
                            self._deck_start_time[dk] = time.time()
                        if title and not any(t.get("title") == title for t in self.tracks_played):
                            # BUG-8 fix (Phase A2 dry run #2 2026-04-19):
                            # include `path` so downstream dedup (BUG-6
                            # played-path filter in planner_loop) can match
                            # canonical library paths stably. Previously
                            # only {title, time} was recorded, so any
                            # path-based comparison matched nothing.
                            self.tracks_played.append({
                                "title": title,
                                "path": path,
                                "time": time.time(),
                            })
                            # Record in DB set_history
                            if self.current_set:
                                from .db import add_track_to_set
                                add_track_to_set(self.current_set["id"], title, dk, path)
        except Exception:
            pass

    def _agent_reflect(self):
        """Periodic self-evolution — reflect on recent tracks."""
        if self._agent_busy:
            return  # skip if agent is already working
        try:
            recent = [t.get("title", "?") for t in self.tracks_played[-5:]]
            self._invoke_agent(
                f"REFLECTION: Last 5 tracks were: {recent}\n"
                f"Use save_learning() to note what worked and what didn't.\n"
                f"Then respond with a brief summary of your learnings."
            )
            log.info("Self-reflection complete")
        except Exception as e:
            log.warning(f"Reflection error: {e}")
