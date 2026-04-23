"""Merge layer between local DB tracks and external knowledge tracks.

Join keys (in priority order):
  1. MusicBrainz id (`mbid`) — exact, when both sides have it.
  2. Canonical 2-tuple (LOWER(canonical_artist), LOWER(canonical_song)) —
     version/remixer are too sparse to require for match; we widen the net
     on lookup and let the caller/planner disambiguate later.

Produces `MergedCandidate` objects that carry both the dataset metadata
(mbid, video_id, youtube_music_url, subgenre, label, year, similarity)
and — when present — the local analysis numbers (bpm, key_camelot,
energy_peak, path). `downloaded` is True iff a local row with a path was
found.

v9 Phase K3. Never raises on DB issues — degraded merge (treat as "no
local matches") keeps the planner running; we log.warning instead.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from .models import CanonicalRef, KnowledgeTrack, MergedCandidate

log = logging.getLogger("dj-treta")


# ── Row → candidate ──────────────────────────────────────────────────

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


def _canonical_from_local(local_row: dict, fallback: CanonicalRef) -> CanonicalRef:
    """Build a CanonicalRef from a local tracks row, falling back to the
    dataset canonical whenever the local column is blank/NULL."""
    artist = local_row.get("canonical_artist") or fallback.artist
    song = local_row.get("canonical_song") or fallback.song
    version = local_row.get("canonical_version") or fallback.version
    remixer = local_row.get("remixer") or fallback.remixer
    return CanonicalRef(
        artist=artist,
        song=song,
        version=version or None,
        remixer=remixer or None,
    )


def merge_candidate(
    kt: KnowledgeTrack,
    local_row: Optional[dict] = None,
) -> MergedCandidate:
    """Combine a KnowledgeTrack with its local tracks row (if any).

    When local_row is None: pure dataset candidate (downloaded=False).
    When local_row is present: local numeric analysis wins (we measured
    it); dataset wins for editorial metadata (year, label, subgenre).
    """
    canonical_dataset = kt.canonical
    search_query = kt.search_query or f"{canonical_dataset.artist} {kt.title or canonical_dataset.song}".strip()

    if local_row is None:
        title = kt.title or f"{canonical_dataset.artist} - {canonical_dataset.song}".strip(" -")
        return MergedCandidate(
            canonical=canonical_dataset,
            title=title,
            bpm=float(kt.tempo) if kt.tempo is not None else (float(kt.bpm_hint) if kt.bpm_hint is not None else None),
            key_camelot="",
            energy=None,
            genre=kt.primary_genre,
            subgenre=kt.subgenre,
            label=kt.label,
            year=kt.year,
            downloaded=False,
            path="",
            search_query=search_query,
            mbid=kt.mbid,
            video_id=kt.video_id,
            youtube_music_url=kt.youtube_music_url,
            similarity_score=kt.similarity_score,
        )

    # ── Matched local row ────────────────────────────────────────────
    canonical = _canonical_from_local(local_row, canonical_dataset)

    # Numeric fields: local first (we measured), then dataset hint.
    local_bpm = local_row.get("bpm")
    if local_bpm is not None:
        bpm: Optional[float] = float(local_bpm)
    elif kt.tempo is not None:
        bpm = float(kt.tempo)
    elif kt.bpm_hint is not None:
        bpm = float(kt.bpm_hint)
    else:
        bpm = None

    key_camelot = str(local_row.get("key_camelot") or "")

    local_energy = local_row.get("energy_peak")
    energy: Optional[int] = int(local_energy) if local_energy is not None else None

    path = str(local_row.get("path") or "")
    # downloaded requires a real path — fall back to False if local row
    # exists but path is empty (shouldn't happen; path is NOT NULL in schema).
    downloaded = bool(path)

    title = str(local_row.get("title") or kt.title or f"{canonical.artist} - {canonical.song}".strip(" -"))
    genre = str(local_row.get("genre") or kt.primary_genre or "")

    return MergedCandidate(
        canonical=canonical,
        title=title,
        bpm=bpm,
        key_camelot=key_camelot,
        energy=energy,
        genre=genre,
        subgenre=kt.subgenre,
        label=kt.label,
        year=kt.year,
        downloaded=downloaded,
        path=path,
        search_query=search_query,
        mbid=kt.mbid,
        video_id=kt.video_id,
        youtube_music_url=kt.youtube_music_url,
        similarity_score=kt.similarity_score,
    )


# ── Pure merge (no DB) ───────────────────────────────────────────────

def merge_candidates(
    kts: list[KnowledgeTrack],
    local_rows_by_mbid: dict[str, dict],
    local_rows_by_canon: dict[tuple, dict],
) -> list[MergedCandidate]:
    """Merge a batch of KnowledgeTracks against pre-built lookup dicts.

    Deterministic, no DB access — used by tests and by
    `merge_candidates_against_local` once it has fetched its maps.
    Resolution order for each kt: mbid match → canonical (artist, song)
    lowercased → None.
    """
    out: list[MergedCandidate] = []
    for kt in kts:
        local_row: Optional[dict] = None
        if kt.mbid and kt.mbid in local_rows_by_mbid:
            local_row = local_rows_by_mbid[kt.mbid]
        else:
            canon_key = (kt.canonical.artist.lower(), kt.canonical.song.lower())
            local_row = local_rows_by_canon.get(canon_key)
        out.append(merge_candidate(kt, local_row))
    return out


# ── DB-backed lookups ────────────────────────────────────────────────

def _tracks_has_column(db: sqlite3.Connection, col: str) -> bool:
    cols = {row["name"] for row in db.execute("PRAGMA table_info(tracks)").fetchall()}
    return col in cols


def find_local_by_mbid(mbids: list[str]) -> dict[str, dict]:
    """Look up local tracks by MusicBrainz id.

    Returns {mbid: row_dict}. Returns {} (with a single warn log) when
    the local schema doesn't carry `mbid` yet — that migration lands
    separately. Never raises.
    """
    mbids = [m for m in mbids if m]
    if not mbids:
        return {}
    from .. import db as db_mod  # late import — avoid cycles at module load
    try:
        db = db_mod.get_db()
    except Exception as exc:
        log.warning("find_local_by_mbid: get_db failed: %s", exc)
        return {}
    try:
        if not _tracks_has_column(db, "mbid"):
            log.warning("find_local_by_mbid: tracks.mbid column missing — skipping mbid join")
            return {}
        placeholders = ",".join("?" * len(mbids))
        rows = db.execute(
            f"SELECT * FROM tracks WHERE mbid IN ({placeholders})",
            mbids,
        ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            d = dict(row)
            mb = d.get("mbid")
            if mb and mb not in out:
                out[mb] = d
        return out
    except Exception as exc:
        log.warning("find_local_by_mbid: query failed: %s", exc)
        return {}
    finally:
        try:
            db.close()
        except Exception:
            pass


def find_local_matches(canonical_refs: list[CanonicalRef]) -> dict[tuple, dict]:
    """Look up local tracks by canonical (artist, song) lowercased.

    Version/remixer intentionally ignored — they're sparse and force
    false-misses. One row per (artist_lower, song_lower); first wins if
    duplicates. Rows with NULL canonical_artist are skipped.
    Returns {} on any exception (with warn log). Never raises.
    """
    if not canonical_refs:
        return {}
    # Dedup input by the same key we'll key the result on.
    seen: set[tuple] = set()
    uniq: list[CanonicalRef] = []
    for ref in canonical_refs:
        if not ref.artist or not ref.song:
            continue
        k = (ref.artist.lower(), ref.song.lower())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(ref)
    if not uniq:
        return {}

    from .. import db as db_mod
    try:
        db = db_mod.get_db()
    except Exception as exc:
        log.warning("find_local_matches: get_db failed: %s", exc)
        return {}
    try:
        out: dict[tuple, dict] = {}
        for ref in uniq:
            try:
                # Dataset titles include remix/version suffix in one string
                # (e.g. "Wild Sensations - Crowdpleaser Hothaus Remix") while
                # the local canonicalizer splits into (song, version). Accept
                # either an exact match OR a prefix match where local's
                # canonical_song is a prefix of the dataset song.
                row = db.execute(
                    "SELECT * FROM tracks "
                    "WHERE canonical_artist IS NOT NULL "
                    "  AND LOWER(canonical_artist) = ? "
                    "  AND ("
                    "    LOWER(canonical_song) = ? "
                    "    OR ? LIKE LOWER(canonical_song) || ' %'"
                    "    OR ? LIKE LOWER(canonical_song) || '(%'"
                    "  )"
                    "LIMIT 1",
                    (
                        ref.artist.lower(),
                        ref.song.lower(),
                        ref.song.lower(),
                        ref.song.lower(),
                    ),
                ).fetchone()
                if row is not None:
                    out[(ref.artist.lower(), ref.song.lower())] = dict(row)
            except Exception as exc:
                log.warning("find_local_matches: query for %s/%s failed: %s",
                            ref.artist, ref.song, exc)
                continue
        return out
    finally:
        try:
            db.close()
        except Exception:
            pass


def merge_candidates_against_local(kts: list[KnowledgeTrack]) -> list[MergedCandidate]:
    """Merge KnowledgeTracks against the local DB. Convenience wrapper.

    Fetches mbid map first, then falls back to canonical-tuple map for
    the still-unmatched tracks. Preserves input order.
    """
    if not kts:
        return []

    mbids = [kt.mbid for kt in kts if kt.mbid]
    by_mbid = find_local_by_mbid(mbids) if mbids else {}

    # Only query canonical for tracks that didn't hit on mbid.
    unmatched_refs = [
        kt.canonical for kt in kts
        if not (kt.mbid and kt.mbid in by_mbid)
    ]
    by_canon = find_local_matches(unmatched_refs)

    return merge_candidates(kts, by_mbid, by_canon)
