"""Merge layer between local DB tracks and external knowledge tracks.

Join key: canonical 4-tuple (artist, song, version, remixer) — case
insensitive, NULL-aware. Merges local analysis (BPM/key/energy from
librosa) with dataset enrichment (subgenre, label, year).

Phase 3.7 of v8 (deferred to v9): planner feeds knowledge candidates
through here before writing session.playlist, so the DJ sees both
locally-playable and need-to-download options uniformly.
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import CanonicalRef, KnowledgeTrack, MergedCandidate

log = logging.getLogger("dj-treta")


def merge_candidate(
    kb_track: KnowledgeTrack,
    local_row: Optional[dict] = None,
) -> MergedCandidate:
    """Combine a knowledge-dataset track with its local-DB row (if any).

    When local_row is present (tracks table), prefer local analysis for
    numeric fields (we actually measured those). Take label/subgenre/year
    from KB (local almost never has them).
    """
    canonical = kb_track.canonical

    downloaded = local_row is not None and bool(local_row.get("path"))
    path = str(local_row.get("path", "")) if local_row else ""

    def _pick(local_key: str, kb_val):
        if local_row and local_row.get(local_key) is not None:
            return local_row[local_key]
        return kb_val

    # Title: prefer canonical form ("Artist - Song") over whatever the
    # local filename stem was.
    title = f"{canonical.artist} - {canonical.song}".strip(" -")
    if canonical.remixer:
        title += f" ({canonical.remixer} Remix)"

    return MergedCandidate(
        canonical=canonical,
        title=title,
        bpm=_pick("bpm", kb_track.bpm_hint),
        key_camelot=str(local_row.get("key_camelot", "")) if local_row else "",
        energy=_pick("energy_peak", None),
        genre=str(local_row.get("genre", "")) if local_row else kb_track.primary_genre,
        subgenre=kb_track.subgenre,
        label=kb_track.label,
        year=kb_track.year,
        downloaded=downloaded,
        path=path,
        search_query=kb_track.search_query or f"{canonical.artist} - {canonical.song}",
    )


def local_row_to_canonical(row: dict) -> Optional[CanonicalRef]:
    """Build a CanonicalRef from a row in the local tracks table.

    Returns None if the row lacks canonical identity (pre-Phase-0 legacy
    rows where canonical columns are NULL).
    """
    artist = row.get("canonical_artist")
    song = row.get("canonical_song")
    if not artist or not song:
        return None
    return CanonicalRef(
        artist=artist,
        song=song,
        version=row.get("canonical_version") or None,
        remixer=row.get("remixer") or None,
    )
