"""Session mixin — state persistence, context building, and TUI state file."""

import json
import logging
import time
from pathlib import Path

import httpx
from .audio_files import is_audio_file
from .runtime_paths import runtime_path

log = logging.getLogger("dj-treta")


def _format_timeline_compact_plain(timeline_json, current_pos: float) -> str:
    """Plain-text section map: INTRO(1) → BREAKDOWN(2) → »BUILDUP(7)« → ...

    The current section is wrapped in »...« so the cockpit can show it without
    needing rich-text markup. Mirrors the TUI's _format_timeline_compact.
    """
    try:
        sections = json.loads(timeline_json) if isinstance(timeline_json, str) else timeline_json
        if not sections:
            return ""
        parts = []
        for s in sections:
            label = f"{str(s['section']).upper()}({s['energy']})"
            if float(s["start"]) <= current_pos <= float(s["end"]):
                label = f"»{label}«"
            parts.append(label)
        return " → ".join(parts)
    except Exception:
        return ""


STATE_FILE = runtime_path("state.json")
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

            # Section timeline (INTRO→BREAKDOWN→...) for the cockpit header.
            if current.get("file_path"):
                try:
                    from .db import get_track_by_path
                    _tk = get_track_by_path(current["file_path"])
                    if _tk and _tk.get("timeline"):
                        current["timeline_compact"] = _format_timeline_compact_plain(
                            _tk["timeline"], current.get("position", 0))
                except Exception:
                    pass

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
                bf = runtime_path("billing.json")
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

            # Pull scheduled transition into state so the TUI doesn't have to
            # also poll scheduled-transition.json over the WebSocket transport.
            sched_transition = None
            try:
                sf = runtime_path("scheduled-transition.json")
                if sf.exists():
                    sched_transition = json.loads(sf.read_text())
            except Exception:
                sched_transition = None

            # Sarathi: surface the latest live transition suggestion so a TUI
            # attaching mid-window renders the panel without waiting for the
            # next broadcast event.
            sarathi_mode = bool(getattr(self.session, "sarathi_mode", False))
            pending_suggestion = None
            if sarathi_mode:
                try:
                    from .tools.sarathi import list_pending_suggestions
                    pend = list_pending_suggestions()
                    if pend:
                        pending_suggestion = pend[-1]
                except Exception:
                    pending_suggestion = None

            state_payload = {
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
                "scheduled_transition": sched_transition,
                "sarathi_mode": sarathi_mode,
                "pending_suggestion": pending_suggestion,
                "sources": {
                    "youtube": self.config.sources.youtube,
                    "treta_originals": self.config.sources.treta_originals,
                },
                "producing": self._generation_status,
            }
            STATE_FILE.write_text(json.dumps(state_payload, indent=2))

            # Keep the in-Mixxx sidebar's Library/Planned/Suggestions symlink
            # folders in sync (best-effort, throttled to ~every 5th tick).
            self._browse_sync_ctr = getattr(self, "_browse_sync_ctr", 0) + 1
            if self._browse_sync_ctr % 5 == 1:
                try:
                    from .browse_folders import sync_browse_folders
                    sync_browse_folders(self.config.library.music_dir, self.session)
                except Exception:
                    pass

            # Broadcast state via WebSocket — same shape as STATE_FILE so the
            # TUI's state-source can treat WS frames and disk reads identically.
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("state", state_payload)
                # Edge event: emit transition_scheduled exactly when a new
                # schedule appears so consumers can react without diffing
                # the periodic state stream.
                last_sched = getattr(self, '_last_broadcast_sched_id', None)
                cur_sched_id = None
                if sched_transition:
                    cur_sched_id = (
                        sched_transition.get("toDeck"),
                        sched_transition.get("atPosition"),
                        sched_transition.get("technique"),
                    )
                if cur_sched_id and cur_sched_id != last_sched:
                    self._ws_broadcast("transition_scheduled", sched_transition)
                self._last_broadcast_sched_id = cur_sched_id
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
                      if is_audio_file(f)]
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
                            # Enriched per-track ledger record. Foundation
                            # for Tier 1.2 of evolution plan: set archives,
                            # reflection, listener-pattern learning all
                            # read from this list. The legacy {title, path,
                            # time} keys are preserved for back-compat;
                            # new keys are additive.
                            entry = {
                                "title": title,
                                "path": path,
                                "time": time.time(),                # legacy
                                # Enriched fields:
                                "track_id": f"t{int(time.time() * 1000)}",
                                "artist": tinfo.get("artist", "") or "",
                                "deck": dk,
                                "loaded_at": time.time(),
                                "played_from_at": time.time(),
                                "ended_at": None,
                                "bpm": status.get(f"deck{dk}", {}).get("bpm", 0) or 0,
                                "key_camelot": tinfo.get("key", "") or "",
                                "energy": tinfo.get("energy_peak"),
                                "transition_in": None,   # set by transition tools post-flight
                                "transition_out": None,
                                "listener_feedback": None,
                            }
                            self.tracks_played.append(entry)
                            # Record in DB set_history
                            if self.current_set:
                                from .db import add_track_to_set
                                add_track_to_set(self.current_set["id"], title, dk, path)
                            # Best-effort archive: when in-memory list grows
                            # past 200, slice old half to JSONL on disk so
                            # the live list stays fast. See archive helper.
                            if len(self.tracks_played) > 200:
                                try:
                                    self._archive_old_tracks()
                                except Exception as exc:
                                    log.debug(f"tracks_played archive skipped: {exc}")
        except Exception:
            pass

    def _archive_old_tracks(self):
        """Move oldest half of tracks_played to a daily JSONL archive.

        Keeps the in-memory list bounded so reads stay fast. Archive
        files at ~/.beings/dj-treta/history/YYYY-MM-DD.jsonl. Idempotent:
        re-running on the same day appends; never rewrites.
        """
        if len(self.tracks_played) <= 200:
            return
        archive_dir = (
            Path(__file__).parent.parent / ".beings" / "dj-treta" / "history"
        )
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Slice off the oldest half.
        half = len(self.tracks_played) // 2
        old_entries = list(self.tracks_played[:half])
        keep_entries = list(self.tracks_played[half:])

        # Group by date (UTC for stability).
        from collections import defaultdict
        from datetime import datetime, timezone
        by_date = defaultdict(list)
        for e in old_entries:
            ts = e.get("loaded_at") or e.get("time") or time.time()
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            by_date[day].append(e)

        for day, entries in by_date.items():
            path = archive_dir / f"{day}.jsonl"
            with path.open("a") as f:
                for e in entries:
                    f.write(json.dumps(e, default=str) + "\n")

        # Replace in-memory list (use clear+extend so ObservedList fires).
        self.tracks_played.clear()
        self.tracks_played.extend(keep_entries)
        log.info(
            f"[history] archived {len(old_entries)} tracks "
            f"to {archive_dir}, kept {len(keep_entries)} in memory"
        )

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
