"""Library manager loop — root peer thread that grows the library.

v8 Phase 5: library is no longer a DJ sub-agent. It runs as its own
thread, watches `session.library_need` signals from the planner, and
fulfils them via search_music + download_track (which already handle
3-layer canonical dedup + background enrichment).

Also runs a proactive gap-check on mood changes so new mood = eventually
downloaded tracks without the planner having to beg explicitly.

v9 Phase K5: the loop ALSO handles a second `library_need` shape — a
targeted per-track request from the knowledge-planner:

    {
        "video_id": "...",
        "title": "Anyma - Syren",
        "mbid": "...",
        "youtube_music_url": "https://music.youtube.com/watch?v=...",
        "canonical_artist": "Anyma",
        "canonical_song": "Syren",
        "reason": "planner rank-1 pick not downloaded",
        "ts": 1776000000.0,
    }

Schema sniff at tick-top: `video_id` present → targeted K5 flow;
`mood` present → legacy v8 mood-refill flow. Both coexist.
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("dj-treta")

# Max download cycle rate — avoid hammering yt-dlp.
MIN_CYCLE_INTERVAL_S = 30

# K5: max download attempts per targeted video_id before giving up.
K5_MAX_ATTEMPTS = 3


class LibraryMixin:

    def _library_loop(self):
        """Background thread: fulfil session.library_need signals."""
        if not self.config.sources.youtube:
            log.info("Library loop: youtube disabled — thread exits (nothing to do)")
            return

        time.sleep(10)  # let planner + DJ warm up first
        last_cycle = 0.0

        # K5 bookkeeping — lazy-init so mixin stays simple.
        # These live on the being instance because LibraryMixin has no __init__.
        if not hasattr(self, "_library_download_busy"):
            self._library_download_busy = False
        if not hasattr(self, "_library_last_consumed_ts"):
            self._library_last_consumed_ts = 0.0
        if not hasattr(self, "_library_failure_counts"):
            self._library_failure_counts = {}  # {video_id: int}

        while self._running:
            try:
                # Meta-control: Treta can pause the library when she
                # wants to halt downloads (e.g. during quiet talk).
                if getattr(self.session, "library_paused", False):
                    time.sleep(5)
                    continue

                from .observability import tick as _obs_tick
                _obs_tick("library")
                need = getattr(self.session, "library_need", None)
                if need and isinstance(need, dict):
                    # K5 shape first — targeted download for a specific track.
                    if need.get("video_id") and not self._library_download_busy:
                        ts = float(need.get("ts") or 0.0)
                        if ts > self._library_last_consumed_ts:
                            self._library_last_consumed_ts = ts or time.time()
                            self._library_download_busy = True
                            try:
                                self._library_handle_targeted(need)
                            finally:
                                self._library_download_busy = False
                    # Legacy v8 shape — mood-refill.
                    elif need.get("mood"):
                        now = time.time()
                        if now - last_cycle >= MIN_CYCLE_INTERVAL_S:
                            last_cycle = now
                            self._library_fulfil(need)
                        else:
                            # Too soon after last fulfil — wait
                            pass

                # Sarathi proactive deep-queue: Manish drives the decks, so she
                # spends spare cycles growing the library to keep ≥10 unplayed
                # current-mood tracks ready. Gated strictly on sarathi_mode so
                # auto mode is untouched. Skips while a targeted/legacy download
                # is in flight (busy flag). One track per ~25s tick.
                if (getattr(self.session, "sarathi_mode", False)
                        and not self._library_download_busy):
                    now = time.time()
                    if now - getattr(self, "_sarathi_dl_last", 0.0) >= 25:
                        self._sarathi_dl_last = now
                        self._library_download_busy = True
                        threading.Thread(
                            target=self._sarathi_proactive_download,
                            daemon=True,
                        ).start()
            except Exception as exc:
                log.warning(f"Library loop error: {exc}")
            time.sleep(5)

    # ── K5: targeted single-track download ────────────────────────────

    def _library_handle_targeted(self, need: dict) -> None:
        """Handle a K5-shape library_need: download one specific track.

        Flow:
          1. dedup check — skip if mbid or canonical tuple already local
          2. download via existing 3-layer flow (download_track)
          3. backfill mbid on success (if schema supports it)
          4. emit library_ready on success, library_need_failed on 3 fails
          5. clear library_need in all terminal cases
        """
        video_id = need.get("video_id") or ""
        title = need.get("title") or ""
        mbid = need.get("mbid") or ""
        canonical_artist = (need.get("canonical_artist") or "").strip()
        canonical_song = (need.get("canonical_song") or "").strip()
        url = need.get("youtube_music_url") or (
            f"https://music.youtube.com/watch?v={video_id}" if video_id else ""
        )

        log.info(
            f"[library] signal received: {title} (mbid={(mbid or '')[:8]})"
        )

        # Guard: no URL to download from.
        if not url:
            log.warning("[library] targeted need has no url and no video_id — clearing")
            self.session.library_need = None
            return

        # Layer 1: dedup against DB by mbid OR canonical tuple.
        if self._k5_dedup_hit(mbid, canonical_artist, canonical_song):
            log.info(
                f"[library] dedup hit: {canonical_artist} - {canonical_song} already local"
            )
            self.session.library_need = None
            return

        # Layer 2: download via the existing 3-layer discovery flow.
        # Retry up to K5_MAX_ATTEMPTS for this video_id across ticks.
        attempts = self._library_failure_counts.get(video_id, 0)
        try:
            from .tools.discovery import download_track
        except Exception as exc:
            log.warning(f"[library] import download_track failed: {exc}")
            self.session.library_need = None
            return

        # Infer a genre folder from mood_profile if available, else 'unsorted'.
        genre = "unsorted"
        try:
            mp = getattr(self.session, "mood_profile", None) or {}
            genre = (mp.get("canonical_slug") or mp.get("canonical_genre")
                     or getattr(self.session, "mood", "") or "unsorted").strip().lower() or "unsorted"
        except Exception:
            pass

        log.info(f"[library] downloading via {url}")
        try:
            result = download_track(url, genre=genre)
        except Exception as exc:
            result = {"ok": False, "path": None, "message": f"Download failed: {exc}"}

        # download_track now returns a dict; flatten back into the
        # boolean signals this loop cares about. The string form is
        # preserved for log lines + downstream text-only consumers.
        if isinstance(result, dict):
            result_str = result.get("message", "") or ""
            is_failure = not result.get("ok", False)
            is_already = "ALREADY EXISTS" in result_str
        else:
            # Defensive: handle pre-refactor string returns just in case.
            result_str = str(result or "")
            is_failure = result_str.startswith("Download failed")
            is_already = result_str.startswith("ALREADY EXISTS")

        if is_failure:
            attempts += 1
            self._library_failure_counts[video_id] = attempts
            log.warning(f"[library] download failed ({attempts}/{K5_MAX_ATTEMPTS}): {result_str[:200]}")
            if attempts >= K5_MAX_ATTEMPTS:
                self.session.library_need_failed = {
                    "video_id": video_id,
                    "mbid": mbid,
                    "reason": result_str[:200],
                    "ts": time.time(),
                }
                self.session.library_need = None
                # Clear retry counter so a fresh signal gets full K5_MAX_ATTEMPTS again.
                self._library_failure_counts.pop(video_id, None)
            # On non-terminal failures, leave library_need intact so next tick retries.
            return

        # Success (either new download or canonical-already). Clear counter.
        self._library_failure_counts.pop(video_id, None)

        # Locate the freshly-inserted track in DB so we can backfill mbid + emit ready.
        track_row = self._k5_locate_track(url, canonical_artist, canonical_song)

        # Backfill mbid if the column exists (schema-sniff first).
        if mbid and track_row:
            try:
                self._k5_backfill_mbid(track_row.get("id"), mbid)
            except Exception as exc:
                log.warning(f"[library] mbid backfill skipped: {exc}")

        # Pull analysis snapshot for observability (download_track already
        # kicked off _enrich_track in a background thread — analysis may
        # still be in-flight, which is fine; planner re-verifies next tick).
        bpm = key = energy = None
        if track_row:
            bpm = track_row.get("bpm")
            key = track_row.get("key_musical") or track_row.get("key_camelot")
            energy = track_row.get("energy_peak")
        log.info(f"[library] analyzed: bpm={bpm} key={key} energy={energy}")

        # Emit library_ready so planner can re-verify on next tick.
        self.session.library_ready = {
            "mbid": mbid,
            "path": (track_row or {}).get("path") if track_row else "",
            "title": title,
            "ts": time.time(),
        }
        self.session.library_need = None
        # Force planner to re-plan now — the newly-downloaded track should
        # surface as LOCAL in the next playlist, and if more dataset picks
        # remain undownloaded the planner will emit a fresh library_need
        # for the next rank. Without this, planner waits up to 2 tracks
        # before re-planning and v9 playlists stay stuck on DATASET-only
        # rows that DJ can't load_track.
        self.session.replan_requested = True

    # ── Sarathi: proactive deep-queue download ────────────────────────

    def _sarathi_proactive_download(self) -> None:
        """Grow the current-mood library one track per call while in Sarathi.

        In Sarathi mode Manish drives the transitions, so the knowledge-graph
        planner isn't surfacing dataset picks to download. This keeps the deck
        deep: she searches YouTube Music for the current mood and pulls one new
        track per tick (loop-throttled to ~25s) until the genre folder holds a
        comfortable buffer (TARGET). Dedup is filename-based against what's
        already on disk plus the DB canonical check inside download_track.

        Always clears `_library_download_busy` in finally so the loop recovers.
        """
        TARGET = 25  # keep growing past the ~10-ahead floor Manish asked for
        try:
            from pathlib import Path
            from .tools.helpers import _music_dir
            from .tools.discovery import search_music, download_track

            # Mood → genre slug (mirrors _library_handle_targeted logic).
            mp = getattr(self.session, "mood_profile", None) or {}
            slug = (mp.get("canonical_slug") or mp.get("canonical_genre")
                    or getattr(self.session, "mood", "") or "melodic-techno")
            slug = (slug or "melodic-techno").strip().lower() or "melodic-techno"

            genre_dir = Path(_music_dir()) / slug
            existing = []
            if genre_dir.is_dir():
                existing = [p.name.lower() for p in genre_dir.glob("*.mp3")
                            if not p.name.startswith("._")]
            if len(existing) >= TARGET:
                log.debug(f"[sarathi] deep-queue full ({len(existing)}/{TARGET}) — skip")
                return

            # Search the current mood — anchor the query on the canonical genre
            # so YouTube ranks electronic tracks first, not vocal-pop edits that
            # happen to share a keyword. Bias toward DJ/club material.
            genre_label = (mp.get("canonical_genre") or slug.replace("-", " ")).strip()
            alts = [a for a in (mp.get("alternates") or []) if a][:1]
            query = f"{genre_label} {' '.join(alts)}".strip()
            results = search_music(query=query, limit=15) or []

            from .canonicalize import genre_matches
            picked = None
            for r in results:
                if not isinstance(r, dict) or not r.get("url"):
                    continue
                artist = (r.get("artist") or "").strip()
                title = (r.get("title") or "").strip()
                if not (artist or title):
                    continue
                # crude filename dedup — skip if a clear stem already on disk
                stem = title.lower()[:18]
                if stem and any(stem in fn for fn in existing):
                    continue
                # Genre gate — reject Bollywood / off-genre results that YT
                # surfaces for a loose query. Strict flash check (fail-open).
                if not genre_matches(artist, title, genre_label,
                                     mp.get("alternates") or []):
                    log.info(f"[sarathi] deep-queue: skip off-genre '{artist} - {title}'")
                    continue
                picked = r
                break

            if not picked:
                log.debug(f"[sarathi] deep-queue: no on-genre result for '{query}'")
                return

            log.info(
                f"[sarathi] deep-queue download ({len(existing)+1}/{TARGET}): "
                f"{picked.get('artist','')} - {picked.get('title','')}"
            )
            try:
                result = download_track(picked["url"], genre=slug)
                ok = bool(result.get("ok")) if isinstance(result, dict) else False
                msg = (result.get("message", "") if isinstance(result, dict)
                       else str(result))[:120]
                log.info(f"[sarathi] deep-queue result: ok={ok} {msg}")
                if ok:
                    # surface as LOCAL on next plan
                    self.session.replan_requested = True
            except Exception as exc:
                log.warning(f"[sarathi] deep-queue download failed: {exc}")
        except Exception as exc:
            log.debug(f"[sarathi] proactive download error: {exc}")
        finally:
            self._library_download_busy = False

    def _k5_dedup_hit(self, mbid: str, canonical_artist: str,
                      canonical_song: str) -> bool:
        """Return True if a track with this mbid or canonical tuple is already in DB."""
        try:
            from .db import get_db
        except Exception:
            return False
        try:
            db = get_db()
        except Exception:
            return False
        try:
            # mbid check — only if column exists.
            if mbid and self._k5_has_mbid_column(db):
                row = db.execute(
                    "SELECT id FROM tracks WHERE mbid = ? LIMIT 1", (mbid,)
                ).fetchone()
                if row:
                    return True
            # canonical tuple check — case-insensitive on both fields.
            if canonical_artist and canonical_song:
                row = db.execute(
                    "SELECT id FROM tracks "
                    "WHERE LOWER(canonical_artist) = LOWER(?) "
                    "  AND LOWER(canonical_song) = LOWER(?) "
                    "LIMIT 1",
                    (canonical_artist, canonical_song),
                ).fetchone()
                if row:
                    return True
        finally:
            try:
                db.close()
            except Exception:
                pass
        return False

    def _k5_has_mbid_column(self, db) -> bool:
        """PRAGMA check — True if tracks.mbid exists."""
        try:
            cols = {r["name"] for r in db.execute("PRAGMA table_info(tracks)").fetchall()}
            return "mbid" in cols
        except Exception:
            return False

    def _k5_backfill_mbid(self, track_id, mbid: str) -> None:
        """Write mbid onto tracks row if the column exists. No-op otherwise."""
        if not track_id or not mbid:
            return
        try:
            from .db import get_db
        except Exception:
            return
        db = get_db()
        try:
            if not self._k5_has_mbid_column(db):
                return  # schema doesn't support mbid — skip gracefully
            db.execute("UPDATE tracks SET mbid = ? WHERE id = ?", (mbid, track_id))
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _k5_locate_track(self, url: str, canonical_artist: str,
                         canonical_song: str) -> dict | None:
        """Find the track row just inserted by download_track.

        Tries source_url first (exact match, set by download_track on new insert),
        then falls back to canonical tuple (handles the 'already exists' path).
        """
        try:
            from .db import find_track_by_source_url, find_track_by_canonical
        except Exception:
            return None
        if url:
            row = find_track_by_source_url(url)
            if row:
                return row
        if canonical_artist and canonical_song:
            # version/remixer unknown here — fall back to a lowered-pair lookup.
            try:
                from .db import get_db
                db = get_db()
                try:
                    r = db.execute(
                        "SELECT * FROM tracks "
                        "WHERE LOWER(canonical_artist) = LOWER(?) "
                        "  AND LOWER(canonical_song) = LOWER(?) "
                        "ORDER BY id DESC LIMIT 1",
                        (canonical_artist, canonical_song),
                    ).fetchone()
                    return dict(r) if r else None
                finally:
                    db.close()
            except Exception:
                return None
        return None

    def _library_fulfil(self, need: dict) -> None:
        """Run one library-fill cycle for the given need signal."""
        mood = need.get("mood", "")
        count = int(need.get("count") or 3)
        reason = need.get("reason", "")

        if not mood:
            log.warning("Library need with no mood — clearing")
            self.session.library_need = None
            return

        log.info(f"Library: fulfilling need — mood={mood} count={count} ({reason})")

        mood_profile = getattr(self.session, "mood_profile", None) or {}
        vibe = ", ".join(mood_profile.get("vibe_keywords", [])[:4])
        bpm_range = mood_profile.get("bpm_range") or []
        bpm_hint = ""
        if bpm_range and len(bpm_range) == 2:
            bpm_hint = f" (typical BPM {bpm_range[0]}-{bpm_range[1]})"

        # ≤2-line workspace slice from the shared notebook so the library agent
        # stays aligned with the latest mood/decision (guard None; never raise).
        workspace_slice = ""
        try:
            from .notebook import get_notebook
            _nb = get_notebook()
            if _nb is not None:
                _nv = _nb.now_view() or {}
                _md = _nv.get("mood") or mood
                _dec = _nb.find_last("decision")
                _dtxt = str((_dec or {}).get("payload"))[:120] if _dec else "none"
                workspace_slice = (
                    f"Workspace now: mood={_md}; latest decision={_dtxt}.\n"
                )
        except Exception:
            workspace_slice = ""

        instruction = (
            f"The planner signalled that the library needs more tracks for mood "
            f"'{mood}'{bpm_hint}.\n"
            f"Vibe keywords: {vibe or 'none specified'}.\n"
            f"{workspace_slice}"
            f"Reason: {reason or 'library thin for this mood'}.\n\n"
            f"Please:\n"
            f"1. Call list_library_tracks to see what's already in the "
            f"   {mood} genre folder (avoid duplicates).\n"
            f"2. Craft 2-3 diverse YouTube search queries for {mood}.\n"
            f"3. Call search_music on each, pick {count} distinct tracks "
            f"(different artists, 2-10 min, no mixes/compilations).\n"
            f"   STRICT genre rule: only pick tracks that are genuinely "
            f"'{mood}' in an electronic/club sense. Reject Bollywood, "
            f"Hindi/Punjabi film songs, and vocal-pop edits unless they are "
            f"clearly an electronic remix — they pollute the crate.\n"
            f"4. Call download_track(url, genre='{mood}') for each.\n"
            f"5. Report a one-line summary of what you added.\n"
        )

        try:
            result = self._invoke_library(instruction)
            log.info(f"Library done: {str(result)[:200]}")
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("log", {"text": f"Library filled {mood}: {str(result)[:150]}"})
        except Exception as exc:
            log.warning(f"Library fulfil failed: {exc}")
        finally:
            # Clear the need signal (consumed). Planner can set it again
            # next tick if library is still thin.
            self.session.library_need = None
