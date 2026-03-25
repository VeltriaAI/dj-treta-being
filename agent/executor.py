"""Deterministic transition executor.

Brain PLANS, executor EXECUTES. No LLM here — pure timing and math.
Four techniques: blend, bass_swap, filter_sweep, hard_cut.
Runs at configurable FPS (default 20), sends Mixxx API calls.
"""

import asyncio
import math
import time
from typing import Callable

import httpx

from .config import Config


class TransitionExecutor:
    """Executes transitions deterministically at high frame rate."""

    def __init__(self, config: Config):
        self.mixxx_url = config.mixxx.url
        self.fps = config.transitions.fps
        self.timeout = config.mixxx.timeout
        self._client = httpx.Client(base_url=self.mixxx_url, timeout=self.timeout)

    def execute(self, technique: str, incoming_deck: int, duration: int,
                on_progress: Callable[[float], None] | None = None) -> dict:
        """Execute a transition synchronously.

        Args:
            technique: blend, bass_swap, filter_sweep, hard_cut
            incoming_deck: deck number to transition TO (1 or 2)
            duration: transition duration in seconds
            on_progress: optional callback with progress 0.0-1.0

        Returns:
            dict with status and timing info
        """
        outgoing_deck = 2 if incoming_deck == 1 else 1
        start = time.time()

        # ── Pre-flight safety checks ──
        if not self._verify_deck_ready(incoming_deck):
            return {"status": "aborted", "error": f"Deck {incoming_deck} not ready (no track loaded or not playing)", "technique": technique}

        executors = {
            "blend": self._blend,
            "bass_swap": self._bass_swap,
            "filter_sweep": self._filter_sweep,
            "hard_cut": self._hard_cut,
        }

        fn = executors.get(technique, self._blend)

        try:
            fn(outgoing_deck, incoming_deck, duration, on_progress)
            elapsed = time.time() - start
            self._reset_eq(incoming_deck)
            self._reset_eq(outgoing_deck)

            # ── Post-flight safety: verify incoming deck is audible ──
            self._verify_audible(incoming_deck, outgoing_deck)

            return {"status": "complete", "technique": technique, "duration": round(elapsed, 1)}
        except Exception as e:
            # Emergency: ensure incoming deck is audible
            self._emergency_swap(incoming_deck, outgoing_deck)
            return {"status": "error", "error": str(e), "technique": technique}

    def _blend(self, out_deck: int, in_deck: int, duration: int,
               on_progress: Callable | None):
        """Classic crossfader blend with S-curve."""
        frame_delay = 1.0 / self.fps
        total_frames = int(duration * self.fps)

        # Ensure incoming deck is playing
        self._post("/api/play", {"deck": in_deck})

        for i in range(total_frames + 1):
            t = i / total_frames  # 0.0 → 1.0

            # S-curve for smooth crossfade
            curve = (1 - math.cos(t * math.pi)) / 2

            # Crossfader: -1.0 (deck1) → 1.0 (deck2)
            if in_deck == 2:
                xfader = -1.0 + (2.0 * curve)
            else:
                xfader = 1.0 - (2.0 * curve)

            self._post("/api/crossfade", {"position": round(xfader, 4)})

            if on_progress:
                on_progress(t)

            time.sleep(frame_delay)

    def _bass_swap(self, out_deck: int, in_deck: int, duration: int,
                   on_progress: Callable | None):
        """EQ bass swap — techno style.

        Phase 1 (0-40%): Bring in incoming with bass cut, blend up
        Phase 2 (40-60%): Swap bass — cut outgoing, restore incoming
        Phase 3 (60-100%): Fade out outgoing
        """
        frame_delay = 1.0 / self.fps
        total_frames = int(duration * self.fps)

        # Start: incoming has no bass
        self._post("/api/eq", {"deck": in_deck, "band": "lo", "value": 0.0})
        self._post("/api/play", {"deck": in_deck})

        for i in range(total_frames + 1):
            t = i / total_frames

            if t <= 0.4:
                # Phase 1: bring in incoming (hi/mid only)
                blend = t / 0.4
                self._post("/api/volume", {"deck": in_deck, "volume": round(blend, 3)})
            elif t <= 0.6:
                # Phase 2: bass swap at midpoint
                swap_t = (t - 0.4) / 0.2
                self._post("/api/eq", {"deck": out_deck, "band": "lo", "value": round(1.0 - swap_t, 3)})
                self._post("/api/eq", {"deck": in_deck, "band": "lo", "value": round(swap_t, 3)})
            else:
                # Phase 3: fade out outgoing
                fade = 1.0 - ((t - 0.6) / 0.4)
                self._post("/api/volume", {"deck": out_deck, "volume": round(fade, 3)})
                self._post("/api/eq", {"deck": out_deck, "band": "hi", "value": round(fade, 3)})
                self._post("/api/eq", {"deck": out_deck, "band": "mid", "value": round(fade, 3)})

            if on_progress:
                on_progress(t)

            time.sleep(frame_delay)

    def _filter_sweep(self, out_deck: int, in_deck: int, duration: int,
                      on_progress: Callable | None):
        """HPF sweep on incoming, gradually reveal.

        The incoming track starts with full HPF (tinny), then the filter
        opens progressively to reveal the full sound. Meanwhile, outgoing fades.
        """
        frame_delay = 1.0 / self.fps
        total_frames = int(duration * self.fps)

        # Start: incoming at full HPF
        self._post("/api/filter", {"deck": in_deck, "value": 0.0})
        self._post("/api/play", {"deck": in_deck})
        self._post("/api/volume", {"deck": in_deck, "volume": 1.0})

        for i in range(total_frames + 1):
            t = i / total_frames

            # Open filter on incoming: 0.0 (HPF) → 0.5 (neutral)
            filter_val = t * 0.5
            self._post("/api/filter", {"deck": in_deck, "value": round(filter_val, 3)})

            # Fade out outgoing in second half
            if t > 0.5:
                fade = 1.0 - ((t - 0.5) / 0.5)
                self._post("/api/volume", {"deck": out_deck, "volume": round(fade, 3)})

            if on_progress:
                on_progress(t)

            time.sleep(frame_delay)

        # Reset filter to neutral
        self._post("/api/filter", {"deck": in_deck, "value": 0.5})

    def _hard_cut(self, out_deck: int, in_deck: int, duration: int,
                  on_progress: Callable | None):
        """Hard cut with brief overlap. Duration is mostly ignored — quick swap."""
        self._post("/api/play", {"deck": in_deck})
        self._post("/api/volume", {"deck": in_deck, "volume": 1.0})
        time.sleep(0.5)  # brief overlap
        self._post("/api/volume", {"deck": out_deck, "volume": 0.0})

        if on_progress:
            on_progress(1.0)

    def _reset_eq(self, deck: int):
        """Reset EQ to neutral after transition."""
        for band in ("hi", "mid", "lo"):
            self._post("/api/eq", {"deck": deck, "band": band, "value": 1.0})
        self._post("/api/filter", {"deck": deck, "value": 0.5})

    def _emergency_swap(self, in_deck: int, out_deck: int):
        """Emergency: ensure incoming is audible, silence outgoing."""
        try:
            self._post("/api/volume", {"deck": in_deck, "volume": 1.0})
            self._post("/api/volume", {"deck": out_deck, "volume": 0.0})
            self._reset_eq(in_deck)
        except Exception:
            pass  # best effort

    def _verify_deck_ready(self, deck: int) -> bool:
        """Pre-flight: ensure deck has a track loaded. Start playback if needed."""
        try:
            status = self._client.get("/api/status").json()
            deck_state = status.get(f"deck{deck}", {})

            if not deck_state.get("track_loaded", False):
                return False

            # If track loaded but not playing, start it
            if not deck_state.get("playing", False):
                self._post("/api/play", {"deck": deck})
                time.sleep(0.3)

            return True
        except Exception:
            return False

    def _verify_audible(self, in_deck: int, out_deck: int):
        """Post-flight: verify the incoming deck is actually audible after transition."""
        try:
            status = self._client.get("/api/status").json()
            in_state = status.get(f"deck{in_deck}", {})
            crossfader = status.get("crossfader", 0.0)

            # Check incoming is playing
            if not in_state.get("playing", False):
                self._post("/api/play", {"deck": in_deck})

            # Check volume isn't zero
            if in_state.get("volume", 0) < 0.1:
                self._post("/api/volume", {"deck": in_deck, "volume": 1.0})

            # Check crossfader is on the right side
            expected_side = 1.0 if in_deck == 2 else -1.0
            # If crossfader is more than 60% away from incoming, fix it
            if in_deck == 2 and crossfader < -0.2:
                self._post("/api/crossfade", {"position": 1.0})
            elif in_deck == 1 and crossfader > 0.2:
                self._post("/api/crossfade", {"position": -1.0})

            # Silence outgoing
            self._post("/api/volume", {"deck": out_deck, "volume": 0.0})

        except Exception:
            pass  # best effort

    def _post(self, path: str, data: dict) -> dict:
        resp = self._client.post(path, json=data)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()
