"""Perception engine — wraps the Mixxx /api/live and /api/status endpoints.

Simpler than the full listener.py — focused on what the daemon needs:
remaining time, energy estimation, transition readiness.
"""

import time
from dataclasses import dataclass

import httpx

from .config import Config


@dataclass
class DeckPerception:
    playing: bool = False
    bpm: float = 0.0
    key: int = 0
    position_seconds: float = 0.0
    duration: float = 0.0
    remaining_seconds: float = 0.0
    volume: float = 0.0
    track_loaded: bool = False
    vu_left: float = 0.0
    vu_right: float = 0.0
    beat_active: bool = False


@dataclass
class Perception:
    deck1: DeckPerception
    deck2: DeckPerception
    crossfader: float = 0.0
    master_vu: float = 0.0
    active_deck: int = 1
    timestamp: float = 0.0

    @property
    def active(self) -> DeckPerception:
        return self.deck1 if self.active_deck == 1 else self.deck2

    @property
    def idle(self) -> DeckPerception:
        return self.deck2 if self.active_deck == 1 else self.deck1

    def transition_ready(self, lookahead: int = 120) -> bool:
        """Is it time to prepare a transition?"""
        a = self.active
        return a.playing and a.remaining_seconds > 0 and a.remaining_seconds <= lookahead

    def emergency(self) -> bool:
        """Is the active track about to end with nothing loaded?"""
        a = self.active
        return a.playing and 0 < a.remaining_seconds < 15

    def estimate_energy(self) -> float:
        """Rough energy estimate from VU meters (0-10)."""
        vu = max(self.deck1.vu_left + self.deck1.vu_right,
                 self.deck2.vu_left + self.deck2.vu_right) / 2
        return min(10.0, vu * 15)  # VU ~0.0-0.7 → energy 0-10


class PerceptionEngine:
    """Polls Mixxx for state at configurable rate."""

    def __init__(self, config: Config):
        self.mixxx_url = config.mixxx.url
        self.timeout = config.mixxx.timeout
        self.lookahead = config.transitions.lookahead_seconds
        self._client = httpx.Client(base_url=self.mixxx_url, timeout=self.timeout)
        self._last: Perception | None = None

    def poll(self) -> Perception | None:
        """Poll Mixxx for current state. Returns None if Mixxx unreachable."""
        try:
            status = self._client.get("/api/status").json()
            live = self._client.get("/api/live").json()
        except Exception:
            return None  # Mixxx is down — don't return stale data

        d1 = self._parse_deck(status.get("deck1", {}), live.get("deck1", {}))
        d2 = self._parse_deck(status.get("deck2", {}), live.get("deck2", {}))

        # Determine active deck by volume and crossfader
        xf = status.get("crossfader", 0.0)
        if d1.playing and not d2.playing:
            active = 1
        elif d2.playing and not d1.playing:
            active = 2
        elif xf < -0.3:
            active = 1
        elif xf > 0.3:
            active = 2
        else:
            active = 1 if d1.volume >= d2.volume else 2

        master_vu = (live.get("master_vu_left", 0) + live.get("master_vu_right", 0)) / 2

        self._last = Perception(
            deck1=d1,
            deck2=d2,
            crossfader=xf,
            master_vu=master_vu,
            active_deck=active,
            timestamp=time.time(),
        )
        return self._last

    def _parse_deck(self, status: dict, live: dict) -> DeckPerception:
        return DeckPerception(
            playing=status.get("playing", False),
            bpm=status.get("bpm", 0.0),
            key=status.get("key", 0),
            position_seconds=status.get("position_seconds", 0.0),
            duration=status.get("duration", 0.0),
            remaining_seconds=status.get("remaining_seconds", 0.0),
            volume=status.get("volume", 0.0),
            track_loaded=status.get("track_loaded", False),
            vu_left=live.get("vu_left", 0.0),
            vu_right=live.get("vu_right", 0.0),
            beat_active=live.get("beat_active", False),
        )

    @property
    def last(self) -> Perception | None:
        return self._last

    def close(self):
        self._client.close()
