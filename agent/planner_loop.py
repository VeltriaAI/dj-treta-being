"""Planner loop mixin — background track planning and idle deck loading.

v8: planner's output is a structured PlaylistV1 JSON written to
session.playlist (not a markdown blob written to /tmp). Track loading
reads that playlist instead of running SQL filters. See REFACTOR_PLAN.md
§6 Phase 3.
"""

import logging
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")


class PlannerMixin:

    def _planner_loop(self):
        """Background: plan 6 tracks, load idle deck, re-plan every 4 tracks."""
        from .main import _get_status

        self._tracks_since_plan = 0
        last_track = ""
        time.sleep(5)  # let heartbeat boot first
        while self._running:
            try:
                from .observability import tick as _obs_tick
                _obs_tick("planner")
                status = _get_status(self.config.mixxx.url)
                if not status:
                    time.sleep(10)
                    continue

                current_track = self._get_current_track_title(status)

                # Detect track change (transition happened)
                if current_track and current_track != last_track:
                    last_track = current_track
                    self._tracks_since_plan += 1
                    # Immediately load next track on idle deck
                    self._load_next_on_idle(status)
                    # v8 Phase 7: reflection triggering moved to Being's
                    # heartbeat (consciousness loop), not the planner's
                    # responsibility. Planner's job is plan-and-update-playlist
                    # only.

                # v8: playlist lives on session.playlist (structured JSON).
                # Replan when no playlist yet OR enough tracks have elapsed
                # since the last plan OR mood changed (replan_requested
                # signal — populated by Session callback in Phase 4+).
                playlist = getattr(self.session, "playlist", None)
                needs_plan = (
                    not playlist
                    or not playlist.get("tracks")
                    or self._tracks_since_plan >= self.config.planner.replan_every_n_tracks
                    or getattr(self.session, "replan_requested", False)
                )
                if needs_plan and getattr(self.session, "replan_requested", False):
                    self.session.replan_requested = False

                if needs_plan and not self._planner_busy:
                    self._planner_busy = True
                    self._tracks_since_plan = 0
                    try:
                        self._run_planner(status, current_track)
                        # Load after planning
                        status = _get_status(self.config.mixxx.url)
                        if status:
                            self._load_next_on_idle(status)
                    finally:
                        self._planner_busy = False

            except Exception as e:
                import traceback
                log.warning(f"Planner loop error: {type(e).__name__}: {e}")
                log.warning(traceback.format_exc()[:500])
            time.sleep(15)  # 15s — fast enough for short generated tracks (~150s)

    def _run_planner(self, status, current_track):
        """Invoke the planner agent; write its structured JSON playlist to Session.

        v8: planner LLM sees the full analyzed library + state + feedback and
        emits a PlaylistV1 JSON with ranked candidates. No SQL pre-filter — the
        LLM owns selection. On parse/validation failure, session.last_planner_error
        is set and the previous valid playlist remains authoritative (DJ keeps
        mixing with the last good list).
        """
        import json as _json
        from .db import get_track_by_path, get_library_with_metadata
        from .playlist_schema import validate_playlist, PlaylistValidationError
        from .prompts import build_planner_v8_message

        played_list = [t.get("title", "?") for t in self.tracks_played]

        # Current track metadata (for prompt context only — no filtering).
        current_meta = None
        for dk in [1, 2]:
            if status.get(f"deck{dk}", {}).get("playing"):
                try:
                    tinfo = httpx.get(
                        f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2
                    ).json()
                    file_path = tinfo.get("file_path", "")
                    if file_path:
                        current_meta = get_track_by_path(file_path)
                except Exception:
                    pass

        # LLM sees the whole analyzed library — no SQL pre-filter.
        library = get_library_with_metadata(include_unanalyzed=False)

        current_info = "NOTHING — silence!"
        if current_meta:
            bpm = current_meta.get('bpm') or 0
            key = current_meta.get('key_musical') or '?'
            energy = current_meta.get('energy_peak') or '?'
            current_info = f"{current_track} | BPM:{bpm:.0f} Key:{key} Energy:{energy}"

        # Consume transient directive / intent fields (they'll be re-set by Being
        # on next user/agent interaction).
        directive = self.planner_directive or ""
        if directive:
            log.info(f"Planner directive consumed: {directive[:80]}")
            self.planner_directive = ""
        intent = self.user_intent or ""
        if intent:
            self.user_intent = ""

        feedback_line = ""
        try:
            from .db import get_liked_tracks, get_disliked_tracks
            liked = get_liked_tracks(10)
            disliked = get_disliked_tracks(10)
            if liked:
                names = [l["track_title"] for l in liked]
                feedback_line += f"\nLISTENER LIKES: {', '.join(names[:5])}"
            if disliked:
                feedback_line += f"\nLISTENER DISLIKES (AVOID similar): {', '.join(disliked[:5])}"
        except Exception:
            pass

        log.info(
            f"Planner running — current: {current_track or 'nothing'}, "
            f"{len(library)} analyzed library tracks"
        )
        planner_msg = build_planner_v8_message(
            current_info=current_info,
            played_list=played_list,
            library=library,
            mood_profile=getattr(self.session, "mood_profile", None),
            mood=self.mood or "",
            planner_directive=directive,
            user_intent=intent,
            feedback_line=feedback_line,
        )
        result = self._invoke_planner(planner_msg)

        # Parse + validate LLM output. On any failure, keep the previous
        # playlist as authoritative and stash the error for TUI/debugging.
        try:
            raw = (result or "").strip()
            if "```" in raw:
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.split("```")[0].strip()
            data = _json.loads(raw)
            validated = validate_playlist(data)
            self.session.playlist = validated
            self.session.playlist_updated_at = validated["planned_at"]
            self.session.last_planner_error = ""
            log.info(
                f"Planner wrote playlist: {len(validated['tracks'])} candidates, "
                f"mood_snapshot={validated.get('mood_snapshot', '')}"
            )
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("log", {
                    "text": f"Planner: {len(validated['tracks'])} candidates planned"
                })
        except (ValueError, PlaylistValidationError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log.warning(f"Planner output invalid — keeping last good playlist. {msg}")
            self.session.last_planner_error = msg

    def _load_next_on_idle(self, status):
        """Apply the planner's playlist to the idle deck.

        v8: pure executor. Planner's LLM decides which track plays next
        (session.playlist[0]); this function just loads it on the idle deck.
        All SQL filtering / mood soft-matching / genre override / randomness
        that used to live here is deleted — the LLM sees the full analyzed
        library and picks.

        Falls back to ANY library track (random) only as a last-resort safety
        net when the planner hasn't produced a playlist yet (e.g. during the
        first 15 seconds of daemon startup before the planner ticks).
        """
        from .main import _active_idle_decks
        from .playback_applier import load_on_deck, get_deck_paths, refresh_duration
        from .playlist_schema import pick_next_candidate

        active_deck, idle_deck = _active_idle_decks(status)
        d_idle = status.get(f"deck{idle_deck}", {})

        # Skip if idle already has a fresh track (>60s remaining).
        if d_idle.get("track_loaded") and float(d_idle.get("remaining_seconds", 0) or 0) > 60:
            return

        deck_paths = get_deck_paths(self.config.mixxx.url)
        exclude_paths = {p for p in deck_paths.values() if p}
        played_titles = [t.get("title", "") for t in self.tracks_played]

        # Primary path: trust the planner's session.playlist.
        playlist = getattr(self.session, "playlist", None)
        pick = pick_next_candidate(playlist, exclude_paths, played_titles)

        if pick is None:
            # Last-resort safety net. No SQL filter, no mood match, no
            # originals preference — just keep music alive until the planner
            # ticks. Planner owns selection; this only fires during startup.
            from .db import get_db
            db = get_db()
            try:
                rows = [dict(r) for r in db.execute(
                    "SELECT path, title FROM tracks ORDER BY RANDOM() LIMIT 20"
                ).fetchall()]
            finally:
                db.close()
            available = [r for r in rows
                         if r.get("path") not in exclude_paths
                         and r.get("title") not in played_titles]
            if not available:
                log.warning("No tracks available to load on idle deck")
                return
            pick = available[0]
            log.info(f"Idle load: using fallback (no playlist yet)")

        track_path = pick.get("path")
        if not track_path:
            log.warning("Playlist pick had no path")
            return

        ok = load_on_deck(self.config.mixxx.url, idle_deck, track_path)
        if not ok:
            return
        title_display = pick.get("title") or Path(track_path).stem
        if hasattr(self, '_ws_broadcast'):
            self._ws_broadcast(
                "log", {"text": f"Loaded deck {idle_deck}: {title_display[:50]}"}
            )
        refresh_duration(self.config.mixxx.url, idle_deck, track_path)

    def _auto_load_track(self, filepath):
        """Load a freshly generated track on the idle deck."""
        from .main import _get_status, _active_idle_decks

        status = _get_status(self.config.mixxx.url)
        if not status:
            return
        _, idle_deck = _active_idle_decks(status)
        try:
            result = httpx.post(
                f"{self.config.mixxx.url}/api/load",
                json={"deck": idle_deck, "track": filepath}, timeout=5
            ).json()
            if result.get("ok"):
                log.info(f"Auto-loaded generated track on deck {idle_deck}: {Path(filepath).stem[:50]}")
        except Exception as e:
            log.warning(f"Auto-load failed: {e}")

    def _get_current_track_title(self, status) -> str:
        """Get the title of the currently playing track."""
        for dk in [1, 2]:
            if status.get(f"deck{dk}", {}).get("playing"):
                try:
                    tinfo = httpx.get(
                        f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2
                    ).json()
                    return tinfo.get("title", "")
                except Exception:
                    pass
        return ""

