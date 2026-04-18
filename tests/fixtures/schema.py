"""Pydantic schemas for the transition eval fixture database.

`tracks.yaml` holds canonical per-track metadata (BPM, key, energy, full
section timeline).

`transitions.yaml` holds scenario pairs — "given these two tracks at this
state, what SHOULD the DJ do?" — with ground-truth expected technique +
phrase-aligned position + genre-appropriate duration.

Loaded at pytest collection time via `tests/fixtures/loader.py`. Schema
validation catches fixture corruption before any eval runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# Camelot wheel — all 24 valid keys
_CAMELOT_KEYS = {f"{n}{letter}" for n in range(1, 13) for letter in ("A", "B")}

Technique = Literal["crossfade", "bass_swap", "filter_sweep", "echo_out", "hard_cut"]
Section = Literal[
    "intro", "groove", "buildup", "drop", "breakdown", "outro",
    "verse", "chorus", "bridge",  # less common, for vocal/melodic tracks
]


class TimelineEntry(BaseModel):
    start: float = Field(ge=0, description="section start in seconds")
    end: float = Field(gt=0, description="section end in seconds")
    section: Section
    energy: int = Field(ge=1, le=10, description="1 = ambient, 10 = peak-time")

    @model_validator(mode="after")
    def _check_order(self) -> "TimelineEntry":
        if self.end <= self.start:
            raise ValueError(f"section end ({self.end}) must exceed start ({self.start})")
        return self


class Track(BaseModel):
    """A single track in the fixture database."""

    id: str = Field(pattern=r"^[a-z0-9_]+$", description="canonical ID for scenario references")

    # Canonical identity
    canonical_artist: str
    canonical_song: str
    canonical_version: Optional[str] = "Original Mix"
    remixer: Optional[str] = None

    # Audio-measured
    bpm: float = Field(gt=40, lt=250, description="verified BPM")
    key_musical: str  # e.g. "Am", "C#m", "F"
    key_camelot: str  # e.g. "8A", "12B"
    duration_seconds: float = Field(gt=30, description="total track length")
    energy_peak: int = Field(ge=1, le=10)

    # Genre + mood
    genre: str
    mood_descriptors: list[str] = Field(default_factory=list)

    # Full section timeline — the critical ground truth
    timeline: list[TimelineEntry]

    # Derived / genre-default
    phrase_beats: int = Field(default=32, description="bars × 4 for 4/4 time")

    # Mix safety windows
    mix_in_s: Optional[float] = None   # earliest safe entry point
    mix_out_s: Optional[float] = None  # latest safe exit point

    # Provenance
    source_url: Optional[str] = None
    local_path: Optional[str] = None
    verified_by: Optional[str] = None

    @field_validator("key_camelot")
    @classmethod
    def _valid_camelot(cls, v: str) -> str:
        if v not in _CAMELOT_KEYS:
            raise ValueError(f"{v!r} is not a valid Camelot key (1A-12B)")
        return v

    @property
    def phrase_seconds(self) -> float:
        """Length of one standard phrase at this track's BPM."""
        return (60.0 / self.bpm) * self.phrase_beats

    @model_validator(mode="after")
    def _check_timeline(self) -> "Track":
        if not self.timeline:
            raise ValueError(f"track {self.id}: timeline cannot be empty")
        # Sort + check contiguity
        entries = sorted(self.timeline, key=lambda e: e.start)
        for i in range(1, len(entries)):
            prev, curr = entries[i - 1], entries[i]
            if abs(curr.start - prev.end) > 0.5:  # 0.5s tolerance for rounding
                raise ValueError(
                    f"track {self.id}: gap between section {prev.section}"
                    f" ({prev.start}-{prev.end}s) and {curr.section} ({curr.start}-{curr.end}s)"
                )
        # Last section should reach near track end
        last = entries[-1]
        if abs(last.end - self.duration_seconds) > 5.0:
            raise ValueError(
                f"track {self.id}: timeline ends at {last.end}s but duration is "
                f"{self.duration_seconds}s — mismatch >5s"
            )
        return self

    def section_at(self, position: float) -> Optional[TimelineEntry]:
        """Return the TimelineEntry containing the given position."""
        for entry in self.timeline:
            if entry.start <= position < entry.end:
                return entry
        return None


