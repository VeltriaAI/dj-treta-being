"""DJ state machine — phases, track state, and set state."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DJPhase(str, Enum):
    STARTING = "starting"
    PLAYING = "playing"
    PREPARING = "preparing"
    TRANSITIONING = "transitioning"
    RECOVERY = "recovery"
    STOPPED = "stopped"


@dataclass
class TrackState:
    path: str = ""
    title: str = ""
    artist: str = ""
    bpm: float = 0.0
    key: str = ""
    camelot: str = ""
    energy: int = 5
    duration: float = 0.0
    remaining: float = 0.0
    genre: str = ""


@dataclass
class DJState:
    phase: DJPhase = DJPhase.STOPPED
    active_deck: int = 1
    idle_deck: int = 2
    current_track: TrackState = field(default_factory=TrackState)
    next_track: Optional[TrackState] = None
    transition_technique: str = ""
    transition_duration: int = 60
    set_start_time: float = 0.0
    set_duration_target: int = 3600  # seconds
    tracks_played: list = field(default_factory=list)
    mood: str = "techno-deep"
    energy_arc: list = field(default_factory=list)  # [(timestamp, energy)]
    consecutive_errors: int = 0
    last_brain_call: float = 0.0
    last_command: str = ""
    last_command_result: str = ""

    @property
    def set_elapsed(self) -> float:
        if self.set_start_time == 0:
            return 0
        return time.time() - self.set_start_time

    @property
    def set_remaining(self) -> float:
        elapsed = self.set_elapsed
        return max(0, self.set_duration_target - elapsed)

    def swap_decks(self):
        self.active_deck, self.idle_deck = self.idle_deck, self.active_deck

    def record_track(self, track: TrackState):
        self.tracks_played.append({
            "title": track.title,
            "artist": track.artist,
            "bpm": track.bpm,
            "key": track.key,
            "energy": track.energy,
            "played_at": time.time(),
        })

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "active_deck": self.active_deck,
            "current_track": {
                "title": self.current_track.title,
                "artist": self.current_track.artist,
                "bpm": self.current_track.bpm,
                "key": self.current_track.key,
                "remaining": self.current_track.remaining,
            },
            "next_track": {
                "title": self.next_track.title,
                "artist": self.next_track.artist,
            } if self.next_track else None,
            "mood": self.mood,
            "tracks_played": len(self.tracks_played),
            "set_elapsed": round(self.set_elapsed),
            "set_remaining": round(self.set_remaining),
            "consecutive_errors": self.consecutive_errors,
            "last_command": self.last_command,
            "last_command_result": self.last_command_result,
        }
