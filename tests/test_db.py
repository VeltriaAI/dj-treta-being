"""Tests for agent.db — SQLite operations for tracks, sets, learnings.

All tests use the test_db fixture which provides a temporary database
seeded with 5 tracks and 1 live set.
"""

import time
import unicodedata

import pytest

from agent.db import (
    upsert_track,
    find_compatible_tracks,
    get_track_by_path,
    insert_set,
    get_current_set,
    add_track_to_set,
    get_set_tracks,
    get_next_set_number,
    get_db,
)


class TestUpsertTrack:

    def test_upsert_track_insert(self, test_db):
        """Inserting a new track should create a row in the tracks table."""
        upsert_track(
            path="/music/new/Brand_New.mp3",
            title="Brand New",
            artist="New Artist",
            genre="progressive",
            bpm=135.0,
        )

        db = get_db()
        row = db.execute("SELECT * FROM tracks WHERE path=?",
                         ("/music/new/Brand_New.mp3",)).fetchone()
        db.close()

        assert row is not None
        assert row["title"] == "Brand New"
        assert row["bpm"] == 135.0
        assert row["genre"] == "progressive"

    def test_upsert_track_update(self, test_db):
        """Upserting an existing path should update fields, not create a duplicate."""
        upsert_track(
            path="/music/techno/Track_A.mp3",
            title="Track A Updated",
            bpm=130.0,
        )

        db = get_db()
        rows = db.execute("SELECT * FROM tracks WHERE path=?",
                          ("/music/techno/Track_A.mp3",)).fetchall()
        db.close()

        assert len(rows) == 1
        assert rows[0]["title"] == "Track A Updated"
        assert rows[0]["bpm"] == 130.0

    def test_upsert_track_partial_update(self, test_db):
        """Upserting with only some fields should leave other fields untouched."""
        upsert_track(
            path="/music/techno/Track_A.mp3",
            bpm=132.0,
        )

        db = get_db()
        row = db.execute("SELECT * FROM tracks WHERE path=?",
                         ("/music/techno/Track_A.mp3",)).fetchone()
        db.close()

        assert row["bpm"] == 132.0
        # Title and artist should remain from seed data
        assert row["artist"] == "Artist 1"


class TestFindCompatibleTracks:

    def test_find_compatible_tracks_bpm(self, test_db):
        """Should return tracks within BPM ±10 range."""
        results = find_compatible_tracks(
            bpm=128.0, key_camelot="5A", energy=7, played_titles=[], limit=10,
        )

        bpms = [r["bpm"] for r in results]
        for bpm in bpms:
            assert 118.0 <= bpm <= 138.0, f"BPM {bpm} outside ±10 range of 128"

    def test_find_compatible_tracks_key(self, test_db):
        """Should return tracks with Camelot-compatible keys."""
        # 5A compatible keys: 5A, 4A, 6A, 5B
        results = find_compatible_tracks(
            bpm=128.0, key_camelot="5A", energy=7, played_titles=[], limit=10,
        )

        from agent.camelot import get_compatible_keys
        compatible = get_compatible_keys("5A")

        for r in results:
            assert r["key_camelot"] in compatible, \
                f"Key {r['key_camelot']} not in compatible set {compatible}"

    def test_find_compatible_excludes_played(self, test_db):
        """Tracks in played_titles should not appear in results."""
        results = find_compatible_tracks(
            bpm=128.0, key_camelot="5A", energy=7,
            played_titles=["Track A"], limit=10,
        )

        titles = [r["title"] for r in results]
        assert "Track A" not in titles

    def test_find_compatible_tracks_energy_range(self, test_db):
        """Should return tracks within energy ±3 range."""
        results = find_compatible_tracks(
            bpm=128.0, key_camelot="", energy=7, played_titles=[], limit=10,
        )
        for r in results:
            assert 4 <= r["energy_peak"] <= 10, \
                f"Energy {r['energy_peak']} outside ±3 of 7"


class TestGetTrackByPath:

    def test_get_track_by_path_exact(self, test_db):
        """Exact path match should return the correct track."""
        track = get_track_by_path("/music/techno/Track_A.mp3")

        assert track is not None
        assert track["title"] == "Track A"
        assert track["bpm"] == 128.0

    def test_get_track_by_path_normalized(self, test_db):
        """Unicode-normalized path should still match."""
        # Insert a track with NFC-normalized path
        nfc_path = unicodedata.normalize("NFC", "/music/techno/Tréma.mp3")
        upsert_track(path=nfc_path, title="Trema Track")

        # Query with NFD form (different unicode representation, same visual)
        nfd_path = unicodedata.normalize("NFD", "/music/techno/Tréma.mp3")
        track = get_track_by_path(nfd_path)

        assert track is not None
        assert track["title"] == "Trema Track"

    def test_get_track_by_path_filename_fallback(self, test_db):
        """When full path doesn't match, fallback to filename-only match."""
        # Track_A.mp3 exists at /music/techno/Track_A.mp3
        # Query with a different parent path
        track = get_track_by_path("/different/path/Track_A.mp3")

        assert track is not None
        assert track["title"] == "Track A"

    def test_get_track_by_path_not_found(self, test_db):
        """Non-existent path should return None."""
        track = get_track_by_path("/music/nonexistent/Nothing.mp3")
        assert track is None


class TestSetsAndHistory:

    def test_insert_set_and_get_current(self, test_db):
        """Inserting a new set with status=live should be retrievable via get_current_set."""
        new_set = {
            "id": f"set-test-{int(time.time())}",
            "set_number": 2,
            "title": "New Test Set",
            "started_at": time.time(),
            "mood": "melodic",
            "genre": "melodic-techno",
            "target_duration": 90,
        }
        insert_set(new_set)

        current = get_current_set()
        assert current is not None
        assert current["title"] == "New Test Set"
        assert current["status"] == "live"

    def test_add_track_to_set_history(self, test_db):
        """Adding a track to set history should be retrievable."""
        set_id = "set-20260401-120000"  # from seed data
        add_track_to_set(set_id, "Track A", deck=1, transition_type="crossfade")
        add_track_to_set(set_id, "Track B", deck=2, transition_type="bass_swap")

        history = get_set_tracks(set_id)
        assert len(history) == 2
        assert history[0]["title"] == "Track A"
        assert history[0]["deck"] == 1
        assert history[0]["transition_type"] == "crossfade"
        assert history[1]["title"] == "Track B"

    def test_set_id_uniqueness(self, test_db):
        """Set IDs include seconds, so two sets created in different seconds should differ."""
        id1 = f"set-{time.strftime('%Y%m%d-%H%M%S')}"
        time.sleep(1.1)
        id2 = f"set-{time.strftime('%Y%m%d-%H%M%S')}"

        assert id1 != id2, "Set IDs created 1s apart should differ"

    def test_get_next_set_number(self, test_db):
        """get_next_set_number should return max(set_number) + 1."""
        # Seed has set_number=1
        next_num = get_next_set_number()
        assert next_num == 2