class Scenario(BaseModel):
    """A transition-decision test case with ground-truth expected behavior."""

    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    category: str  # e.g. "positive_bass_swap", "negative_filter_sweep", "edge_identity"

    # Scene
    active_track: str  # Track.id reference
    active_position_s: float
    idle_track: str  # Track.id reference
    directive: Optional[str] = None
    pending: bool = False
    context_note: str = ""

    # Ground truth expectations
    expected_technique: Optional[Technique] = None  # None = expect "waiting" / no call
    allowed_alternatives: list[Technique] = Field(default_factory=list)
    rejected_techniques: list[Technique] = Field(default_factory=list)
    expect_wait: bool = False  # if True, DJ should NOT schedule anything

    expected_at_position_range: Optional[tuple] = None  # (min, max) in seconds
    expected_duration_range: Optional[tuple] = None  # (min_s, max_s)

    # Required: cite a rule or reasoning
    rationale: str

    @model_validator(mode="after")
    def _check_consistency(self) -> "Scenario":
        if self.expect_wait and self.expected_technique:
            raise ValueError(
                f"scenario {self.id}: cannot have both expect_wait=True and expected_technique"
            )
        if not self.expect_wait and not self.expected_technique:
            raise ValueError(
                f"scenario {self.id}: must set either expected_technique or expect_wait=True"
            )
        if self.expected_at_position_range:
            lo, hi = self.expected_at_position_range
            if hi <= lo:
                raise ValueError(f"scenario {self.id}: at_position range invalid ({lo}, {hi})")
        if self.expected_duration_range:
            lo, hi = self.expected_duration_range
            if hi <= lo:
                raise ValueError(f"scenario {self.id}: duration range invalid ({lo}, {hi})")
        if not self.rationale.strip():
            raise ValueError(f"scenario {self.id}: rationale is required (documents the DJ rule)")
        return self


# ── YAML loaders ──────────────────────────────────────────────────────

def load_tracks_yaml(path: Path) -> dict[str, Track]:
    """Load tracks.yaml → {track_id: Track}. Validates every entry."""
    import yaml
    raw = yaml.safe_load(path.read_text()) or {}
    tracks_list = raw.get("tracks", [])
    out: dict[str, Track] = {}
    for i, t_dict in enumerate(tracks_list):
        try:
            track = Track(**t_dict)
        except Exception as exc:
            raise ValueError(f"tracks.yaml entry #{i} ({t_dict.get('id', '?')}): {exc}") from exc
        if track.id in out:
            raise ValueError(f"tracks.yaml duplicate id: {track.id}")
        out[track.id] = track
    return out


def load_scenarios_yaml(path: Path, track_db: dict[str, Track]) -> dict[str, Scenario]:
    """Load transitions.yaml → {scenario_id: Scenario}. Cross-validates
    that referenced track IDs exist + active_position is within timeline."""
    import yaml
    raw = yaml.safe_load(path.read_text()) or {}
    items = raw.get("scenarios", [])
    out: dict[str, Scenario] = {}
    for i, s_dict in enumerate(items):
        try:
            sc = Scenario(**s_dict)
        except Exception as exc:
            raise ValueError(f"transitions.yaml entry #{i} ({s_dict.get('id', '?')}): {exc}") from exc
        if sc.id in out:
            raise ValueError(f"transitions.yaml duplicate id: {sc.id}")
        if sc.active_track not in track_db:
            raise ValueError(
                f"scenario {sc.id}: active_track {sc.active_track!r} not in track DB"
            )
        if sc.idle_track not in track_db:
            raise ValueError(
                f"scenario {sc.id}: idle_track {sc.idle_track!r} not in track DB"
            )
        active = track_db[sc.active_track]
        if sc.active_position_s >= active.duration_seconds:
            raise ValueError(
                f"scenario {sc.id}: active_position_s ({sc.active_position_s}) "
                f">= active track duration ({active.duration_seconds})"
            )
        out[sc.id] = sc
    return out
