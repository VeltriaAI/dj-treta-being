"""Tests for agent.playlist_schema — PlaylistV1 validation + pick helper.

Phase 3 of v8 makes planner output structured JSON (session.playlist). A
malformed LLM response must fail loud (ValueError) so the previous valid
playlist stays authoritative — never silently corrupt downstream state.
"""

import pytest

from agent.playlist_schema import (
    validate_playlist,
    pick_next_candidate,
    PlaylistValidationError,
)


def _valid_playlist() -> dict:
    return {
        "planned_at": 1776500000.0,
        "mood_snapshot": "bollyafro",
        "reasoning_summary": "5 bolly tracks matching current BPM.",
        "tracks": [
            {"rank": 1, "path": "/music/a.mp3", "title": "A", "bpm": 120.0,
             "key_camelot": "8A", "energy": 7, "reason": "fits",
             "transition_hint": {"technique": "crossfade", "duration": 45, "at_section": "outro"}},
            {"rank": 2, "path": "/music/b.mp3", "title": "B", "bpm": 122.0,
             "key_camelot": "9A", "energy": 8, "reason": "backup"},
        ],
    }


class TestValidateHappy:

    def test_valid_minimal(self):
        data = {
            "planned_at": 100.0,
            "mood_snapshot": "deep-house",
            "tracks": [{"rank": 1, "path": "/t.mp3"}],
        }
        out = validate_playlist(data)
        assert out["planned_at"] == 100.0
        assert out["mood_snapshot"] == "deep-house"
        assert len(out["tracks"]) == 1
        assert out["reasoning_summary"] == ""

    def test_valid_full(self):
        data = _valid_playlist()
        out = validate_playlist(data)
        assert len(out["tracks"]) == 2
        assert out["tracks"][0]["transition_hint"]["technique"] == "crossfade"


class TestValidateRejects:

    def test_non_dict_payload_rejected(self):
        with pytest.raises(PlaylistValidationError):
            validate_playlist([])
        with pytest.raises(PlaylistValidationError):
            validate_playlist("string")

    def test_missing_planned_at(self):
        with pytest.raises(PlaylistValidationError, match="planned_at"):
            validate_playlist({"mood_snapshot": "x", "tracks": [{"rank": 1, "path": "/a"}]})

    def test_empty_tracks_list_allowed(self):
        """Empty tracks list is now valid — planner uses it to signal
        'library thin, need download'. reasoning_summary carries the why."""
        out = validate_playlist({
            "planned_at": 1.0, "mood_snapshot": "x",
            "reasoning_summary": "library empty — download needed",
            "tracks": [],
        })
        assert out["tracks"] == []
        assert "library empty" in out["reasoning_summary"]

    def test_missing_tracks_key(self):
        with pytest.raises(PlaylistValidationError, match="tracks"):
            validate_playlist({"planned_at": 1.0, "mood_snapshot": "x"})

    def test_duplicate_rank_rejected(self):
        data = {
            "planned_at": 1.0,
            "mood_snapshot": "x",
            "tracks": [
                {"rank": 1, "path": "/a"},
                {"rank": 1, "path": "/b"},  # duplicate rank
            ],
        }
        with pytest.raises(PlaylistValidationError, match="duplicate rank"):
            validate_playlist(data)

    def test_duplicate_path_rejected(self):
        data = {
            "planned_at": 1.0,
            "mood_snapshot": "x",
            "tracks": [
                {"rank": 1, "path": "/same"},
                {"rank": 2, "path": "/same"},  # duplicate path
            ],
        }
        with pytest.raises(PlaylistValidationError, match="duplicate path"):
            validate_playlist(data)

    def test_missing_path_rejected(self):
        with pytest.raises(PlaylistValidationError, match="path"):
            validate_playlist({
                "planned_at": 1.0, "mood_snapshot": "x",
                "tracks": [{"rank": 1}],
            })

    def test_bad_rank_type_rejected(self):
        with pytest.raises(PlaylistValidationError, match="rank"):
            validate_playlist({
                "planned_at": 1.0, "mood_snapshot": "x",
                "tracks": [{"rank": "1", "path": "/a"}],  # string not int
            })

    def test_energy_out_of_range_rejected(self):
        with pytest.raises(PlaylistValidationError, match="energy"):
            validate_playlist({
                "planned_at": 1.0, "mood_snapshot": "x",
                "tracks": [{"rank": 1, "path": "/a", "energy": 11}],
            })


class TestTransitionHintNormalization:

    def test_unknown_technique_coerced(self):
        data = {
            "planned_at": 1.0, "mood_snapshot": "x",
            "tracks": [{"rank": 1, "path": "/a",
                        "transition_hint": {"technique": "telepathy", "duration": 45}}],
        }
        out = validate_playlist(data)
        # Unknown technique downgraded to crossfade, doesn't reject the playlist
        assert out["tracks"][0]["transition_hint"]["technique"] == "crossfade"

    def test_duration_clamped(self):
        data = {
            "planned_at": 1.0, "mood_snapshot": "x",
            "tracks": [{"rank": 1, "path": "/a",
                        "transition_hint": {"duration": 500}}],
        }
        out = validate_playlist(data)
        assert out["tracks"][0]["transition_hint"]["duration"] == 90

        data["tracks"][0]["transition_hint"]["duration"] = 2
        out = validate_playlist(data)
        assert out["tracks"][0]["transition_hint"]["duration"] == 10


class TestPickNextCandidate:

    def test_picks_rank_1_when_available(self):
        pl = _valid_playlist()
        pick = pick_next_candidate(pl, exclude_paths=set(), played_titles=[])
        assert pick["rank"] == 1

    def test_skips_tracks_on_deck(self):
        pl = _valid_playlist()
        pick = pick_next_candidate(pl, exclude_paths={"/music/a.mp3"}, played_titles=[])
        assert pick["rank"] == 2

    def test_skips_played_titles(self):
        pl = _valid_playlist()
        pick = pick_next_candidate(pl, exclude_paths=set(), played_titles=["A"])
        assert pick["rank"] == 2

    def test_returns_none_when_all_excluded(self):
        pl = _valid_playlist()
        pick = pick_next_candidate(
            pl,
            exclude_paths={"/music/a.mp3", "/music/b.mp3"},
            played_titles=[],
        )
        assert pick is None

    def test_returns_none_for_empty_playlist(self):
        assert pick_next_candidate(None, set(), []) is None
        assert pick_next_candidate({}, set(), []) is None
        assert pick_next_candidate({"tracks": []}, set(), []) is None
