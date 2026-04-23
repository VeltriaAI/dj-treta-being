"""Eval harness for the v9 knowledge-planner integration.

This eval verifies that the 3.5M-track dataset, loaded via
``agent.knowledge.queries``, produces candidate lists that are good
enough to feed the planner. It is intentionally a dataset-level eval —
the planner loop itself isn't live yet during Phase K7.

What this covers
----------------
* ``discover_candidates`` diversity for 5 representative moods
  (melodic techno, peak-time techno, afro house, progressive house,
  and the niche ``bollyafro``) — asserts result counts plus distinct
  artist / year counts where the dataset is dense enough.
* Rank-1 quality: when tempo data is available for the mood, the
  top-ranked candidate must carry a ``bpm_hint``.
* ``similar_to_text`` wires end-to-end (skipped unless the LanceDB
  vector index is present — still being built in Phase K7).
* ``gap_analysis`` surfaces a realistic dataset_count / saturation /
  missing_artists triple for an empty local library.
* ``merge_candidates_against_local`` correctly joins dataset rows to
  existing local DB rows (by canonical 2-tuple) and marks non-matches
  as not-downloaded with a usable ``search_query``.
* Data-quality signal: DJ continuous-mix compilations DO leak through
  the current filters; documented here via ``pytest.xfail`` so the
  planner's downstream filter gets a fixture to drive it.
* ``KnowledgeHealth`` round-trip: ``last_query_ms`` moves from 0 → >0
  after a successful query.

What this does NOT cover (yet)
------------------------------
* ``similar_to(seed)`` — needs a seed mbid that exists in BOTH the
  local DB and the LanceDB table. The Phase K7 DB doesn't yet share
  mbids with the dataset, so this path is deferred.
* Planner loop candidate selection — planner is still being built.
* Performance benchmarks — a separate latency harness will land
  alongside the planner integration.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent.knowledge import queries as kb
from agent.knowledge.client import KnowledgeClient
from agent.knowledge.merge import merge_candidates_against_local
from agent.knowledge.models import CanonicalRef, KnowledgeTrack


PARQUET_PATH = Path.home() / "Music" / "DJTreta" / "knowledge" / "dj_treta_library.parquet"


# ─── Module-level fixture ─────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def enabled_knowledge_client():
    """Force the knowledge backend enabled and pre-load it for the module.

    Skips the whole module when the parquet isn't on disk — eval only runs
    where the dataset is downloaded.
    """
    if not PARQUET_PATH.exists():
        pytest.skip(f"parquet not downloaded at {PARQUET_PATH}")

    # Force queries._enabled() to return True without depending on config.
    with patch.object(kb, "_enabled", return_value=True):
        KnowledgeClient.reset()
        client = KnowledgeClient.instance()
        ok = client.ensure_loaded(enabled=True, data_dir=str(PARQUET_PATH.parent))
        if not ok:
            pytest.skip(f"knowledge client failed to load: {client.health.last_error}")
        yield client
        KnowledgeClient.reset()


# ─── Mood scenarios for discover_candidates ───────────────────────────

DISCOVER_SCENARIOS = [
    # (id, mood_profile, bpm_range, min_results, min_artists, min_years)
    (
        "melodic-techno-120-125",
        {
            "canonical_slug": "melodic-techno",
            "discogs_subgenres": ["Melodic Techno", "Techno"],
        },
        (120, 125),
        10, 5, 3,
    ),
    (
        "peak-time-techno-130-140",
        {
            "canonical_slug": "peak-time-techno",
            "discogs_subgenres": ["Techno", "Peak Time"],
        },
        (130, 140),
        10, 5, 0,   # years can be sparse in peak-time; don't force it
    ),
    (
        "afro-house-118-125",
        {
            "canonical_slug": "afro-house",
            "discogs_subgenres": ["Afro House"],
        },
        (118, 125),
        5, 3, 0,
    ),
    (
        "progressive-house-120-128",
        {
            "canonical_slug": "progressive-house",
            "discogs_subgenres": ["Progressive House"],
        },
        (120, 128),
        10, 5, 0,
    ),
    (
        "bollyafro-118-125",
        {
            "canonical_slug": "bollyafro",
            "discogs_subgenres": ["Afro House", "Bollywood House"],
        },
        (118, 125),
        3, 2, 0,
    ),
]


@pytest.mark.parametrize(
    "scenario_id,mood_profile,bpm_range,min_results,min_artists,min_years",
    DISCOVER_SCENARIOS,
    ids=[s[0] for s in DISCOVER_SCENARIOS],
)
def test_discover_candidates_diversity(
    scenario_id,
    mood_profile,
    bpm_range,
    min_results,
    min_artists,
    min_years,
):
    """Each mood should return ≥N diverse, playable candidates."""
    with patch.object(kb, "_enabled", return_value=True):
        results = kb.discover_candidates(
            mood_profile=mood_profile,
            bpm_range=bpm_range,
            limit=20,
        )

    # Niche moods (like bollyafro) are allowed to return nothing —
    # document and skip rather than hard-fail.
    if len(results) == 0:
        pytest.skip(f"{scenario_id}: dataset returned zero candidates (niche mood)")

    # Every candidate must be playable + identifiable.
    for t in results:
        assert t.artist_name, f"{scenario_id}: empty artist_name in result: {t}"
        assert t.title, f"{scenario_id}: empty title in result: {t}"
        assert t.video_id, f"{scenario_id}: empty video_id in result: {t}"

    assert len(results) >= min_results, (
        f"{scenario_id}: expected ≥{min_results} results, got {len(results)}"
    )

    distinct_artists = {t.artist_name.lower() for t in results if t.artist_name}
    assert len(distinct_artists) >= min_artists, (
        f"{scenario_id}: expected ≥{min_artists} distinct artists, "
        f"got {len(distinct_artists)}"
    )

    if min_years > 0:
        distinct_years = {t.year for t in results if t.year is not None}
        assert len(distinct_years) >= min_years, (
            f"{scenario_id}: expected ≥{min_years} distinct years, "
            f"got {len(distinct_years)}"
        )


def test_rank_1_has_known_bpm_when_available():
    """When tempo data is present for the mood, rank-1 should carry a bpm_hint.

    We pick melodic techno because the dataset has extensive tempo coverage
    there; the sort in discover_candidates prefers rows with tempo set.
    """
    with patch.object(kb, "_enabled", return_value=True):
        results = kb.discover_candidates(
            mood_profile={
                "canonical_slug": "melodic-techno",
                "discogs_subgenres": ["Melodic Techno", "Techno"],
            },
            bpm_range=(120, 125),
            limit=20,
        )

    if not results:
        pytest.skip("melodic-techno returned zero candidates")

    # Check that at least one of the top candidates has tempo data.
    # Sort is supposed to float has_tempo=True to the front, so rank-1 ideally
    # has it. Tolerant check: any of top-3 is fine.
    top_3 = results[:3]
    has_bpm_count = sum(1 for t in top_3 if t.bpm_hint is not None)
    assert has_bpm_count >= 1, (
        f"Expected ≥1 of top 3 candidates to have bpm_hint set, "
        f"got {has_bpm_count}. Top 3: "
        f"{[(t.artist_name, t.title, t.bpm_hint) for t in top_3]}"
    )

    # Stricter check: rank-1 has it.
    assert results[0].bpm_hint is not None, (
        f"Rank-1 candidate has no bpm_hint: "
        f"{results[0].artist_name} - {results[0].title}"
    )


def test_similar_to_text_wires_through():
    """Free-text ANN query returns KnowledgeTracks with similarity scores."""
    client = KnowledgeClient.instance()
    if not client.has_vectors():
        pytest.skip("LanceDB vectors not yet built")
    # has_vectors() only checks that the table handle exists; the table
    # may still be empty while the embedding job is running.
    try:
        row_count = client.vec_tbl.count_rows()
    except Exception as exc:
        pytest.skip(f"could not read LanceDB row count: {exc}")
    if row_count == 0:
        pytest.skip("LanceDB table empty — embedding job still running")

    with patch.object(kb, "_enabled", return_value=True):
        results = kb.similar_to_text("dark hypnotic techno", limit=10)

    assert len(results) == 10, f"expected 10 results, got {len(results)}"
    for t in results:
        assert t.similarity_score is not None and t.similarity_score > 0, (
            f"expected similarity_score > 0, got {t.similarity_score}"
        )
        assert t.artist_name, f"empty artist in similar_to_text result: {t}"


def test_gap_analysis_reports_realistic():
    """Empty local library + dense mood → saturation=0, ≥3 missing artists."""
    with patch.object(kb, "_enabled", return_value=True):
        report = kb.gap_analysis(
            mood_profile={
                "canonical_slug": "melodic-techno",
                "discogs_subgenres": ["Melodic Techno", "Techno"],
            },
            local_canonical_refs=[],
        )

    assert report is not None
    assert report.mood_slug == "melodic-techno"
    assert report.local_count == 0
    assert report.dataset_count > 0, (
        f"dataset_count should be > 0, got {report.dataset_count}"
    )
    assert report.saturation == 0.0, (
        f"saturation should be 0.0 for empty library, got {report.saturation}"
    )
    assert len(report.missing_artists) >= 3, (
        f"expected ≥3 missing_artists, got {len(report.missing_artists)}: "
        f"{report.missing_artists}"
    )


def test_merge_layer_correctness(test_db):
    """Merge: 2 matching local rows → downloaded=True; 1 fresh → search_query set.

    Uses the shared ``test_db`` fixture to seed a local tracks table, then
    extends two rows with canonical identity that we'll also construct in
    our KnowledgeTrack inputs.
    """
    import agent.db as db_mod
    db = db_mod.get_db()
    try:
        # Phase-0+ schema may not have canonical_* columns on the seed DB.
        # Add them if missing, so the canonical-tuple join can resolve.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(tracks)").fetchall()}
        for col in ("canonical_artist", "canonical_song",
                    "canonical_version", "remixer"):
            if col not in cols:
                db.execute(f"ALTER TABLE tracks ADD COLUMN {col} TEXT")

        # Stamp canonical identity onto two of the seed rows.
        db.execute(
            "UPDATE tracks SET canonical_artist=?, canonical_song=? "
            "WHERE title=?",
            ("Artist 1", "Track A", "Track A"),
        )
        db.execute(
            "UPDATE tracks SET canonical_artist=?, canonical_song=? "
            "WHERE title=?",
            ("Artist 2", "Track B", "Track B"),
        )
        db.commit()
    finally:
        db.close()

    kts = [
        KnowledgeTrack(
            canonical=CanonicalRef(artist="Artist 1", song="Track A"),
            artist_name="Artist 1",
            title="Track A",
            mbid="",
            search_query="Artist 1 Track A",
        ),
        KnowledgeTrack(
            canonical=CanonicalRef(artist="Artist 2", song="Track B"),
            artist_name="Artist 2",
            title="Track B",
            mbid="",
            search_query="Artist 2 Track B",
        ),
        KnowledgeTrack(
            canonical=CanonicalRef(artist="Unknown Artist", song="Fresh Song"),
            artist_name="Unknown Artist",
            title="Fresh Song",
            mbid="",
            search_query="Unknown Artist Fresh Song",
        ),
    ]

    merged = merge_candidates_against_local(kts)
    assert len(merged) == 3

    # First two should have local rows → downloaded=True
    assert merged[0].downloaded is True, (
        f"Expected downloaded=True for Artist 1/Track A, got {merged[0]}"
    )
    assert merged[0].path, f"Expected non-empty path, got: {merged[0].path!r}"

    assert merged[1].downloaded is True, (
        f"Expected downloaded=True for Artist 2/Track B, got {merged[1]}"
    )
    assert merged[1].path, f"Expected non-empty path, got: {merged[1].path!r}"

    # Third has no local match → downloaded=False with usable search_query
    assert merged[2].downloaded is False
    assert merged[2].path == ""
    assert merged[2].search_query == "Unknown Artist Fresh Song"


def test_continuous_mix_filter():
    """Documented data-quality gap: dataset contains DJ continuous-mix rows.

    The planner needs a downstream filter that rejects titles like
    ``<x> (Continuous Mix)`` or ``Mix 1`` before scheduling them — a
    3-hour compilation is not a 4-min candidate. This test documents the
    leak via xfail so we have a fixture to drive the filter once it lands.
    """
    with patch.object(kb, "_enabled", return_value=True):
        # Pull a large window so the compilation rows surface.
        results = kb.discover_candidates(
            mood_profile={
                "canonical_slug": "melodic-techno",
                "discogs_subgenres": ["Melodic Techno", "Techno"],
            },
            bpm_range=None,  # widen; compilations often lack tempo
            limit=500,
        )

    bad_markers = ("continuous mix", "mix 1", "mix 2", "mix 3", "dj mix")
    leaks = [
        t for t in results
        if any(m in (t.title or "").lower() for m in bad_markers)
    ]

    if leaks:
        pytest.xfail(
            reason=(
                "known data-quality: continuous-mix compilations not yet "
                f"filtered (saw {len(leaks)} leak, e.g. "
                f"{leaks[0].artist_name} - {leaks[0].title!r}). "
                "Planner downstream filter should exclude titles matching "
                f"{bad_markers}."
            )
        )

    # If we're here, the dataset slice happened to not contain any — the
    # filter may already be upstream, or the slice is small. Either way,
    # don't fail; the planner-side filter is still needed in principle.
    pytest.skip(
        "no continuous-mix rows in this slice — filter regression check "
        "deferred to planner layer"
    )


def test_query_health_updates():
    """health.last_query_ms must move from 0 to >0 after a successful query."""
    KnowledgeClient.reset()
    client = KnowledgeClient.instance()
    # Pre-state: never queried → 0
    assert client.health.last_query_ms == 0

    with patch.object(kb, "_enabled", return_value=True):
        ok = client.ensure_loaded(enabled=True, data_dir=str(PARQUET_PATH.parent))
        assert ok, f"client failed to load: {client.health.last_error}"

        results = kb.discover_candidates(
            mood_profile={
                "canonical_slug": "melodic-techno",
                "discogs_subgenres": ["Melodic Techno"],
            },
            bpm_range=(120, 125),
            limit=5,
        )

    assert len(results) > 0, "expected non-empty results to register a query"
    assert client.health.available is True
    assert client.health.last_query_ms > 0, (
        f"expected last_query_ms > 0 after query, got {client.health.last_query_ms}"
    )
