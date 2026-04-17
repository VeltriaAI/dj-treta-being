"""Typed contracts for the knowledge package.

v8 Phase 3.5 establishes the typed surface. When
`config.knowledge.enabled=False` (v8 default), query functions return
empty lists/None of these types — never markdown strings, never silent
empty-return. v9 will wire these to the 18M-track dataset.

All models are plain dataclasses (no pydantic dep) for portability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class CanonicalRef:
    """Canonical track identity tuple — the join key between local DB and
    any external dataset. Shape mirrors the columns in the `tracks` table
    (canonical_artist, canonical_song, canonical_version, remixer)."""

    artist: str
    song: str
    version: Optional[str] = None
    remixer: Optional[str] = None

    def key(self) -> tuple:
        """Lower-cased tuple for hash/compare. NULLs kept distinct from empty strings."""
        return (
            self.artist.lower(),
            self.song.lower(),
            self.version.lower() if self.version else None,
            self.remixer.lower() if self.remixer else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class KnowledgeTrack:
    """A track known to the external dataset (not necessarily downloaded)."""

    canonical: CanonicalRef
    subgenre: str = ""
    primary_genre: str = ""
    label: str = ""
    year: Optional[int] = None
    bpm_hint: Optional[int] = None
    search_query: str = ""  # recommended YouTube query


@dataclass
class GenreInfo:
    """Dataset metadata about a genre/subgenre."""

    name: str
    bpm_low: Optional[int] = None
    bpm_high: Optional[int] = None
    description: str = ""
    parent: Optional[str] = None
    related: list = field(default_factory=list)


@dataclass
class GapReport:
    """Result of gap_analysis — how well the local library covers a mood."""

    mood_slug: str
    local_count: int
    dataset_count: int
    missing_artists: list = field(default_factory=list)
    missing_labels: list = field(default_factory=list)
    saturation: float = 0.0  # 0.0 = nothing in library, 1.0 = fully saturated


@dataclass
class MergedCandidate:
    """A playlist candidate merged from dataset knowledge + local library.

    `downloaded=True` means the local DB has a row with this canonical
    identity and `path` is set. `downloaded=False` means the planner wants
    this track but the library manager needs to fetch it first.
    """

    canonical: CanonicalRef
    title: str
    bpm: Optional[float] = None
    key_camelot: str = ""
    energy: Optional[int] = None
    genre: str = ""
    subgenre: str = ""
    label: str = ""
    year: Optional[int] = None
    downloaded: bool = False
    path: str = ""             # only meaningful when downloaded=True
    search_query: str = ""     # for library manager when not downloaded


@dataclass
class KnowledgeHealth:
    """Surfaced on session.knowledge_health so TUI/Being can see degradation."""

    available: bool = False
    last_error: str = ""
    last_query_ms: int = 0
    checked_at: float = 0.0

    @classmethod
    def offline(cls, reason: str = "disabled") -> "KnowledgeHealth":
        return cls(available=False, last_error=reason, checked_at=time.time())
