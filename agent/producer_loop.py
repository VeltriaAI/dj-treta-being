"""Producer loop — root peer thread that generates original Treta tracks.

v8 Phase 6: producer no longer lives as twin sub-agent copies under DJ
and Planner. One canonical peer that watches session.producer_need and
invokes Lyria 3 via the generate_track tool. Phase 6.5 adds KB-enriched
briefs.

Budget guardrails: config.producer.max_per_day caps the day's generations.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("dj-treta")

MIN_CYCLE_INTERVAL_S = 30


class ProducerMixin:

    def _producer_loop(self):
        """Background thread: fulfil session.producer_need signals."""
        if not self.config.producer.enabled or not self.config.sources.treta_originals:
            log.info(
                "Producer loop: disabled "
                "(config.producer.enabled=false or treta_originals=false)"
            )
            return

        time.sleep(15)  # let other loops warm up
        last_cycle = 0.0
        self._producer_count_today = 0
        self._producer_day = datetime.now(timezone.utc).date()

        while self._running:
            try:
                # Reset daily count on day change
                today = datetime.now(timezone.utc).date()
                if today != self._producer_day:
                    self._producer_count_today = 0
                    self._producer_day = today

                need = getattr(self.session, "producer_need", None)
                if need and isinstance(need, dict):
                    if self._producer_count_today >= getattr(
                        self.config.producer, "max_per_day", 4
                    ):
                        log.info("Producer: daily cap reached, skipping")
                        self.session.producer_need = None
                    elif time.time() - last_cycle >= MIN_CYCLE_INTERVAL_S:
                        last_cycle = time.time()
                        self._producer_fulfil(need)
                        self._producer_count_today += 1
            except Exception as exc:
                log.warning(f"Producer loop error: {exc}")
            time.sleep(5)

    def _producer_fulfil(self, need: dict) -> None:
        mood = need.get("mood", "") or self.mood or "melodic-techno"
        brief = need.get("brief", "")
        bpm = need.get("bpm")
        key = need.get("key", "")
        name_hint = need.get("name", "")

        mood_profile = getattr(self.session, "mood_profile", None) or {}
        bpm_range = mood_profile.get("bpm_range") or []
        if not bpm and bpm_range and len(bpm_range) == 2:
            bpm = (bpm_range[0] + bpm_range[1]) // 2
        vibe = ", ".join(mood_profile.get("vibe_keywords", [])[:5])

        log.info(f"Producer: generating — mood={mood} bpm={bpm} key={key}")

        instruction = (
            f"Generate ONE original track for DJ Treta.\n\n"
            f"Mood / genre: {mood}\n"
            f"Vibe keywords: {vibe or '(use your judgement)'}\n"
            f"BPM: {bpm or 'pick an appropriate BPM for the mood'}\n"
            f"Key: {key or 'pick a key that fits'}\n"
            f"Name hint: {name_hint or '(choose creatively)'}\n"
            f"Brief: {brief or '(describe texture/mood/instruments yourself)'}\n\n"
            f"Call generate_track with precise parameters. Tag the track with "
            f"genre='{mood}' — NOT 'ai-generated'. Report a one-line summary "
            f"of what you made."
        )

        try:
            result = self._invoke_producer(instruction)
            log.info(f"Producer done: {str(result)[:200]}")
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("log", {"text": f"Producer generated {mood}: {str(result)[:150]}"})
        except Exception as exc:
            log.warning(f"Producer fulfil failed: {exc}")
        finally:
            self.session.producer_need = None
