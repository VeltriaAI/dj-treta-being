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
                    # Phase A2: planner no longer loads the idle deck — it
                    # emits a signal that the DJ agent consumes on the next
                    # heartbeat P4 tick. Watchdog P2 is the Python fallback
                    # if DJ hangs.
                    self.session.idle_needs_load = True
                    # v8 Phase 7: reflection triggering moved to Being's
                    # heartbeat (consciousness loop), not the planner's
                    # responsibility. Planner's job is plan-and-update-playlist
                    # only.

                # v8: playlist lives on session.playlist (structured JSON).
                # Replan when no playlist yet OR enough tracks have elapsed
                # since the last plan OR mood changed (replan_requested
                # signal — populated by Session callback in Phase 4+) OR
                # the current playlist is stale (contains already-played
                # tracks, BUG-9 fix). The staleness check catches the
                # post-daemon-restart case where session.playlist persisted
                # but _tracks_since_plan reset to 0, so the regular
                # threshold never triggers a refresh.
                playlist = getattr(self.session, "playlist", None)
                playlist_is_stale = self._playlist_contains_played(playlist)
                needs_plan = (
                    not playlist
                    or not playlist.get("tracks")
                    or self._tracks_since_plan >= self.config.planner.replan_every_n_tracks
                    or getattr(self.session, "replan_requested", False)
                    or playlist_is_stale
                )
                if needs_plan and getattr(self.session, "replan_requested", False):
                    self.session.replan_requested = False

                if needs_plan and not self._planner_busy:
                    self._planner_busy = True
                    self._tracks_since_plan = 0
                    try:
                        self._run_planner(status, current_track)
                        # BUG-7 fix (Phase A2 dry run #2 2026-04-19):
                        # Only signal idle_needs_load if idle is genuinely
                        # empty or has a track that's not in the new
                        # playlist. Previously we re-signalled after EVERY
                        # replan, causing DJ to oscillate load_track between
                        # rank-1 and rank-2 on each planner tick (observed
                        # 11 loads in 5 min, 0 transitions).
                        fresh_status = _get_status(self.config.mixxx.url)
                        if fresh_status and self._idle_needs_fresh_load(fresh_status):
                            self.session.idle_needs_load = True
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
            # BUG-12 fix (Phase A2 dry run #2 2026-04-19): Flash
            # occasionally returns a valid-looking but non-existent path
            # (e.g. strips the genre subdirectory). DJ then fails to
            # load_track, idle deck stays empty, oscillates retrying.
            # Cross-validate every playlist path against the library DB;
            # drop candidates whose path is not a known library file.
            library_paths = {t.get("path", "") for t in library}
            library_paths.discard("")
            if library_paths and validated.get("tracks"):
                before = len(validated["tracks"])
                validated["tracks"] = [
                    t for t in validated["tracks"]
                    if t.get("path") in library_paths
                ]
                invalid = before - len(validated["tracks"])
                if invalid:
                    log.info(
                        f"Planner path-validation filter: dropped {invalid} "
                        f"invalid-path candidate(s) from playlist"
                    )
                    for i, t in enumerate(validated["tracks"]):
                        t["rank"] = i + 1
            # BUG-6 fix (Phase A2 dry run #2 2026-04-19): planner's
            # played-title exclusion uses the YouTube display title
            # while the library uses a canonical title — Flash can't
            # reliably match "Anyma & Rebūke - Syren [Live from
            # Afterlife Tomorrowland]" against "Anyma, Rebūke - Syren
            # (Live)" as the same track, so already-played tracks
            # resurface in the playlist. Post-filter in Python by
            # PATH (stable, unique) as a deterministic dedup, with
            # title fallback for pre-BUG-8 session entries that lack
            # the `path` field.
            played_paths = {
                (t.get("path") or t.get("file_path") or "")
                for t in self.tracks_played
            }
            played_paths.discard("")
            # BUG-13 fix: title-fuzzy against ALL played entries, not
            # only path-less ones. When Flash returns a malformed path
            # (BUG-12) the planner candidate and the played entry might
            # both have paths, but the paths don't match exactly while
            # titles refer to the same song. Title-fuzzy is the safety
            # net for that class of Flash-hallucination.
            played_titles_lower = {
                (t.get("title") or "").lower() for t in self.tracks_played
            }
            played_titles_lower.discard("")

            # BUG-14 fix (Phase A2 dry run #2 2026-04-19): strip common
            # boilerplate words AND raise threshold to 0.8. At 0.6 with
            # raw titles, "Stephan Bodzin - Bedford (Original Mix)" and
            # "Stephan Bodzin - Io (Original Mix)" falsely matched
            # because the two artist words + suffix boilerplate ate up
            # the overlap ratio. After stripping boilerplate, only the
            # song identifier words remain, so the threshold can be
            # stricter without missing true duplicates.
            _TITLE_BOILERPLATE = {
                "original", "mix", "live", "extended", "remix", "feat",
                "ft", "ft.", "official", "video", "audio", "edit",
                "radio", "lyric", "visual", "visualizer", "dub", "vip",
                "remastered", "vs", "vs.", "&", "-", "",
            }

            def _significant_words(title: str) -> set:
                words = title.replace("(", " ").replace(")", " ")\
                    .replace(",", " ").replace("[", " ").replace("]", " ").split()
                return {w for w in words if w.lower() not in _TITLE_BOILERPLATE}

            def _title_matches_played(candidate_title: str) -> bool:
                if not played_titles_lower:
                    return False
                cwords = _significant_words(candidate_title.lower())
                if not cwords:
                    return False
                for played in played_titles_lower:
                    pwords = _significant_words(played)
                    if not pwords:
                        continue
                    overlap = len(cwords & pwords) / min(len(cwords), len(pwords))
                    if overlap >= 0.8:
                        return True
                return False

            if (played_paths or played_titles_lower) and validated.get("tracks"):
                before = len(validated["tracks"])
                validated["tracks"] = [
                    t for t in validated["tracks"]
                    if (t.get("path") not in played_paths)
                    and not _title_matches_played(t.get("title", ""))
                ]
                dropped = before - len(validated["tracks"])
                if dropped:
                    log.info(
                        f"Planner played-track filter: dropped {dropped} "
                        f"already-played track(s) from playlist"
                    )
                    # Re-rank remaining so rank numbers stay 1..N
                    for i, t in enumerate(validated["tracks"]):
                        t["rank"] = i + 1
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
            # v8 bridge: emit library_need whenever the usable playlist is
            # empty, regardless of library size. BUG-10 fix: previously the
            # guard only fired on `not library` (empty library). With BUG-6
            # filter active, the library can be full but post-filter be
            # empty because every track has been played — that's the
            # played-exhaustion case which still needs more tracks, just
            # for a different reason. Treat both as library_need.
            if not validated["tracks"]:
                mood_slug = validated.get("mood_snapshot") or (
                    getattr(self.session, "mood_profile", {}) or {}
                ).get("canonical_slug") or self.mood or "melodic-techno"
                reason = (
                    "library empty" if not library
                    else f"all {len(library)} library tracks already played this set"
                )
                if not getattr(self.session, "library_need", None):
                    self.session.library_need = {
                        "mood": mood_slug,
                        "count": 5,
                        "reason": reason,
                    }
                    log.info(f"Planner emitted library_need signal for {mood_slug}: {reason}")
        except (ValueError, PlaylistValidationError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log.warning(f"Planner output invalid — keeping last good playlist. {msg}")
            self.session.last_planner_error = msg

    def _playlist_contains_played(self, playlist) -> bool:
        """BUG-9 fix: True if the current playlist contains any track that
        has already been played this session. Forces a planner replan
        which will re-run the BUG-6 played-filter. Catches the post-
        restart case where session.playlist persists but
        _tracks_since_plan resets to 0.
        """
        if not playlist or not playlist.get("tracks"):
            return False
        tracks_played = list(self.tracks_played or [])
        if not tracks_played:
            return False
        played_paths = {
            (t.get("path") or t.get("file_path") or "")
            for t in tracks_played
        }
        played_paths.discard("")
        # BUG-13 fix: title-fuzzy check must run against ALL played
        # entries, not only path-less ones. Flash can return a candidate
        # with a MALFORMED path (BUG-12 territory) that doesn't match
        # any library path — but the title still refers to an already-
        # played track. Previously this check was gated on `if not path`
        # which meant path-bearing played entries didn't contribute to
        # the fallback fuzzy match, so ARTBAT Remember (Original Mix)
        # in a new playlist wouldn't be flagged as re-playing
        # ARTBAT Remember (Official Video) already in history.
        played_titles_lower = {
            (t.get("title") or "").lower() for t in tracks_played
        }
        played_titles_lower.discard("")

        # BUG-14: strip boilerplate, threshold 0.8 (same as BUG-6 filter).
        _BOILERPLATE = {
            "original", "mix", "live", "extended", "remix", "feat", "ft",
            "ft.", "official", "video", "audio", "edit", "radio", "lyric",
            "visual", "visualizer", "dub", "vip", "remastered",
            "vs", "vs.", "&", "-", "",
        }

        def _sig_words(title: str) -> set:
            w = title.replace("(", " ").replace(")", " ")\
                .replace(",", " ").replace("[", " ").replace("]", " ").split()
            return {x for x in w if x.lower() not in _BOILERPLATE}

        for track in playlist["tracks"]:
            if track.get("path") in played_paths:
                return True
            cwords = _sig_words((track.get("title") or "").lower())
            if not cwords:
                continue
            for played in played_titles_lower:
                pwords = _sig_words(played)
                if not pwords:
                    continue
                overlap = len(cwords & pwords) / min(len(cwords), len(pwords))
                if overlap >= 0.8:
                    return True
        return False

    def _idle_needs_fresh_load(self, status) -> bool:
        """True if idle deck should get a new track loaded.

        BUG-7 fix: only signal idle_needs_load when idle is genuinely empty
        OR loaded with a track that is NOT in the new playlist's top-5.
        Otherwise DJ spams load_track on every planner tick even though
        idle already has a valid candidate cued.
        """
        from .main import _active_idle_decks
        _, idle_deck = _active_idle_decks(status)
        d_idle = status.get(f"deck{idle_deck}", {})
        if not d_idle.get("track_loaded"):
            return True  # empty — definitely needs load
        # Idle has a track loaded. Check if it's in the current playlist's
        # top 5 — if yes, keep it; if no, it's stale, request a load.
        playlist = getattr(self.session, "playlist", None)
        if not playlist or not playlist.get("tracks"):
            return False  # no playlist → nothing to compare; let DJ decide
        # Get the idle deck's loaded path from Mixxx
        try:
            import httpx
            tinfo = httpx.get(
                f"{self.config.mixxx.url}/api/deck/{idle_deck}/track_info",
                timeout=2,
            ).json()
            idle_path = tinfo.get("file_path", "")
        except Exception:
            return False  # can't tell; don't spam signal
        if not idle_path:
            return True
        top_paths = {t.get("path", "") for t in playlist["tracks"][:5]}
        return idle_path not in top_paths

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
        # BUG-17 fix (Phase A2 dry run #2 2026-04-19): path-based played set
        # for the fallback SQL pool. Runtime titles from Mixxx track_info
        # (e.g. "Massano - Telepathic") can differ from DB titles
        # (e.g. "Massano - Telepathic (Original Mix)"), so title-only dedup
        # lets a played track re-load when the planner's playlist is empty.
        played_paths = {t.get("path", "") for t in self.tracks_played}
        played_paths.discard("")

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
                         and r.get("path") not in played_paths
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

