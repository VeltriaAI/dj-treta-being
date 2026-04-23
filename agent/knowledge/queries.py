"""Typed query functions against the knowledge backend.

v9 implementation. Queries run against:
  - Polars LazyFrame over `~/Music/DJTreta/knowledge/dj_treta_library.parquet`
    (3.5M rows, metadata for filtering/ranking)
  - LanceDB table `tracks` (384-dim text-embedding-005 Matryoshka vectors,
    join key = mbid) for ANN similarity. Optional — metadata-only queries
    still succeed when vectors aren't built yet.

When the backend is unavailable, each query returns an empty list / None
and calls `KnowledgeClient.record_degraded(...)` so callers can see the
degradation explicitly. No silent empty returns.

Caller pattern:

    from .knowledge import queries as kb
    candidates = kb.discover_candidates(mood_profile, bpm_range=(115, 125))
    similar = kb.similar_to(CanonicalRef("Artbat", "Horizon"))
    prompt_hits = kb.similar_to_text("driving melodic techno at sunrise")
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .client import KnowledgeClient
from .models import (
    CanonicalRef,
    GapReport,
    GenreInfo,
    KnowledgeTrack,
)

log = logging.getLogger("dj-treta")

# Vertex AI embedding model — lazy singleton (initialised on first
# similar_to_text call). text-embedding-005 returns 768-dim vectors; we
# truncate to 384 (Matryoshka) to match the LanceDB index.
_EMBED_MODEL = None
_EMBED_DIMS = 384


def _client() -> KnowledgeClient:
    return KnowledgeClient.instance()


def _enabled() -> bool:
    """Resolve the enabled flag from config without a hard dep on runtime."""
    try:
        from ..config import load_config
        cfg = load_config()
        k = getattr(cfg, "knowledge", None)
        return bool(getattr(k, "enabled", False))
    except Exception:
        return False


def _ensure() -> bool:
    """Make sure the backend is loaded. Returns False if unavailable."""
    client = _client()
    enabled = _enabled()
    try:
        from ..config import load_config
        data_dir = getattr(load_config().knowledge, "data_dir", None)
    except Exception:
        data_dir = None
    return client.ensure_loaded(enabled=enabled, data_dir=data_dir)


# ── Internal helpers ──────────────────────────────────────────────────

def _row_to_track(row: dict, similarity: Optional[float] = None) -> KnowledgeTrack:
    """Map a polars row-dict to KnowledgeTrack."""
    artist = row.get("artist_name") or ""
    title = row.get("title") or ""
    tempo = row.get("tempo")
    bpm_hint = int(tempo) if tempo is not None else None

    subgenre = row.get("dvi_styles") or ""
    label = row.get("dvi_labels") or ""

    return KnowledgeTrack(
        canonical=CanonicalRef(artist=artist, song=title),
        subgenre=subgenre,
        primary_genre="",
        label=label,
        year=row.get("year"),
        bpm_hint=bpm_hint,
        search_query=f"{artist} {title}".strip(),
        mbid=row.get("mbid") or "",
        artist_name=artist,
        title=title,
        video_id=row.get("video_id") or "",
        youtube_music_url=row.get("youtube_music_url") or "",
        tempo=tempo,
        key=row.get("key"),
        danceability=row.get("danceability"),
        energy_feat=row.get("energy"),
        valence=row.get("valence"),
        similarity_score=similarity,
    )


def _build_mood_filter(mood_profile: Optional[dict]):
    """Return a polars expression filtering by mood_profile subgenres.

    None when no mood filter applies (caller should skip the filter).
    Uses dvi_styles exact-overlap OR yt_matched_album keyword match.
    """
    import polars as pl

    if not mood_profile:
        return None
    subs = mood_profile.get("discogs_subgenres") or []
    if not subs:
        return None

    subs_lc = [s.lower() for s in subs if isinstance(s, str) and s.strip()]
    if not subs_lc:
        return None

    # dvi_styles overlap (case-insensitive). dvi_styles is a delimited
    # string — use `contains` against lowercased field.
    dvi_lc = pl.col("dvi_styles").str.to_lowercase().fill_null("")
    album_lc = pl.col("yt_matched_album").str.to_lowercase().fill_null("")

    expr = None
    for s in subs_lc:
        # Escape regex specials (subgenres may contain '+', '.', etc.)
        import re
        pat = re.escape(s)
        clause = dvi_lc.str.contains(pat) | album_lc.str.contains(pat)
        expr = clause if expr is None else (expr | clause)
    return expr


def _build_exclude_filter(exclude_canonical: Optional[list]):
    """Return a polars expression that is True for rows to KEEP.

    exclude_canonical items may be CanonicalRef or (artist, song) tuples
    or dicts. We exclude by mbid if the item exposes one (not typical for
    CanonicalRef) and by (artist_name.lower(), title.lower()) tuple match.
    """
    import polars as pl

    if not exclude_canonical:
        return None

    excl_mbids = set()
    excl_pairs = set()
    for item in exclude_canonical:
        mbid = getattr(item, "mbid", None)
        if mbid:
            excl_mbids.add(mbid)
        if isinstance(item, CanonicalRef):
            excl_pairs.add((item.artist.lower(), item.song.lower()))
        elif isinstance(item, tuple) and len(item) >= 2:
            excl_pairs.add((str(item[0]).lower(), str(item[1]).lower()))
        elif isinstance(item, dict):
            a = item.get("artist") or item.get("artist_name") or ""
            s = item.get("song") or item.get("title") or ""
            if a and s:
                excl_pairs.add((a.lower(), s.lower()))

    keep_expr = None
    if excl_mbids:
        keep_expr = ~pl.col("mbid").is_in(list(excl_mbids))
    if excl_pairs:
        artist_lc = pl.col("artist_name").str.to_lowercase().fill_null("")
        title_lc = pl.col("title").str.to_lowercase().fill_null("")
        # Build a struct column of (artist_lc, title_lc) and exclude in-set.
        pair_list = [list(p) for p in excl_pairs]
        pair_excl = None
        for a, t in pair_list:
            clause = (artist_lc == a) & (title_lc == t)
            pair_excl = clause if pair_excl is None else (pair_excl | clause)
        pair_keep = ~pair_excl
        keep_expr = pair_keep if keep_expr is None else (keep_expr & pair_keep)
    return keep_expr


def _apply_discover_filters(
    lf,
    mood_profile: Optional[dict],
    bpm_range: Optional[tuple],
    exclude_canonical: Optional[list],
):
    """Shared filter chain for discover_candidates / gap_analysis."""
    import polars as pl

    # video_id must be non-null (playable)
    out = lf.filter(pl.col("video_id").is_not_null() & (pl.col("video_id") != ""))

    if bpm_range is not None:
        low, high = bpm_range
        out = out.filter(
            pl.col("tempo").is_null()
            | ((pl.col("tempo") >= low) & (pl.col("tempo") <= high))
        )

    mood_expr = _build_mood_filter(mood_profile)
    if mood_expr is not None:
        out = out.filter(mood_expr)

    excl_expr = _build_exclude_filter(exclude_canonical)
    if excl_expr is not None:
        out = out.filter(excl_expr)

    return out


# ── Queries ───────────────────────────────────────────────────────────

def discover_candidates(
    mood_profile: Optional[dict] = None,
    bpm_range: Optional[tuple] = None,
    exclude_canonical: Optional[list] = None,
    limit: int = 20,
) -> list:
    """Return KnowledgeTrack[] matching the mood — metadata-only, no ANN."""
    t0 = time.time()
    client = _client()
    if not _ensure():
        client.record_degraded("backend unavailable")
        return []

    import polars as pl

    try:
        lf = _apply_discover_filters(
            client.lf, mood_profile, bpm_range, exclude_canonical
        )

        # Artist bonus: +1 when artist_name appears in mood_profile.artist_hints
        artist_hints = []
        if mood_profile:
            artist_hints = [
                a.lower() for a in (mood_profile.get("artist_hints") or [])
                if isinstance(a, str) and a.strip()
            ]

        if artist_hints:
            artist_bonus = (
                pl.col("artist_name").str.to_lowercase().is_in(artist_hints)
                .cast(pl.Int32)
            )
        else:
            artist_bonus = pl.lit(0).cast(pl.Int32)

        ranked = (
            lf.with_columns([
                pl.col("tempo").is_not_null().alias("_has_tempo"),
                artist_bonus.alias("_artist_bonus"),
            ])
            .sort(
                by=["_artist_bonus", "_has_tempo", "year", "mbid"],
                descending=[True, True, True, False],
                nulls_last=True,
            )
            .limit(limit)
        )

        df = ranked.collect()
        results = [_row_to_track(r) for r in df.to_dicts()]
        client.record_query(int((time.time() - t0) * 1000))
        return results
    except Exception as exc:
        log.warning(f"discover_candidates failed: {exc}")
        client.record_degraded(f"discover_candidates error: {type(exc).__name__}")
        return []


def similar_to(
    seed: CanonicalRef,
    limit: int = 20,
    exclude_canonical: Optional[list] = None,
) -> list:
    """Return KnowledgeTrack[] similar to a seed canonical ref (RAG/ANN)."""
    t0 = time.time()
    client = _client()
    if not _ensure():
        client.record_degraded("backend unavailable")
        return []

    if not client.has_vectors():
        client.record_degraded("vectors not yet built")
        return []

    import polars as pl

    try:
        # 1. Resolve seed -> mbid via metadata lookup
        artist_lc = seed.artist.lower()
        song_lc = seed.song.lower()
        seed_row = (
            client.lf.filter(
                (pl.col("artist_name").str.to_lowercase() == artist_lc)
                & (pl.col("title").str.to_lowercase() == song_lc)
            )
            .select(["mbid"])
            .limit(1)
            .collect()
        )
        if seed_row.is_empty():
            client.record_degraded(f"seed not found: {seed.artist} - {seed.song}")
            return []
        mbid = seed_row["mbid"][0]
        if not mbid:
            client.record_degraded("seed has null mbid")
            return []

        # 2. Fetch seed vector from LanceDB
        tbl = client.vec_tbl
        seed_hits = tbl.search().where(f"mbid = '{mbid}'").limit(1).to_list()
        if not seed_hits:
            client.record_degraded(f"seed mbid {mbid[:8]} not in vector index")
            return []
        seed_vec = seed_hits[0]["vector"]

        # 3. ANN query — overfetch for dedup/exclude headroom
        ann_hits = (
            tbl.search(seed_vec)
            .metric("cosine")
            .limit(max(limit * 3, limit + 5))
            .to_list()
        )

        # 4. Build exclusion set (include seed itself)
        excl_mbids = {mbid}
        excl_pairs = set()
        for item in exclude_canonical or []:
            m = getattr(item, "mbid", None)
            if m:
                excl_mbids.add(m)
            if isinstance(item, CanonicalRef):
                excl_pairs.add((item.artist.lower(), item.song.lower()))

        # 5. Batch-join to metadata by mbid (single polars scan)
        hit_mbids = [h["mbid"] for h in ann_hits if h.get("mbid")]
        if not hit_mbids:
            client.record_query(int((time.time() - t0) * 1000))
            return []

        meta_df = (
            client.lf.filter(pl.col("mbid").is_in(hit_mbids))
            .collect()
        )
        meta_by_mbid = {r["mbid"]: r for r in meta_df.to_dicts()}

        results: list = []
        for hit in ann_hits:
            m = hit.get("mbid")
            if not m or m in excl_mbids:
                continue
            row = meta_by_mbid.get(m)
            if row is None:
                continue
            pair = (
                (row.get("artist_name") or "").lower(),
                (row.get("title") or "").lower(),
            )
            if pair in excl_pairs:
                continue
            dist = hit.get("_distance", 0.0) or 0.0
            sim = 1.0 - float(dist)
            results.append(_row_to_track(row, similarity=sim))
            if len(results) >= limit:
                break

        client.record_query(int((time.time() - t0) * 1000))
        return results
    except Exception as exc:
        log.warning(f"similar_to failed: {exc}")
        client.record_degraded(f"similar_to error: {type(exc).__name__}")
        return []


def _get_embed_model():
    """Lazy-init Vertex AI text-embedding-005 model. Returns None on failure."""
    global _EMBED_MODEL
    if _EMBED_MODEL is not None:
        return _EMBED_MODEL
    try:
        from vertexai.language_models import TextEmbeddingModel
        _EMBED_MODEL = TextEmbeddingModel.from_pretrained("text-embedding-005")
        return _EMBED_MODEL
    except Exception as exc:
        log.warning(f"Vertex embedding model init failed: {exc}")
        return None


def similar_to_text(prompt_text: str, limit: int = 20) -> list:
    """Embed a free-form text query, run ANN against the vector index."""
    t0 = time.time()
    client = _client()
    if not _ensure():
        client.record_degraded("backend unavailable")
        return []

    if not client.has_vectors():
        client.record_degraded("vectors not yet built")
        return []

    if not prompt_text or not prompt_text.strip():
        client.record_degraded("empty prompt_text")
        return []

    import polars as pl

    try:
        model = _get_embed_model()
        if model is None:
            client.record_degraded("embedding model unavailable")
            return []

        from vertexai.language_models import TextEmbeddingInput
        inputs = [TextEmbeddingInput(text=prompt_text, task_type="RETRIEVAL_QUERY")]
        embeddings = model.get_embeddings(inputs)
        if not embeddings:
            client.record_degraded("embedding returned empty")
            return []
        full_vec = list(embeddings[0].values)
        if len(full_vec) < _EMBED_DIMS:
            client.record_degraded(
                f"embedding dim {len(full_vec)} < {_EMBED_DIMS}"
            )
            return []
        query_vec = full_vec[:_EMBED_DIMS]

        tbl = client.vec_tbl
        ann_hits = (
            tbl.search(query_vec)
            .metric("cosine")
            .limit(max(limit * 3, limit + 5))
            .to_list()
        )

        hit_mbids = [h["mbid"] for h in ann_hits if h.get("mbid")]
        if not hit_mbids:
            client.record_query(int((time.time() - t0) * 1000))
            return []

        meta_df = (
            client.lf.filter(pl.col("mbid").is_in(hit_mbids))
            .collect()
        )
        meta_by_mbid = {r["mbid"]: r for r in meta_df.to_dicts()}

        seen_mbid: set = set()
        results: list = []
        for hit in ann_hits:
            m = hit.get("mbid")
            if not m or m in seen_mbid:
                continue
            seen_mbid.add(m)
            row = meta_by_mbid.get(m)
            if row is None:
                continue
            dist = hit.get("_distance", 0.0) or 0.0
            sim = 1.0 - float(dist)
            results.append(_row_to_track(row, similarity=sim))
            if len(results) >= limit:
                break

        client.record_query(int((time.time() - t0) * 1000))
        return results
    except Exception as exc:
        log.warning(f"similar_to_text failed: {exc}")
        client.record_degraded(f"similar_to_text error: {type(exc).__name__}")
        return []


def genre_context(genre_slug: str) -> Optional[GenreInfo]:
    """Return GenreInfo aggregated from the dataset for a genre slug."""
    t0 = time.time()
    client = _client()
    if not _ensure():
        client.record_degraded("backend unavailable")
        return None

    if not genre_slug or not genre_slug.strip():
        return None

    import polars as pl
    import re

    try:
        slug_lc = genre_slug.lower().strip()
        pat = re.escape(slug_lc)
        dvi_lc = pl.col("dvi_styles").str.to_lowercase().fill_null("")
        album_lc = pl.col("yt_matched_album").str.to_lowercase().fill_null("")
        match_expr = dvi_lc.str.contains(pat) | album_lc.str.contains(pat)

        matched = client.lf.filter(match_expr)

        # Single aggregate pass: count + tempo percentiles
        agg = matched.select([
            pl.len().alias("n"),
            pl.col("tempo").quantile(0.1).alias("bpm_low"),
            pl.col("tempo").quantile(0.9).alias("bpm_high"),
        ]).collect()

        n = int(agg["n"][0])
        if n == 0:
            return None

        bpm_low = agg["bpm_low"][0]
        bpm_high = agg["bpm_high"][0]
        bpm_low_i = int(bpm_low) if bpm_low is not None else None
        bpm_high_i = int(bpm_high) if bpm_high is not None else None

        # Sample 5 distinct artists
        artists_df = (
            matched.select(pl.col("artist_name").drop_nulls().unique())
            .limit(5)
            .collect()
        )
        sampled_artists = [a for a in artists_df["artist_name"].to_list() if a]
        artists_str = ", ".join(sampled_artists) if sampled_artists else "various"
        description = f"{n:,} tracks in dataset; artists include {artists_str}"

        client.record_query(int((time.time() - t0) * 1000))
        return GenreInfo(
            name=genre_slug,
            bpm_low=bpm_low_i,
            bpm_high=bpm_high_i,
            description=description,
        )
    except Exception as exc:
        log.warning(f"genre_context failed: {exc}")
        client.record_degraded(f"genre_context error: {type(exc).__name__}")
        return None


def gap_analysis(
    mood_profile: dict,
    local_canonical_refs: list,
) -> GapReport:
    """Return a GapReport describing library coverage for the mood.

    Always returns a GapReport (never None) so callers can see local_count
    + saturation even when the dataset backend is offline.
    """
    t0 = time.time()
    client = _client()
    local_count = len(local_canonical_refs or [])
    slug = (mood_profile or {}).get("canonical_slug", "")

    if not _ensure():
        client.record_degraded("backend unavailable")
        return GapReport(
            mood_slug=slug,
            local_count=local_count,
            dataset_count=0,
            saturation=1.0,
        )

    import polars as pl

    try:
        lf = _apply_discover_filters(
            client.lf, mood_profile, bpm_range=None, exclude_canonical=None
        )

        # Count + top 10 artists in a single collect
        counts = (
            lf.group_by("artist_name")
            .agg(pl.len().alias("n"))
            .sort(by=["n", "artist_name"], descending=[True, False])
            .limit(10)
            .collect()
        )
        top_artists = [
            a for a in counts["artist_name"].to_list() if a
        ]

        dataset_count = int(lf.select(pl.len()).collect().item())

        local_artists_lc = set()
        for ref in local_canonical_refs or []:
            if isinstance(ref, CanonicalRef):
                local_artists_lc.add(ref.artist.lower())
            elif isinstance(ref, tuple) and ref:
                local_artists_lc.add(str(ref[0]).lower())
            elif isinstance(ref, dict):
                a = ref.get("artist") or ref.get("artist_name") or ""
                if a:
                    local_artists_lc.add(a.lower())

        missing_artists = [
            a for a in top_artists if a.lower() not in local_artists_lc
        ]

        saturation = min(1.0, local_count / max(dataset_count, 1))

        client.record_query(int((time.time() - t0) * 1000))
        return GapReport(
            mood_slug=slug,
            local_count=local_count,
            dataset_count=dataset_count,
            missing_artists=missing_artists,
            saturation=saturation,
        )
    except Exception as exc:
        log.warning(f"gap_analysis failed: {exc}")
        client.record_degraded(f"gap_analysis error: {type(exc).__name__}")
        return GapReport(
            mood_slug=slug,
            local_count=local_count,
            dataset_count=0,
            saturation=1.0,
        )
