"""Library manager loop — root peer thread that grows the library.

v8 Phase 5: library is no longer a DJ sub-agent. It runs as its own
thread, watches `session.library_need` signals from the planner, and
fulfils them via search_music + download_track (which already handle
3-layer canonical dedup + background enrichment).

Also runs a proactive gap-check on mood changes so new mood = eventually
downloaded tracks without the planner having to beg explicitly.
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("dj-treta")

# Max download cycle rate — avoid hammering yt-dlp.
MIN_CYCLE_INTERVAL_S = 30


class LibraryMixin:

    def _library_loop(self):
        """Background thread: fulfil session.library_need signals."""
        if not self.config.sources.youtube:
            log.info("Library loop: youtube disabled — thread exits (nothing to do)")
            return

        time.sleep(10)  # let planner + DJ warm up first
        last_cycle = 0.0

        while self._running:
            try:
                need = getattr(self.session, "library_need", None)
                if need and isinstance(need, dict) and need.get("mood"):
                    now = time.time()
                    if now - last_cycle >= MIN_CYCLE_INTERVAL_S:
                        last_cycle = now
                        self._library_fulfil(need)
                    else:
                        # Too soon after last fulfil — wait
                        pass
            except Exception as exc:
                log.warning(f"Library loop error: {exc}")
            time.sleep(5)

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

        instruction = (
            f"The planner signalled that the library needs more tracks for mood "
            f"'{mood}'{bpm_hint}.\n"
            f"Vibe keywords: {vibe or 'none specified'}.\n"
            f"Reason: {reason or 'library thin for this mood'}.\n\n"
            f"Please:\n"
            f"1. Call list_library_tracks to see what's already in the "
            f"   {mood} genre folder (avoid duplicates).\n"
            f"2. Craft 2-3 diverse YouTube search queries for {mood}.\n"
            f"3. Call search_music on each, pick {count} distinct tracks "
            f"(different artists, 2-10 min, no mixes/compilations).\n"
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
