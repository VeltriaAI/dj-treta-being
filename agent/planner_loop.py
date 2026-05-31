"""Planner loop mixin — background track planning and idle deck loading.

v8: planner's output is a structured PlaylistV1 JSON written to
session.playlist (not a markdown blob written to /tmp). Track loading
reads that playlist instead of running SQL filters. See REFACTOR_PLAN.md
§6 Phase 3.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("dj-treta")


# Hard-genre gate: when the resolved mood is in this map, candidates must
# match at least one of the listed substrings against subgenre/genre. Keeps
# bolly-vocal tech-house tracks from polluting a `techno` plan when they
# happen to land inside the BPM/energy window.
#
# Moods NOT in this map are intentionally cross-genre (bollyafro, fusion,
# experimental) and don't get a hard gate.
HARD_GENRE_GATE = {
    "techno":          ("techno",),
    "techno-deep":     ("techno",),
    "deep-techno":     ("techno",),
    "melodic-techno":  ("techno", "melodic"),
    "minimal-techno":  ("techno", "minimal"),
    "peak-techno":     ("techno",),
    "dark-techno":     ("techno",),
    "psytrance":       ("psy", "trance"),
    "psy-trance":      ("psy", "trance"),
    "trance":          ("trance",),
    "afro-house":      ("afro",),
    "deep-house":      ("deep house", "house"),
    "tech-house":      ("tech house",),
    "drum-and-bass":   ("drum", "bass", "dnb"),
}


def _format_current_timeline(current_meta: dict | None) -> str:
    """Render the current track's section timeline for the v9 planner prompt.

    v9 Tier-1 enrichment: the planner needs to see the current track's
    structure (intro/build/drop/breakdown/outro) so rank-1 picks can match
    outro energy character. Data comes from track.timeline JSON — already
    extracted by the audio analyzer.
    """
    if not current_meta:
        return ""
    raw = current_meta.get("timeline") or ""
    if not raw:
        return ""
    try:
        sections = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if not isinstance(sections, list):
        return ""
    parts = []
    for s in sections:
        name = s.get("section") or "?"
        energy = s.get("energy") or "?"
        parts.append(f"{name}(e{energy})")
    return " → ".join(parts) if parts else ""


class PlannerMixin:

    def _emit_kb(self, msg: str, level: str = "INFO") -> None:
        """Broadcast a knowledge-surfacing event to the TUI.

        Routes through `_ws_broadcast("thinking", ...)` with `agent="planner"`
        so the TUI's existing thinking-event path picks it up; the unified
        renderer in tui.py classifies by tag (TAG_PLAN here). The level kw
        is reserved for future when we add a typed log event — for now it
        just prefixes the body so WARN/ERROR are still readable.
        """
        if not hasattr(self, "_ws_broadcast"):
            return
        body = msg if level == "INFO" else f"[{level}] {msg}"
        try:
            self._ws_broadcast("thinking", {
                "agent": "planner",
                "type": "think",
                "text": body,
            })
        except Exception:
            pass
        # Also write to thinking.log so the file replay shows it.
        try:
            from .runtime_paths import runtime_path
            with open(runtime_path("thinking.log"), "a") as f:
                f.write(f"[THINK:planner] {body}\n")
        except Exception:
            pass

    def _planner_loop(self):
        """Background: plan 6 tracks, load idle deck, re-plan every 4 tracks."""
        from .main import _get_status

        self._tracks_since_plan = 0
        last_track = ""
        time.sleep(5)  # let heartbeat boot first
        while self._running:
            try:
                # Meta-control: Treta can pause the planner when she
                # wants direct control. Cycle sleeps and re-checks.
                if getattr(self.session, "planner_paused", False):
                    time.sleep(5)
                    continue

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
                # Sarathi: Manish drives the transitions, so the track-change
                # replan trigger rarely fires and the Up Next queue goes stale /
                # thin. Keep it fresh on a time cadence (and immediately when
                # it's thin) so the Up Next panel always offers a real list of
                # candidates for him to pick from.
                if getattr(self.session, "sarathi_mode", False):
                    _n = len((playlist or {}).get("tracks", []) or [])
                    _age = time.time() - getattr(self, "_last_plan_ts", 0.0)
                    if _n < 5 or _age > 30:
                        needs_plan = True
                if needs_plan and getattr(self.session, "replan_requested", False):
                    self.session.replan_requested = False

                if needs_plan and not self._planner_busy:
                    self._planner_busy = True
                    self._tracks_since_plan = 0
                    self._last_plan_ts = time.time()
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

    def _topup_playlist_local(self, validated, library, current_meta,
                              played_paths, title_played_fn, target=8):
        """Fill the playlist up to `target` with real local-library tracks.

        Robust Up-Next floor: the LLM playlist can be thin or empty, but the
        cockpit Up Next panel + Sarathi candidate list need a dependable list
        of real tracks following the current energy. We pick current-genre,
        not-yet-played tracks ranked by BPM proximity to the current track
        (falling back to the mood BPM-range center) and append them after the
        LLM's existing ranks.
        """
        tracks = validated.get("tracks") or []
        if len(tracks) >= target:
            return
        have_paths = {t.get("path", "") for t in tracks}
        have_paths.discard("")

        # Anchor BPM: current track, else mood-range center.
        cur_bpm = 0.0
        if current_meta:
            try:
                cur_bpm = float(current_meta.get("bpm") or 0)
            except Exception:
                cur_bpm = 0.0
        if not cur_bpm:
            mp = getattr(self.session, "mood_profile", None) or {}
            rng = mp.get("bpm_range") if isinstance(mp, dict) else None
            if rng and len(rng) == 2:
                try:
                    cur_bpm = (float(rng[0]) + float(rng[1])) / 2.0
                except Exception:
                    cur_bpm = 0.0

        # Resolve the mood band: canonical genre + BPM/energy ranges. These
        # are what selection should target, not the raw free-text mood.
        mp = getattr(self.session, "mood_profile", None)
        mp = mp if isinstance(mp, dict) else {}
        canonical = (mp.get("canonical_slug") or "").strip().lower()
        _br = mp.get("bpm_range")
        bpm_range = _br if (isinstance(_br, (list, tuple)) and len(_br) == 2) else None
        _er = mp.get("energy_range")
        energy_range = _er if (isinstance(_er, (list, tuple)) and len(_er) == 2) else None
        energy_target = ((float(energy_range[0]) + float(energy_range[1])) / 2.0
                         if energy_range else None)
        cur_key = (current_meta or {}).get("key_camelot", "") or "" if current_meta else ""
        from .camelot import key_compatibility_score

        # Pull the FULL library incl. unanalyzed tracks — most freshly
        # downloaded tracks have no BPM/key yet, but they're real files that
        # resolve + display fine, and we want them in the Up Next list. (The
        # analyzed-only `library` arg the planner uses is too small here.)
        try:
            from .db import get_library_with_metadata as _glib
            full_library = _glib(include_unanalyzed=True) or library
        except Exception:
            full_library = library

        # Restrict to the current genre folder. The resolver emits fine-grained
        # slugs ("peak-time-melodic-techno") but tracks live in coarse genre
        # folders ("melodic-techno/"). Map canonical → actual folder by finding
        # the library folder whose slug is contained in the canonical slug
        # (longest wins); fall back to the raw-mood slug, then to no gate.
        # (Gating by the raw canonical slug directly would match no folder →
        # zero candidates → emergency-load garbage — the bug we're fixing.)
        genre_slugs = set()
        for tk in full_library:
            pp = tk.get("path", "") or ""
            if "/" in pp:
                genre_slugs.add(pp.split("/", 1)[0].lower())
        raw_slug = (self.mood or "").strip().lower().replace(" ", "-")
        slug = ""
        for src in (canonical, raw_slug):
            if not src:
                continue
            matches = [g for g in genre_slugs if g and g in src]
            if matches:
                slug = max(matches, key=len)
                break

        candidates = []
        for tk in full_library:
            p = tk.get("path", "") or ""
            if not p or p in have_paths or p in played_paths:
                continue
            # DB paths are relative ("melodic-techno/foo.mp3"); accept both
            # relative and absolute forms of the genre-folder match.
            if slug and (f"{slug}/" not in p):
                continue
            if title_played_fn(tk.get("title", "")):
                continue
            try:
                bpm = float(tk.get("bpm") or 0)
            except Exception:
                bpm = 0.0
            # Hard BPM-band gate: an analyzed track outside the mood's BPM
            # range (± small tolerance) is off-energy — exclude it as a
            # playable pick. A 152-BPM track can never land under a 122-128
            # melodic-techno band. Unanalyzed (bpm=0) tracks bypass this and
            # sort last so the Up-Next list still fills when the pool is thin.
            if bpm and bpm_range:
                lo = float(bpm_range[0]) - 4.0
                hi = float(bpm_range[1]) + 4.0
                if not (lo <= bpm <= hi):
                    continue
            # Composite score (lower = better): BPM gap + harmonic distance
            # + energy gap. Replaces pure BPM-proximity so harmonic clashes
            # and energy jumps are penalised, not just tempo.
            if bpm and cur_bpm:
                bpm_gap = abs(bpm - cur_bpm)
            elif bpm and bpm_range:
                bpm_gap = abs(bpm - ((float(bpm_range[0]) + float(bpm_range[1])) / 2.0))
            else:
                bpm_gap = 999.0  # unanalyzed → sort last
            cand_key = tk.get("key_camelot", "") or ""
            key_penalty = ((10 - key_compatibility_score(cur_key, cand_key)) * 1.5
                           if (cur_key and cand_key) else 0.0)
            cand_energy = tk.get("energy_peak", tk.get("energy"))
            energy_gap = 0.0
            if energy_target is not None and cand_energy is not None:
                try:
                    energy_gap = abs(float(cand_energy) - energy_target) * 2.0
                except Exception:
                    energy_gap = 0.0
            score = bpm_gap + key_penalty + energy_gap
            candidates.append((score, tk))

        candidates.sort(key=lambda x: x[0])
        rank = len(tracks)
        for _score, tk in candidates:
            if len(tracks) >= target:
                break
            rank += 1
            tracks.append({
                "rank": rank,
                "path": tk.get("path", ""),
                "title": tk.get("title", "") or tk.get("canonical_song", "") or "",
                "bpm": tk.get("bpm") or 0,
                "key_camelot": tk.get("key_camelot", "") or "",
                "downloaded": True,
                "source": "local-topup",
            })
        validated["tracks"] = tracks

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
        from .prompts import build_planner_v8_message, build_planner_v9_message

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

        # Read shape directive (free-text) for prompt injection. We no
        # longer clear it after one consume — shape directives have a TTL
        # and auto-expire via session.expire_stale(), so a directive the
        # LLM ignored on cycle N still applies on cycle N+1 until it
        # naturally expires (~90s default) or is replaced by a new call.
        # See plan: atomic-cuddling-manatee — fixes Bug 2 fire-once-clear.
        try:
            self.session.expire_stale()
        except Exception as exc:
            log.warning(f"directive expire_stale failed: {exc}")
        directive = self.planner_directive or ""

        # user_intent stays one-shot (replace_deck legacy path uses it).
        intent = self.user_intent or ""
        if intent:
            self.user_intent = ""

        # Detect a replace_deck intent before the planner LLM call so we
        # can act on it after the playlist is generated. Replace intents
        # are JSON dicts; bare strings flow through unchanged as free-form
        # intent text for the prompt.
        replace_intent = None
        if intent and intent.lstrip().startswith("{"):
            try:
                parsed = json.loads(intent)
                if isinstance(parsed, dict) and parsed.get("action") == "replace_deck":
                    replace_intent = parsed
            except Exception:
                pass

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

        # v9: if knowledge is enabled, surface candidates from the 3.5M-track
        # dataset and use the v9 prompt. Fallback to v8 (full-library dump) if
        # knowledge is off or surfacing produces too few candidates.
        v9_merged = None
        v9_mode = False
        if getattr(self.config, "knowledge", None) and getattr(
            self.config.knowledge, "enabled", False
        ):
            try:
                v9_merged = self._surface_v9_candidates(
                    current_meta=current_meta,
                    played_list=played_list,
                )
            except Exception as exc:
                log.warning(f"v9 candidate surface failed, falling back to v8: {exc}")

        # Phase 7: surface externally-owned decks to the planner so it stays
        # advisory for treta-owned decks only.
        external_decks = [
            int(d) for d, o in (getattr(self.session, "deck_ownership", {}) or {}).items()
            if o != "treta"
        ]

        if v9_merged and len(v9_merged) >= 5:
            v9_mode = True
            current_timeline = _format_current_timeline(current_meta)
            log.info(
                f"Planner running (v9) — current: {current_track or 'nothing'}, "
                f"{len(v9_merged)} merged candidates "
                f"({sum(1 for c in v9_merged if c.get('downloaded'))} local)"
            )
            planner_msg = build_planner_v9_message(
                current_info=current_info,
                current_timeline=current_timeline,
                played_list=played_list,
                merged_candidates=v9_merged,
                mood_profile=getattr(self.session, "mood_profile", None),
                mood=self.mood or "",
                planner_directive=directive,
                user_intent=intent,
                feedback_line=feedback_line,
                external_decks=external_decks,
            )
        else:
            log.info(
                f"Planner running (v8) — current: {current_track or 'nothing'}, "
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
                external_decks=external_decks,
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
            if v9_mode:
                # v9: validate downloaded tracks against local library paths;
                # undownloaded tracks against the dataset refs we surfaced.
                # Catches Flash hallucinations of either kind.
                local_paths = {
                    c.get("path", "") for c in v9_merged if c.get("downloaded")
                }
                local_paths.discard("")
                dataset_refs = {
                    (c.get("mbid") or "", c.get("video_id") or "")
                    for c in v9_merged if not c.get("downloaded")
                }
                dataset_refs.discard(("", ""))
                # Same relaxation as v8 (see comment below): drop nothing,
                # mark mismatches as downloaded=False so library_loop fetches
                # them and DJ falls through via load_track's fuzzy resolver.
                # When v9_merged is small (e.g. LanceDB unavailable) Flash
                # picks from training-data memory; the strict drop emptied
                # the playlist and burned to emergency_load.
                if validated.get("tracks"):
                    flagged = 0
                    for t in validated["tracks"]:
                        if t.get("downloaded", True):
                            if t.get("path") not in local_paths:
                                t["downloaded"] = False
                                flagged += 1
                        else:
                            ref = (t.get("mbid", ""), t.get("video_id", ""))
                            if ref not in dataset_refs:
                                # Already undownloaded; nothing to flip,
                                # but keep the candidate — library_loop
                                # will try fetching by title.
                                flagged += 1
                    if flagged:
                        log.info(
                            f"Planner v9 validation: {flagged} candidate(s) "
                            f"with unknown refs flagged for fetch / fuzzy load"
                        )
            else:
                # v8 path validation — relaxed.
                #
                # Flash sometimes returns real-world track names from training
                # data ("Massano - The Lights" exists; we don't have it). The
                # old code dropped every such candidate, leaving the planner
                # with an EMPTY playlist whenever Flash hallucinated more than
                # 4 of its 5 picks (observed on VM: 0-track playlists →
                # emergency_load every minute).
                #
                # New behavior: keep the candidate but flag it. If the path
                # isn't in the library we mark `downloaded=False` so the
                # library_loop can try to fetch it via the title (Flash often
                # picks real tracks YouTube can resolve), and so DJ knows to
                # try the next candidate via load_track's fuzzy resolver
                # rather than treating it as a guaranteed-good local file.
                library_paths = {t.get("path", "") for t in library}
                library_paths.discard("")
                if library_paths and validated.get("tracks"):
                    flagged = 0
                    for t in validated["tracks"]:
                        if t.get("path") not in library_paths:
                            t["downloaded"] = False
                            flagged += 1
                    if flagged:
                        log.info(
                            f"Planner v8 validation: {flagged} candidate(s) "
                            f"with unknown paths flagged downloaded=False "
                            f"(library_loop will fetch / DJ will skip)"
                        )
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
            # Deterministic Up-Next top-up. The LLM playlist often comes back
            # thin/empty (Flash hallucinates paths with knowledge off; the
            # played-filter is aggressive in long sets). Guarantee a real,
            # valid, full Up Next by topping up from the local library —
            # current-genre, already-played excluded, ranked by BPM fit. These
            # are real on-disk files so they always resolve in the cockpit and
            # load cleanly. Appended AFTER the LLM's ranks, so auto-mode rank-1
            # (the LLM's pick) is unchanged.
            try:
                self._topup_playlist_local(
                    validated, library, current_meta,
                    played_paths, _title_matches_played, target=8,
                )
            except Exception as exc:
                log.warning(f"playlist local top-up failed: {exc}")

            # Override LLM-hallucinated planned_at with real wall-clock time.
            # Flash occasionally emits 2024-era timestamps from its training
            # corpus; downstream logic (TUI age display, stale-playlist check)
            # relies on this being reality.
            validated["planned_at"] = time.time()
            self.session.playlist = validated
            self.session.playlist_updated_at = validated["planned_at"]
            self.session.last_planner_error = ""

            # --- E3/E5 ---  Emit a rolling arrangement plan alongside the
            # playlist. This is the leapfrog: a short sequence of musical
            # intents (track + technique + energy target + bars) toward a
            # goal, re-derived every cycle. Stored on the session as plain
            # dicts so Agent C can map them onto its State model and the DJ
            # agent can realize the techniques via Agent A's transitions.
            # Additive + defensive — a failure here never blocks the playlist.
            try:
                arr_plan = self._build_arrangement_plan(validated, current_meta)
                self.session.arrangement_plan = arr_plan.to_dict()
                self.session.arrangement_plan_updated_at = time.time()
                log.info(
                    f"Arrangement plan: goal={arr_plan.goal} "
                    f"{len(arr_plan.intents)} intents, {arr_plan.horizon_bars} bars"
                )
            except Exception as exc:
                log.warning(f"arrangement plan build failed (non-fatal): {exc}")
            log.info(
                f"Planner wrote playlist: {len(validated['tracks'])} candidates, "
                f"mood_snapshot={validated.get('mood_snapshot', '')}"
            )

            # Replace-deck intent: bypass the "idle deck has fresh cued
            # track" auto-load gate and directly eject + load the rank-1
            # downloaded candidate from the freshly written playlist.
            if replace_intent:
                self._execute_replace_intent(replace_intent, validated)
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("log", {
                    "text": f"Planner: {len(validated['tracks'])} candidates planned"
                })

            # v9: if rank-1 (or any track) is undownloaded, signal library to
            # fetch. K5 library loop consumes session.library_need with the
            # targeted-fetch shape {video_id, title, mbid, ...}.
            if v9_mode and validated.get("tracks"):
                for t in sorted(
                    validated["tracks"], key=lambda x: x.get("rank", 99)
                ):
                    if t.get("downloaded", True):
                        continue
                    # Try to enrich from the candidate list, but fall back to
                    # the track's own fields — library agent only needs
                    # video_id to download. Don't block on a perfect match.
                    vid = t.get("video_id") or ""
                    mbid = t.get("mbid") or ""
                    match = next(
                        (
                            c for c in v9_merged
                            if (c.get("video_id") or "") == vid and vid
                            or ((c.get("mbid") or "") == mbid and mbid)
                        ),
                        {},
                    )
                    if not vid and not mbid:
                        continue  # library can't do anything without either
                    existing = getattr(self.session, "library_need", None) or {}
                    if existing.get("video_id") == vid and vid:
                        break
                    canonical = match.get("canonical") if isinstance(match, dict) else None
                    if isinstance(canonical, dict):
                        canon_artist = canonical.get("artist", "")
                        canon_song = canonical.get("song", "")
                    else:
                        canon_artist = match.get("artist", "") if match else ""
                        canon_song = (match.get("title", "") if match else "") or t.get("title", "")
                    self.session.library_need = {
                        "video_id": vid,
                        "mbid": mbid,
                        "title": t.get("title") or (match.get("title", "") if match else ""),
                        "youtube_music_url": (
                            (match.get("youtube_music_url", "") if match else "")
                            or (f"https://music.youtube.com/watch?v={vid}" if vid else "")
                        ),
                        "canonical_artist": canon_artist,
                        "canonical_song": canon_song,
                        "reason": f"planner rank-{t.get('rank')} pick not downloaded",
                        "ts": time.time(),
                    }
                    log.info(
                        f"Planner emitted library_need for rank {t.get('rank')}: "
                        f"{t.get('title') or vid}"
                    )
                    break
            # v8 bridge: emit library_need whenever the usable playlist is
            # empty, regardless of library size. BUG-10 fix: previously the
            # guard only fired on `not library` (empty library). With BUG-6
            # filter active, the library can be full but post-filter be
            # empty because every track has been played — that's the
            # played-exhaustion case which still needs more tracks, just
            # for a different reason. Treat both as library_need.
            # Played-exhaustion check: count fresh (unplayed) tracks in the
            # validated playlist. If <2, library is too thin even if planner
            # produced 5 ranked tracks — they're all replays. Emit
            # library_need so library_manager peer downloads more.
            #
            # Path-format note: validated tracks come from the LLM with
            # relative paths (genre/filename.mp3); session.tracks_played
            # stores absolute paths. Match by basename to bridge formats
            # (filename collisions across genres are rare enough that
            # endswith match is reliable here).
            played_basenames = set()
            for t in (self.tracks_played or []):
                p = t.get("path") or t.get("file_path") or ""
                if p:
                    played_basenames.add(p.rsplit("/", 1)[-1])
            fresh_in_playlist = sum(
                1 for t in validated.get("tracks", [])
                if (t.get("path") or "").rsplit("/", 1)[-1] not in played_basenames
            )
            playlist_empty = not validated["tracks"]
            playlist_exhausted = (not playlist_empty) and fresh_in_playlist < 2

            if playlist_empty or playlist_exhausted:
                mood_slug = validated.get("mood_snapshot") or (
                    getattr(self.session, "mood_profile", {}) or {}
                ).get("canonical_slug") or self.mood or "melodic-techno"
                if playlist_empty:
                    reason = (
                        "library empty" if not library
                        else f"all {len(library)} library tracks already played this set"
                    )
                else:
                    reason = (
                        f"playlist has {len(validated['tracks'])} tracks but only "
                        f"{fresh_in_playlist} unplayed — library exhausted"
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

    def _surface_v9_candidates(self, current_meta, played_list) -> list[dict]:
        """Build merged candidate list for v9 planner prompt.

        Unions knowledge.discover_candidates (mood-filtered) with
        knowledge.similar_to (current-track seeded; skipped if vectors aren't
        ingested yet), dedups by mbid/canonical, merges with local library
        to mark downloaded tracks, filters out played, and sorts by
        (downloaded DESC, similarity DESC, year DESC). Returns top 30 as
        plain dicts matching build_planner_v9_message's `merged_candidates`.
        """
        from dataclasses import asdict
        from .knowledge import queries as kb
        from .knowledge.merge import merge_candidates_against_local
        from .knowledge.models import CanonicalRef

        mood_profile = getattr(self.session, "mood_profile", None) or {}
        raw_range = mood_profile.get("bpm_range") or []
        bpm_range = None
        if isinstance(raw_range, (list, tuple)) and len(raw_range) == 2:
            bpm_range = (int(raw_range[0]), int(raw_range[1]))

        mood_slug = mood_profile.get("canonical_slug") or self.mood or "?"
        self._emit_kb(f"cycle start — mood={mood_slug} bpm={bpm_range}")

        discover = kb.discover_candidates(
            mood_profile=mood_profile,
            bpm_range=bpm_range,
            limit=40,
        )
        self._emit_kb(f"discover_candidates → {len(discover)} hits")

        similar = []
        if current_meta and kb._client().has_vectors():
            artist = (
                current_meta.get("canonical_artist")
                or current_meta.get("artist")
                or ""
            )
            song = (
                current_meta.get("canonical_song")
                or current_meta.get("title")
                or ""
            )
            if artist and song:
                seed = CanonicalRef(artist=artist, song=song)
                try:
                    similar = kb.similar_to(seed, limit=20)
                    self._emit_kb(f"similar_to({artist} — {song}) → {len(similar)} hits")
                except Exception as exc:
                    log.warning(f"v9 similar_to seed failed: {exc}")
                    self._emit_kb(f"similar_to failed: {exc}", level="WARN")

        # Union + dedup (similar first to preserve similarity rank).
        seen = set()
        ktracks = []
        for t in similar + discover:
            key = t.mbid or f"{t.artist_name.lower()}|{t.title.lower()}"
            if key in seen:
                continue
            seen.add(key)
            ktracks.append(t)

        if not ktracks:
            self._emit_kb("0 unique candidates — knowledge dry, falling back to v8")
            return []

        self._emit_kb(f"union+dedup → {len(ktracks)} unique candidates")

        # Hard genre gate. When the mood has a strict genre family
        # (techno, psytrance, etc), drop candidates whose subgenre doesn't
        # match — even if their BPM/energy fit. Cross-genre moods skip this.
        gate = HARD_GENRE_GATE.get(mood_slug.lower())
        if gate:
            before = len(ktracks)
            def _passes_gate(t):
                fields = " ".join([
                    (getattr(t, "subgenre", "") or ""),
                    (getattr(t, "primary_genre", "") or ""),
                    (getattr(t, "label", "") or ""),
                ]).lower()
                return any(g in fields for g in gate)
            ktracks = [t for t in ktracks if _passes_gate(t)]
            self._emit_kb(
                f"hard genre gate ({mood_slug}, allow={list(gate)}) "
                f"→ {len(ktracks)}/{before}"
            )
            if not ktracks:
                self._emit_kb(
                    f"genre gate emptied candidate pool — falling back to v8",
                    level="WARN",
                )
                return []

        merged = merge_candidates_against_local(ktracks)
        n_dl = sum(1 for m in merged if getattr(m, "downloaded", False))
        self._emit_kb(f"merge_against_local → {n_dl} downloaded, {len(merged)-n_dl} need-download")

        # Played dedup: by canonical-title normalization.
        played_lower = {(p or "").lower() for p in played_list}
        played_lower.discard("")
        before_play_dedup = len(merged)
        merged = [
            m for m in merged
            if m.title.lower() not in played_lower
        ]
        dropped = before_play_dedup - len(merged)
        if dropped:
            self._emit_kb(f"played_dedup → dropped {dropped} already-played")

        # Filter out continuous-mix compilations (K7 data-quality finding).
        _MIX_MARKERS = (
            "continuous mix", "dj mix", "mix 01", "mix 02", "mix 03",
            "mix 1 - continuous", "mix 2 - continuous", "mix 3 - continuous",
        )
        merged = [
            m for m in merged
            if not any(marker in (m.title or "").lower() for marker in _MIX_MARKERS)
        ]

        # Rank: downloaded wins, then similarity DESC, then year DESC.
        def sort_key(m):
            return (
                0 if m.downloaded else 1,
                -(m.similarity_score or 0.0),
                -(m.year or 0),
            )
        merged.sort(key=sort_key)

        # Top 30, rank, flatten to dicts for the prompt.
        result = []
        for i, m in enumerate(merged[:30], start=1):
            d = asdict(m)
            d["rank"] = i
            # Flatten canonical for the prompt renderer.
            d["artist"] = m.canonical.artist
            result.append(d)
        return result

    # --- E3/E5 ---  Dynamic real-time arrangement authoring (the leapfrog).
    def _build_arrangement_plan(self, validated_playlist, current_meta,
                                goal: str = "", max_steps: int = 4):
        """Emit a short ROLLING sequence of ArrangementIntents toward a goal.

        This is E5's headline: instead of only "what track next", the planner
        constructs a sequence of future *intents* — (track, technique, energy
        target, bar duration) — shaping the next few phrases. It is re-derived
        every planner cycle (rolling horizon), so it adapts live as the room
        and the listener profile change.

        Inputs are PLANNER-LEVEL only:
          * `validated_playlist`: the ranked PlaylistV1 we just built — supplies
            the candidate tracks for upcoming steps.
          * `current_meta`: the playing track's DB row — supplies its energy,
            BPM, beatgrid + cue_points (for loop reasoning + the phantom cue).

        Output: an `ArrangementPlan`. It is NOT a mixer-State snapshot — Agent C
        maps each intent onto its State model at integration; Agent A realizes
        the `technique` via schedule_transition/do_transition. We never touch
        those modules from here.

        The goal is chosen from the set-arc directive if present, else inferred
        from the current energy + listener pulse, so Treta authors a deliberate
        shape rather than a flat chain of blends.
        """
        from .arrangement import (
            ArrangementIntent, ArrangementPlan,
            loop_cues_from_track, phantom_grid_cue,
        )

        tracks = sorted(
            (validated_playlist or {}).get("tracks", []) or [],
            key=lambda t: t.get("rank", 99),
        )

        # Resolve current energy + BPM (anchor the arc).
        cur_energy = 5
        cur_bpm = 0.0
        if current_meta:
            try:
                cur_energy = int(current_meta.get("energy_peak") or 5)
            except Exception:
                cur_energy = 5
            try:
                cur_bpm = float(current_meta.get("bpm") or 0)
            except Exception:
                cur_bpm = 0.0

        # Goal resolution: explicit arg > set-arc directive > inferred.
        if not goal:
            goal = self._infer_arrangement_goal(cur_energy)

        # Energy trajectory per goal — a target delta applied step over step.
        step_delta = {
            "build": +2, "peak": +1, "breakdown": -3,
            "coast": 0, "reset": -2, "loop_roll": 0,
        }.get(goal, 0)

        intents: list[ArrangementIntent] = []

        # Step 0 (optional): exploit a loop cue on the CURRENT track as creative
        # material before moving on — "16-bar vocal loop" reasoning. Only when
        # the goal benefits (build/loop_roll) and the current track has a loop.
        cur_loops = loop_cues_from_track(current_meta or {})
        if cur_loops and goal in ("build", "loop_roll", "peak"):
            best_loop = max(
                cur_loops, key=lambda l: (l.get("length_beats") or 0)
            )
            intents.append(ArrangementIntent(
                step=len(intents),
                goal="loop_roll",
                track_path=None,  # operates on the current track
                track_title=(current_meta or {}).get("title", "") or "current",
                technique="loop_roll",
                energy_target=min(10, cur_energy + 1),
                bars=int(best_loop.get("length_beats") or 16) // 4 or 8,
                loop_cue=best_loop,
                reason=(
                    f"extend current phrase on a "
                    f"{best_loop.get('length_beats') or '?'}-beat loop before "
                    f"the next track"
                ),
            ))

        # Subsequent steps: walk the ranked candidates, ramping energy toward
        # the goal. Each step picks a transition technique fit for the move.
        target_energy = cur_energy
        for t in tracks:
            if len([i for i in intents if i.track_path]) >= max_steps:
                break
            target_energy = max(1, min(10, target_energy + step_delta))
            tmeta = self._candidate_meta(t)
            technique = self._technique_for_move(cur_energy, target_energy, tmeta)
            # Phantom grid-start when the candidate has a beatgrid anchor — lets
            # Treta enter cleanly on the one even with no numbered cue there.
            use_grid = bool((tmeta or {}).get("beatgrid_anchor_seconds") is not None)
            intents.append(ArrangementIntent(
                step=len(intents),
                goal=goal,
                track_path=t.get("path"),
                track_title=t.get("title", "") or "",
                technique=technique,
                energy_target=target_energy,
                bars=16,
                use_grid_start=use_grid,
                reason=(
                    f"{goal}: take energy {cur_energy}→{target_energy} via "
                    f"{technique}"
                ),
            ))
            cur_energy = target_energy

        horizon = sum(i.bars for i in intents)
        plan = ArrangementPlan(
            goal=goal, intents=intents, created_at=time.time(),
            horizon_bars=horizon,
        )
        return plan

    def _infer_arrangement_goal(self, cur_energy: int) -> str:
        """Pick a musical goal when none is given. Honors an active set-arc
        directive if Treta planned one; else infers from current energy."""
        # Set-arc directive (Treta's plan_set_arc) takes precedence.
        try:
            arc = getattr(self.session, "set_arc", None)
            if isinstance(arc, dict) and arc.get("phase"):
                phase = str(arc["phase"]).lower()
                if phase in ("warmup", "warm-up", "build"):
                    return "build"
                if phase in ("peak", "climax"):
                    return "peak"
                if phase in ("cooldown", "cool-down", "outro", "wind-down"):
                    return "reset"
        except Exception:
            pass
        # Inference from current energy: low → build, high → coast/peak.
        if cur_energy <= 4:
            return "build"
        if cur_energy >= 8:
            return "coast"
        return "build"

    def _candidate_meta(self, candidate: dict) -> dict:
        """Best-effort DB lookup of a playlist candidate's full metadata
        (for beatgrid anchor + cues). Falls back to the candidate dict."""
        try:
            from .db import get_track_by_path
            p = candidate.get("path")
            if p:
                m = get_track_by_path(p)
                if m:
                    return m
        except Exception:
            pass
        return candidate

    def _technique_for_move(self, from_energy: int, to_energy: int,
                            cand_meta: dict) -> str:
        """Choose a transition technique that fits the energy move. Pure
        planner-level hint — Agent A's executor has final say."""
        delta = to_energy - from_energy
        if delta >= 2:
            return "riser"        # building hard → riser into the next
        if delta <= -2:
            return "echo_out"     # dropping → delay-throw the outgoing tail
        if abs(delta) <= 1 and from_energy >= 7:
            return "bass_swap"    # peak-time → tight bass swap on the one
        return "filter_sweep"     # default smooth move

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

        Typed-directive override: if there is an active `load_track`
        directive (from play_specific_track or replace_deck(path=…))
        targeting the idle deck and the named path differs from what's
        currently loaded, return True regardless of playlist top-5.
        Surgical directives must override the gate or the named track
        never gets loaded — see plan: atomic-cuddling-manatee Bug 2.
        """
        from .main import _active_idle_decks
        _, idle_deck = _active_idle_decks(status)
        d_idle = status.get(f"deck{idle_deck}", {})

        # Get the idle deck's loaded path from Mixxx (used by both the
        # directive check and the top-5 gate below).
        idle_path = ""
        if d_idle.get("track_loaded"):
            try:
                import httpx
                tinfo = httpx.get(
                    f"{self.config.mixxx.url}/api/deck/{idle_deck}/track_info",
                    timeout=2,
                ).json()
                idle_path = tinfo.get("file_path", "")
            except Exception:
                idle_path = ""

        # Directive override.
        try:
            d_load = self.session.find_active_directive(
                "load_track", target="planner", deck=idle_deck,
            )
            if d_load:
                want = (d_load.get("payload") or {}).get("path", "")
                if want and want != idle_path:
                    return True
        except Exception:
            pass

        if not d_idle.get("track_loaded"):
            return True  # empty — definitely needs load
        # Idle has a track loaded. Check if it's in the current playlist's
        # top 5 — if yes, keep it; if no, it's stale, request a load.
        playlist = getattr(self.session, "playlist", None)
        if not playlist or not playlist.get("tracks"):
            return False  # no playlist → nothing to compare; let DJ decide
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
        from .heartbeat import _idle_was_played

        active_deck, idle_deck = _active_idle_decks(status)
        d_idle = status.get(f"deck{idle_deck}", {})

        # Typed-directive override (load_track). Honored before the
        # "fresh cued, skip" gate because a surgical directive from
        # Treta (e.g. play_specific_track) outranks the fresh-cued
        # heuristic — that's the whole point of being able to say
        # "play X now". See plan: atomic-cuddling-manatee Bug 2.
        try:
            d_load = self.session.find_active_directive(
                "load_track", target="planner", deck=idle_deck,
            )
        except Exception:
            d_load = None
        if d_load:
            want_path = (d_load.get("payload") or {}).get("path", "")
            want_title = (d_load.get("payload") or {}).get("title", "")
            # Resolve current idle deck path.
            try:
                import httpx
                tinfo = httpx.get(
                    f"{self.config.mixxx.url}/api/deck/{idle_deck}/track_info",
                    timeout=2,
                ).json()
                current_idle_path = tinfo.get("file_path", "")
            except Exception:
                current_idle_path = ""
            if want_path and want_path != current_idle_path:
                if not os.path.exists(want_path):
                    log.warning(
                        f"[directive-load] path no longer on disk: {want_path!r} "
                        f"— marking directive expired, falling through to LLM playlist"
                    )
                    # Drop the directive so we don't loop on a missing file.
                    try:
                        self.session.mark_satisfied(d_load.get("id"))
                    except Exception:
                        pass
                else:
                    log.info(
                        f"[directive-load] deck={idle_deck} ← {want_title or want_path} "
                        f"(directive {d_load.get('id')})"
                    )
                    ok = load_on_deck(self.config.mixxx.url, idle_deck, want_path)
                    if ok:
                        try:
                            refresh_duration(self.config.mixxx.url, idle_deck, want_path)
                        except Exception:
                            pass
                        try:
                            self.session.mark_satisfied(d_load.get("id"))
                        except Exception:
                            pass
                        if hasattr(self, "_ws_broadcast"):
                            self._ws_broadcast(
                                "log",
                                {"text": f"Directive load deck {idle_deck}: {(want_title or '')[:50]}"},
                            )
                        return
                    else:
                        log.warning(
                            f"[directive-load] load_on_deck failed for {want_path!r} "
                            f"— falling through to LLM playlist"
                        )
            elif want_path and want_path == current_idle_path:
                # Directive's track is already on idle — mark satisfied so the
                # transition_now directive bound to it can proceed.
                try:
                    self.session.mark_satisfied(d_load.get("id"))
                except Exception:
                    pass

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
        #
        # 2026-05-03 fix (replay-guard-planner): include `file_path` fallback
        # AND derive a basename set, because `tracks_played[].path` is
        # ABSOLUTE (Mixxx tinfo.file_path) while:
        #   - the LLM playlist's candidate `path` is RELATIVE (genre/foo.mp3)
        #   - the DB `tracks.path` column is RELATIVE (normalized)
        # Direct `relative in {absolutes}` always misses → played tracks
        # leak through both `pick_next_candidate` (relied on basename via
        # played_paths arg, but only if path key was present) AND the SQL
        # fallback (compared DB-relative against absolute set). Mirror the
        # 3-tier match `_idle_was_played` does in heartbeat.
        played_paths = {
            (t.get("path") or t.get("file_path") or "")
            for t in self.tracks_played
        }
        played_paths.discard("")

        # Primary path: trust the planner's session.playlist.
        # Pass played_paths so basename-match catches replays where DB
        # title ≠ Mixxx-reported title (BUG-17 pattern).
        playlist = getattr(self.session, "playlist", None)
        pick = pick_next_candidate(
            playlist, exclude_paths, played_titles, played_paths
        )

        # Replay-guard post-filter: even after pick_next_candidate, defend
        # against the path-format edge case where neither basename nor title
        # match worked (e.g. playlist entry has empty path). Use the same
        # helper heartbeat uses for parity.
        if pick is not None and _idle_was_played(pick.get("path", ""), self.tracks_played):
            log.warning(
                f"[REPLAY-GUARD-PLANNER] pick_next_candidate returned already-played "
                f"track {pick.get('title', '?')!r} (path={pick.get('path', '')!r}); "
                f"forcing fallback"
            )
            pick = None

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
            available = []
            for r in rows:
                rp = r.get("path") or ""
                if rp in exclude_paths:
                    continue
                # Use the path-bridge helper instead of the broken
                # `rp in played_paths` direct compare — DB path is RELATIVE
                # but played_paths is ABSOLUTE, so direct membership is
                # always False and lets played tracks recycle.
                if _idle_was_played(rp, self.tracks_played):
                    log.warning(
                        f"[REPLAY-GUARD-PLANNER] SQL fallback skipped already-played "
                        f"track {r.get('title', '?')!r} (path={rp!r})"
                    )
                    continue
                if r.get("title") in played_titles:
                    continue
                available.append(r)
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

    # ── Fuzzy library matcher (for replace_deck instruction-mode) ────
    #
    # Treta often calls replace_deck with a free-text instruction
    # naming a track she wants ("play Hum Pyaar Karne Wale from
    # Dhurandhar"). When she doesn't have a verified path, we search
    # the local library for a basename match BEFORE falling back to
    # the planner's rank-1 guess. Closes the gap where the file is on
    # disk but the planner LLM ranks something else as #1.
    #
    # Stop-words filter common verbs Treta puts in front
    # ("play X", "load X", "from <album>", etc.) so they don't
    # contaminate the score.
    _FUZZY_STOP_WORDS = frozenset({
        "play", "load", "skip", "next", "the", "a", "an", "in", "on",
        "of", "from", "by", "and", "feat", "ft", "with", "for", "to",
        "now", "please", "yaar", "bro", "mix", "remix", "extended",
        "original", "edit", "version", "audio", "video", "official",
        "genre", "deck", "track", "song",
    })

    def _fuzzy_match_library(self, instruction: str) -> Optional[dict]:
        """Find the local library track whose basename best matches `instruction`.

        Returns a dict shaped like a playlist track (path, title, rank=0),
        or None when no match clears the score floor. Path is RELATIVE to
        ~/Music/DJTreta/ (consistent with the playlist `path` convention),
        so load_on_deck resolves it via the same code path as planner picks.
        """
        import re
        from pathlib import Path as _P

        music_dir = _P.home() / "Music" / "DJTreta"
        if not music_dir.exists():
            return None

        # Tokenize instruction: lowercase, alphanumeric tokens, drop
        # short tokens (< 3 chars) and stop-words.
        def _toks(s: str) -> set[str]:
            words = re.findall(r"[a-z0-9]+", s.lower())
            return {
                w for w in words
                if len(w) >= 3 and w not in self._FUZZY_STOP_WORDS
            }

        wanted = _toks(instruction)
        if not wanted:
            return None

        best = None
        best_score = 0.0
        for mp3 in music_dir.glob("*/*.mp3"):
            base_toks = _toks(mp3.stem)
            if not base_toks:
                continue
            overlap = wanted & base_toks
            if not overlap:
                continue
            # Token-set ratio: matched tokens / total unique tokens in the
            # instruction. Bias toward filenames that cover MORE of the
            # instruction (avoids "play Argy Tataki" matching every Argy
            # track equally).
            score = len(overlap) / max(len(wanted), 1)
            # Tie-breaker: prefer shorter basenames (less noise).
            if score > best_score or (
                score == best_score
                and best is not None
                and len(mp3.stem) < len(best["title"])
            ):
                # Path stored as RELATIVE (genre/file.mp3) for parity
                # with playlist['path'] convention. load_on_deck handles
                # both relative and absolute via _resolve_track_path.
                rel_path = str(mp3.relative_to(music_dir))
                best = {
                    "path": rel_path,
                    "title": mp3.stem,
                    "rank": 0,
                }
                best_score = score

        # Score floor: more than 50% of instruction tokens must hit. The
        # strict-greater rejects "play Argy - Ketuvim" → "Argy - Papito"
        # (only the artist token matches, score=0.5). When the listener
        # names a specific song that isn't on disk, prefer falling back
        # to the planner's playlist rank-1 over silently substituting a
        # different song by the same artist.
        if best is None or best_score <= 0.50:
            return None
        return best

    def _execute_replace_intent(self, intent: dict, playlist: dict) -> bool:
        """Force-replace a track on a specific deck.

        Triggered when the Being calls replace_deck() (which writes a
        structured intent into session.user_intent). Bypasses the planner's
        usual "idle deck has a fresh cued track, skip load" gate so a clear
        user intent ("get this off deck 2") actually changes what's there.

        Strategy: pick the highest-rank downloaded candidate from the
        freshly-written playlist, eject the target deck, then load. The
        downloaded restriction means listener feedback is immediate
        (no 30-120s YouTube fetch wait).
        """
        from .playback_applier import load_on_deck, refresh_duration

        deck = int(intent.get("deck", 0))
        if deck not in (1, 2):
            log.warning(f"replace_deck: invalid deck {deck}")
            return False

        # Path-mode: if the intent carries an explicit `path`, honour it
        # directly without consulting the LLM playlist. This is the
        # typed-directive path — replace_deck(deck, path=…) writes the
        # resolved path here, no rank-1 guessing. See plan:
        # atomic-cuddling-manatee Bug 2.
        explicit_path = intent.get("path") or ""
        if explicit_path:
            if not os.path.exists(explicit_path):
                log.warning(
                    f"replace_deck (path mode): file not found {explicit_path!r}"
                )
                return False
            pick = {
                "path": explicit_path,
                "title": Path(explicit_path).stem,
                "rank": 0,
            }
        else:
            # Instruction-fuzzy mode: when Treta calls replace_deck with a
            # specific track described in `instruction` ("play Hum Pyaar
            # Karne Wale", "One Bottle Down by Honey Singh"), search the
            # local library for a basename match BEFORE falling back to the
            # planner's rank-1 guess. Closes the gap where Treta knows
            # which track she wants but doesn't have a verified path.
            instruction = (intent.get("instruction") or "").strip()
            pick = None
            if instruction:
                pick = self._fuzzy_match_library(instruction)
                if pick:
                    log.info(
                        f"replace_deck (fuzzy): instruction={instruction[:60]!r} "
                        f"→ {pick['title'][:60]!r}"
                    )

            if pick is None:
                tracks = (playlist or {}).get("tracks") or []
                for t in sorted(tracks, key=lambda x: x.get("rank", 99)):
                    if t.get("downloaded") and t.get("path"):
                        pick = t
                        break
            if not pick:
                tracks_n = len((playlist or {}).get("tracks") or [])
                log.warning(
                    f"replace_deck: no downloaded candidate in playlist "
                    f"(have {tracks_n} entries) and fuzzy-match found nothing "
                    f"— skipping eject"
                )
                return False

        track_path = pick["path"]
        title_display = pick.get("title") or Path(track_path).stem

        # Step 1: eject. Mixxx clears the deck's loaded track. Done first
        # so even if the load fails, the wrong-track is gone (silence is
        # better than the wrong song).
        try:
            httpx.post(
                f"{self.config.mixxx.url}/api/control",
                json={"group": f"[Channel{deck}]", "key": "eject", "value": 1},
                timeout=3,
            )
        except Exception as exc:
            log.warning(f"replace_deck: eject failed for deck {deck}: {exc}")

        # Step 2: load the new pick. load_on_deck handles the /api/load
        # POST and waits for Mixxx to confirm.
        ok = load_on_deck(self.config.mixxx.url, deck, track_path)
        if not ok:
            log.warning(f"replace_deck: load_on_deck failed for {track_path!r}")
            return False
        try:
            refresh_duration(self.config.mixxx.url, deck, track_path)
        except Exception:
            pass

        log.info(
            f"replace_deck: deck {deck} ← {title_display[:60]} "
            f"(rank {pick.get('rank')})"
        )
        if hasattr(self, '_ws_broadcast'):
            self._ws_broadcast("log", {
                "text": f"Replaced deck {deck}: {title_display[:50]}"
            })
        return True

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

