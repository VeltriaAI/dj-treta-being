"""Planner loop mixin — background track planning and idle deck loading."""

import json
import logging
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")

PLAYLIST_FILE = Path("/tmp/dj-treta-playlist.json")


class PlannerMixin:

    def _planner_loop(self):
        """Background: plan 6 tracks, load idle deck, re-plan every 4 tracks."""
        from .main import _get_status

        self._tracks_since_plan = 0
        last_track = ""
        time.sleep(5)  # let heartbeat boot first
        while self._running:
            try:
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
                    # Self-evolution check
                    reflect_n = getattr(self.config, 'evolution', None)
                    reflect_interval = reflect_n.reflect_every_n_tracks if reflect_n else 5
                    if (len(self.tracks_played) >= reflect_interval
                            and len(self.tracks_played) - self._last_reflect_count >= reflect_interval):
                        self._last_reflect_count = len(self.tracks_played)
                        if hasattr(self, '_evolution_reflect') and getattr(self.config, 'evolution', None) and self.config.evolution.enabled:
                            threading.Thread(target=self._evolution_reflect, daemon=True).start()
                        else:
                            threading.Thread(target=self._agent_reflect, daemon=True).start()

                playlist = self._read_playlist()
                needs_plan = (
                    not playlist
                    or not playlist.get("planner_output")
                    or self._tracks_since_plan >= self.config.planner.replan_every_n_tracks
                )

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
        """Run planner agent with DB-powered track selection."""
        from .db import get_track_by_path, find_compatible_tracks, get_all_analyzed_tracks

        played_list = [t.get("title", "?") for t in self.tracks_played]

        # Get current track's REAL metadata from DB
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

        # SQL query for compatible tracks
        candidates = []
        if current_meta and current_meta.get("bpm"):
            candidates = find_compatible_tracks(
                bpm=current_meta.get("bpm", 125),
                key_camelot=current_meta.get("key_camelot", ""),
                energy=current_meta.get("energy_peak", 5),
                played_titles=played_list,
            )

        # Build compact candidate list for planner
        candidate_text = ""
        if candidates:
            for c in candidates:
                # Compact timeline summary
                timeline_summary = ""
                tl = c.get("timeline", "")
                if tl:
                    try:
                        import json as _json
                        sections = _json.loads(tl) if isinstance(tl, str) else tl
                        parts = [f"{s['section']}({s['energy']})" for s in sections]
                        timeline_summary = f" | Structure: {' → '.join(parts)}"
                    except Exception:
                        pass

                candidate_text += (
                    f"  - {c['title']} | path: {c['path']} | "
                    f"BPM:{c.get('bpm',0):.0f} Key:{c.get('key_musical','?')} "
                    f"Energy:{c.get('energy_peak','?')} "
                    f"Mix-in:{c.get('mix_in_seconds',0) or 0:.0f}s "
                    f"Mix-out:{c.get('mix_out_seconds',0) or 0:.0f}s"
                    f"{timeline_summary}\n"
                )

        # Current track info
        current_info = "NOTHING — silence!"
        if current_meta:
            bpm = current_meta.get('bpm') or 0
            key = current_meta.get('key_musical') or '?'
            energy = current_meta.get('energy_peak') or '?'
            current_info = f"{current_track} | BPM:{bpm:.0f} Key:{key} Energy:{energy}"

        # v6.0: Being's directive to Planner agent (replaces user_intent band-aid)
        directive_line = ""
        if self.planner_directive:
            directive_line = f"\nDIRECTIVE FROM TRETA: {self.planner_directive}\nThis is a direct instruction from the Being. Prioritize this above BPM/key matching.\n\n"
            log.info(f"Planner directive consumed: {self.planner_directive[:80]}")
            self.planner_directive = ""

        # Legacy: user_intent still supported (from talk command mood extraction)
        intent_line = ""
        if self.user_intent:
            intent_line = f"\nLISTENER REQUEST: \"{self.user_intent}\"\nThis is what the listener wants RIGHT NOW. Prioritize this above BPM/key matching.\n\n"
            self.user_intent = ""

        # Listener feedback — what they like/dislike shapes selection
        feedback_line = ""
        try:
            from .db import get_liked_tracks, get_disliked_tracks
            liked = get_liked_tracks(10)
            disliked = get_disliked_tracks(10)
            if liked:
                genres = set(l.get("genre", "") for l in liked if l.get("genre"))
                bpms = [l.get("bpm", 0) for l in liked if l.get("bpm")]
                liked_names = [l["track_title"] for l in liked]
                feedback_line += f"\nLISTENER LIKES: {', '.join(liked_names[:5])}"
                if genres:
                    feedback_line += f"\n  Preferred genres: {', '.join(genres)}"
                if bpms:
                    feedback_line += f"\n  Preferred BPM range: {min(bpms):.0f}-{max(bpms):.0f}"
                feedback_line += "\n  Prioritize tracks SIMILAR to what the listener liked.\n"
            if disliked:
                feedback_line += f"\nLISTENER DISLIKES (AVOID similar tracks): {', '.join(disliked[:5])}\n"
        except Exception:
            pass

        log.info(f"Planner running — current: {current_track or 'nothing'}, {len(candidates)} candidates in DB")
        result = self._invoke_planner(
            f"Currently playing: {current_info}\n"
            f"Already played (DO NOT repeat): {played_list}\n\n"
            f"Tracks already in library:\n{candidate_text or '  (none)'}\n\n"
            f"Current mood/genre: {self.mood or 'melodic-techno'}.\n"
            + directive_line
            + intent_line
            + feedback_line
            + self._build_source_instructions() +
            f"After creating/finding new tracks, analyze each one.\n"
            f"Then pick the best next 3 tracks from what's available.\n"
            f"For each: title, full path, BPM, key, energy, why it fits."
        )
        log.info(f"Planner done: {str(result)[:500]}")

        self._write_playlist(result, current_track)

    def _build_source_instructions(self) -> str:
        """Build planner instructions based on enabled music sources."""
        mood = self.mood or 'melodic-techno'
        parts = []
        if self.config.sources.youtube:
            parts.append(
                f"Search YouTube and download {self.config.planner.download_new_tracks} NEW "
                f"'{mood}' tracks. Search for different artists each time. "
                f"Don't download what's already in library.\n"
            )
        else:
            parts.append("YouTube is DISABLED. Do NOT search YouTube, do NOT download. You cannot.\n")
        if self.config.sources.treta_originals:
            gen_count = self.config.planner.generate_new_tracks
            if not self.config.sources.youtube:
                # Originals only — generate more to compensate
                gen_count = self.config.planner.download_new_tracks + self.config.planner.generate_new_tracks
            parts.append(
                f"Delegate to your 'producer' sub-agent to generate {gen_count} "
                f"original track(s). Tell the producer the BPM, key, genre='{mood}', and describe the mood/instruments.\n"
                f"Example: producer(\"Generate a {mood} track, 125 BPM, A minor, with warm pads and driving bass, genre {mood}\")\n"
                f"This is YOUR music — be creative with the description. Each track should sound DIFFERENT.\n"
            )
        if not self.config.sources.youtube and not self.config.sources.treta_originals:
            parts.append("Only use tracks already in the library.\n")
        return "".join(parts)

    def _load_next_on_idle(self, status):
        """Load next compatible track on idle deck — direct Mixxx API, no agent."""
        from .main import _active_idle_decks
        from .db import find_compatible_tracks, get_track_by_path

        active_deck, idle_deck = _active_idle_decks(status)
        d_idle = status.get(f"deck{idle_deck}", {})

        # Skip if idle already has a fresh track
        if d_idle.get("track_loaded") and float(d_idle.get("remaining_seconds", 0) or 0) > 60:
            return

        # Get BOTH deck file paths — never load what's on either deck
        exclude_paths = set()
        for dk in [1, 2]:
            try:
                tinfo = httpx.get(
                    f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2
                ).json()
                p = tinfo.get("file_path", "")
                if p:
                    exclude_paths.add(p)
            except Exception:
                pass

        active_path = ""
        try:
            tinfo = httpx.get(
                f"{self.config.mixxx.url}/api/deck/{active_deck}/track_info", timeout=2
            ).json()
            active_path = tinfo.get("file_path", "")
        except Exception:
            pass

        # Find compatible tracks from DB
        played_titles = [t.get("title", "") for t in self.tracks_played]
        current_meta = get_track_by_path(active_path) if active_path else None

        candidates = []
        if current_meta and current_meta.get("bpm"):
            candidates = find_compatible_tracks(
                bpm=current_meta["bpm"],
                key_camelot=current_meta.get("key_camelot", ""),
                energy=current_meta.get("energy_peak", 5),
                played_titles=played_titles,
            )
        # Filter out tracks on EITHER deck
        candidates = [c for c in candidates if c.get("path") not in exclude_paths]

        # Prefer tracks matching current mood/genre
        if self.mood and candidates:
            mood_match = [c for c in candidates
                          if self.mood.lower() in (c.get("genre", "") or "").lower()
                          or self.mood.lower() in (c.get("mood", "") or "").lower()
                          or self.mood.lower() in (c.get("path", "") or "").lower()]
            if mood_match:
                candidates = mood_match

        # Genre override: if mood is set but NO compatible tracks match the genre,
        # search DB by genre directly (ignore BPM matching for genre switches)
        if self.mood and not candidates:
            from .db import get_db
            db = get_db()
            try:
                genre_tracks = [dict(r) for r in db.execute(
                    "SELECT * FROM tracks WHERE (genre LIKE ? OR path LIKE ?) AND analyzed_at IS NOT NULL ORDER BY RANDOM() LIMIT 10",
                    (f"%{self.mood}%", f"%{self.mood}%")
                ).fetchall()]
                candidates = [t for t in genre_tracks
                              if t.get("path") not in exclude_paths
                              and t.get("title") not in played_titles]
                if candidates:
                    log.info(f"Genre override: found {len(candidates)} {self.mood} tracks (bypassed BPM filter)")
            finally:
                db.close()

        # When youtube source is off, prefer Treta originals
        if not self.config.sources.youtube and self.config.sources.treta_originals:
            originals = [c for c in candidates
                         if c.get("artist") == "DJ Treta" or "DJ Treta" in c.get("title", "") or c.get("title", "").startswith("DJ Treta")]
            if originals:
                candidates = originals

        if not candidates:
            # Fallback: get ANY track from DB (analyzed or not) — Mixxx can play anything
            from .db import get_db as _get_db
            db = _get_db()
            try:
                all_tracks = [dict(r) for r in db.execute(
                    "SELECT path, title FROM tracks ORDER BY RANDOM() LIMIT 20"
                ).fetchall()]
                candidates = [t for t in all_tracks
                              if t.get("path") not in exclude_paths
                              and t.get("title") not in played_titles]
            finally:
                db.close()

        if not candidates:
            log.warning("No tracks available to load on idle deck")
            return

        next_track = candidates[0]
        track_path = next_track["path"]

        # Load via Mixxx API
        try:
            result = httpx.post(
                f"{self.config.mixxx.url}/api/load",
                json={"deck": idle_deck, "track": track_path},
                timeout=5,
            ).json()

            if result.get("ok"):
                log.info(f"Loaded deck {idle_deck}: {next_track.get('title', '?')[:50]}")

                # Save duration from Mixxx (Gemini analysis often misses it)
                try:
                    url = self.config.mixxx.url
                    time.sleep(1)
                    from .main import _get_status
                    st = _get_status(url)
                    if st:
                        dur = float(st.get(f"deck{idle_deck}", {}).get("duration", 0) or 0)
                        if dur > 0:
                            from .db import upsert_track
                            upsert_track(path=track_path, duration_seconds=dur)
                except Exception:
                    pass

            else:
                log.warning(f"Load failed: {result}")
        except Exception as e:
            log.warning(f"Load error: {e}")

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

    def _write_playlist(self, planner_output, current_track):
        """Write planner output to playlist file."""
        playlist = {
            "current": {"title": current_track or ""},
            "planner_output": planner_output[:2000],
            "played": [t.get("title", "?") for t in self.tracks_played],
            "updated_at": time.time(),
        }
        PLAYLIST_FILE.write_text(json.dumps(playlist, indent=2))

    def _read_playlist(self) -> dict | None:
        try:
            if PLAYLIST_FILE.exists():
                return json.loads(PLAYLIST_FILE.read_text())
        except Exception:
            pass
        return None
