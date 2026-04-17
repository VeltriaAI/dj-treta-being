"""Heartbeat mixin — monitors deck state and triggers transitions.
# v7.0 Evolution Protocol — self-improving DJ

Priority order:
1. Silence → emergency recovery
2. Track ending + idle ready → auto-transition
3. Scheduled transition file → execute it
4. Agent decides (past 50% played)
5. Backup load (idle empty, past threshold)
"""

import json
import logging
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")


class HeartbeatMixin:

    def _heartbeat(self):
        """Pure Python heartbeat. Reads mix_out from DB. No flags, no timers."""
        from .main import _get_status, _active_idle_decks, _ensure_mixxx

        status = _get_status(self.config.mixxx.url)
        if not status:
            _ensure_mixxx(self.config)
            return

        active_deck, idle_deck = _active_idle_decks(status)
        d_active = status.get(f"deck{active_deck}", {})
        d_idle = status.get(f"deck{idle_deck}", {})
        position = float(d_active.get("position_seconds", 0) or 0)
        remaining = float(d_active.get("remaining_seconds", 0) or 0)
        playing = d_active.get("playing", False)
        idle_loaded = d_idle.get("track_loaded", False)
        idle_remaining = float(d_idle.get("remaining_seconds", 0) or 0)

        nothing_playing = (not status.get("deck1", {}).get("playing")
                           and not status.get("deck2", {}).get("playing"))

        # === PRIORITY 1: SILENCE — emergency recovery (ALWAYS runs, even if agent busy) ===
        if nothing_playing:
            self._next_sleep = 5
            # Emergency runs regardless of _agent_busy — music must never stop
            if not getattr(self, '_emergency_running', False):
                self._emergency_running = True
                threading.Thread(target=self._emergency_play, daemon=True).start()
            return

        idle_ready = idle_loaded and idle_remaining > 60
        duration = float(d_active.get("duration", 0) or 0)

        # === PRIORITY 2: Auto-transition when track about to end ===
        # If track ending soon, idle deck ready → just do it
        # Skip if transition already in progress (prevents double auto-transition #66)
        if (idle_ready and remaining < 30 and remaining > 0 and playing
                and not self._agent_busy and not self._transition_pending):
            log.info(f"Auto-transition: {remaining:.0f}s left, crossfading to deck {idle_deck}")
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("log", {"text": f"Auto-transition: {remaining:.0f}s left, crossfading to deck {idle_deck}"})
            from .tools import do_transition
            self._transition_pending = True
            def _auto():
                try:
                    result = do_transition(idle_deck, 15)
                    log.info(f"Auto-transition result: {str(result)[:200]}")
                    if self.current_set and isinstance(self.current_set.get("energy_arc"), list):
                        self.current_set["energy_arc"].append({
                            "t": round(time.time() - self.current_set["started_at"]),
                            "event": "transition", "technique": "auto_crossfade", "to_deck": idle_deck,
                        })
                    self._record_playing_tracks()
                except Exception as e:
                    log.error(f"Auto-transition error: {e}")
                finally:
                    self._transition_pending = False
            threading.Thread(target=_auto, daemon=True).start()
            self._next_sleep = 5
            return

        # === PRIORITY 3: Execute scheduled transition (Python handles timing) ===
        if not self._transition_pending:
            sched_file = Path("/tmp/dj-treta-scheduled-transition.json")
            if sched_file.exists():
                try:
                    sched = json.loads(sched_file.read_text())
                    sched_file.unlink(missing_ok=True)  # delete BEFORE starting executor (#67)
                    self._transition_pending = True
                    threading.Thread(
                        target=self._execute_scheduled_transition,
                        args=(sched,), daemon=True
                    ).start()
                except Exception as e:
                    log.warning(f"Bad scheduled transition file: {e}")
                    sched_file.unlink(missing_ok=True)

        # === PRIORITY 4: Agent decides transition (Software 3.0) ===
        # Only ask after 50% played (saves tokens) and when idle deck ready.
        # Don't ask if transition is already pending OR already scheduled —
        # v8 Phase 4 adds the sched_file check to kill wasteful repeat
        # invocations (old P4 fired every 15s even when a transition was
        # already locked, producing 4x log amplification with no actual
        # second tool call).
        sched_file_exists = Path("/tmp/dj-treta-scheduled-transition.json").exists()
        if (idle_ready and duration > 0 and position > (duration * 0.5)
                and not self._agent_busy and not self._transition_pending
                and not sched_file_exists):
            from .db import get_track_by_path

            # Get metadata for both tracks
            active_meta = None
            idle_meta = None
            active_file = ""
            idle_file = ""
            try:
                tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{active_deck}/track_info", timeout=2).json()
                active_file = tinfo.get("file_path", "")
                active_meta = get_track_by_path(active_file) if active_file else None
            except Exception:
                pass
            try:
                tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{idle_deck}/track_info", timeout=2).json()
                idle_file = tinfo.get("file_path", "")
                idle_meta = get_track_by_path(idle_file) if idle_file else None
            except Exception:
                pass

            # Get track names
            active_track = active_meta.get("title", "") if active_meta else ""
            idle_track = idle_meta.get("title", "") if idle_meta else ""

            # Build context with timelines
            active_section = self._get_current_section(active_meta, position)
            active_timeline = self._format_timeline(active_meta)
            idle_timeline = self._format_timeline(idle_meta)
            active_bpm = d_active.get("bpm", 0)
            active_file_bpm = d_active.get("file_bpm", 0) or active_bpm
            idle_bpm = d_idle.get("bpm", 0)
            idle_file_bpm = d_idle.get("file_bpm", 0) or idle_bpm
            active_key = active_meta.get("key_musical", "?") if active_meta else "?"
            idle_key = idle_meta.get("key_musical", "?") if idle_meta else "?"

            from .prompts import build_dj_user_message

            instruction = build_dj_user_message(
                active_track=active_track,
                position=position,
                duration=duration,
                remaining=remaining,
                active_bpm=active_bpm,
                active_file_bpm=active_file_bpm,
                active_key=active_key,
                active_section=active_section,
                active_timeline=active_timeline,
                idle_track=idle_track,
                idle_deck=idle_deck,
                idle_bpm=idle_bpm,
                idle_file_bpm=idle_file_bpm,
                idle_key=idle_key,
                idle_timeline=idle_timeline,
                transition_pending=self._transition_pending,
                dj_directive=self.dj_directive,
                playlist=getattr(self.session, "playlist", None),
                mood_profile=getattr(self.session, "mood_profile", None),
            )

            self._agent_busy = True

            def _run():
                try:
                    result = self._invoke_agent(instruction, fresh_session=True)
                    log.info(f"DJ decision: {result[:500]}")
                    if hasattr(self, '_ws_broadcast'):
                        self._ws_broadcast("log", {"text": f"DJ decision: {result[:200]}"})
                    # Clear directive after DJ has read it
                    if self.dj_directive:
                        log.info(f"DJ directive consumed: {self.dj_directive[:80]}")
                        self.dj_directive = ""
                    self._record_playing_tracks()
                    self._check_set_duration()
                except Exception as e:
                    import traceback
                    log.error(f"DJ decision error: {type(e).__name__}: {e}")
                    log.error(traceback.format_exc()[:500])
                finally:
                    self._agent_busy = False

            threading.Thread(target=_run, daemon=True).start()
            self._next_sleep = 15
            return

        # v8 Phase 7: P5 backup-load DELETED. DJ owns deck loading via its
        # load_track tool, reading session.playlist. If the DJ agent fails
        # to load and silence looms, P1 silence recovery catches it with an
        # emergency play. That's the safety invariant — music never stops —
        # without Python making selection decisions.

        # === Everything fine — dynamic sleep ===
        if duration > 0 and position < (duration * 0.5):
            # First half: sleep longer
            time_until_half = (duration * 0.5) - position
            self._next_sleep = min(15, max(5, time_until_half / 3))
        elif remaining > 120:
            self._next_sleep = min(15, max(5, remaining / 10))
        else:
            self._next_sleep = 5

        self._record_playing_tracks()
        self._check_set_duration()

    def _format_timeline(self, meta) -> str:
        """Format track timeline for agent prompt."""
        if not meta:
            return "(no analysis)"
        timeline_str = meta.get("timeline", "")
        if not timeline_str:
            return f"BPM:{meta.get('bpm','?')} Key:{meta.get('key_musical','?')} Energy:{meta.get('energy_peak','?')}"
        try:
            import json as _json
            sections = _json.loads(timeline_str) if isinstance(timeline_str, str) else timeline_str
            parts = [f"{s['start']}s-{s['end']}s {s['section']}(energy:{s['energy']})" for s in sections]
            return " → ".join(parts)
        except Exception:
            return "(analysis error)"

    def _get_current_section(self, meta, position) -> str:
        """What section is the track currently in?"""
        if not meta or not meta.get("timeline"):
            return "unknown"
        try:
            import json as _json
            sections = _json.loads(meta["timeline"]) if isinstance(meta["timeline"], str) else meta["timeline"]
            for s in sections:
                if float(s["start"]) <= position <= float(s["end"]):
                    return f"{s['section']} (energy:{s['energy']}, {s['start']}s-{s['end']}s)"
            return "past end"
        except Exception:
            return "unknown"

    def _emergency_play(self):
        """Silence! Direct API play first (fast + reliable), agent fallback for empty library."""
        from .main import _get_status

        self._emergency_count += 1
        try:
            url = self.config.mixxx.url

            # Try direct API first — pick any track from library, load, play
            import glob
            all_tracks = glob.glob(str(self.config.library.music_path / "**/*.mp3"), recursive=True)
            # Prefer originals when youtube source is off
            if not self.config.sources.youtube and self.config.sources.treta_originals:
                tracks = [t for t in all_tracks if "DJ Treta" in Path(t).name]
                if not tracks:
                    tracks = all_tracks  # fallback — music never stops
            else:
                tracks = all_tracks
            if tracks:
                import random
                track = random.choice(tracks)
                # Load with retry — Mixxx may not be ready right after boot
                for attempt in range(3):
                    httpx.post(f"{url}/api/load", json={"deck": 1, "track": track}, timeout=5)
                    time.sleep(2)
                    st = _get_status(url)
                    if st and st.get("deck1", {}).get("track_loaded"):
                        break
                    log.warning(f"Emergency load attempt {attempt+1} — not loaded yet")
                    time.sleep(3)

                # Reset rate to native — emergency tracks should play at file BPM
                httpx.post(f"{url}/api/control",
                           json={"group": "[Channel1]", "key": "rate_ratio", "value": 1.0}, timeout=3)
                httpx.post(f"{url}/api/control",
                           json={"group": "[Channel1]", "key": "sync_enabled", "value": 0}, timeout=3)
                httpx.post(f"{url}/api/play", json={"deck": 1}, timeout=3)
                httpx.post(f"{url}/api/crossfade", json={"position": 0.0}, timeout=3)
                time.sleep(2)
                log.info(f"Emergency play: {Path(track).stem[:50]} (rate reset)")
                self._record_playing_tracks()
                return

            # Empty library — generate directly (bypass agent to avoid blocking)
            if self.config.sources.treta_originals:
                log.info("Emergency: generating track directly (no agent)")
                from .tools import generate_track as _gen
                result = _gen(
                    prompt=f"Atmospheric {self.mood or 'melodic-techno'} track with driving rhythm and evolving textures",
                    bpm=125, key="A minor", genre=self.mood or "melodic-techno",
                    duration="full", name="Emergency Pulse",
                )
                log.info(f"Emergency generate: {result[:200]}")
                # Try to load + play the generated track
                if "Generated:" in result:
                    filepath = result.split("Generated: ")[1].split(" |")[0]
                    httpx.post(f"{url}/api/load", json={"deck": 1, "track": filepath}, timeout=5)
                    time.sleep(2)
                    httpx.post(f"{url}/api/play", json={"deck": 1}, timeout=3)
                    httpx.post(f"{url}/api/crossfade", json={"position": 0.0}, timeout=3)
                    log.info(f"Emergency play: {Path(filepath).stem[:50]}")
                    self._record_playing_tracks()
            elif self.config.sources.youtube:
                result = self._invoke_agent(
                    f"{self._build_context(_get_status(url))}\n\n"
                    f"SILENCE! Empty library. Search YouTube, download a {self.mood or 'melodic-techno'} track, "
                    f"load on deck 1, play it, set crossfader to 0.0."
                )
                log.info(f"Emergency play (agent): {result[:200]}")
                self._record_playing_tracks()
            self._record_playing_tracks()
        except Exception as e:
            import traceback
            log.error(f"Emergency play error: {type(e).__name__}: {e}")
            log.error(traceback.format_exc()[:500])
        finally:
            self._emergency_running = False
            self._agent_busy = False
