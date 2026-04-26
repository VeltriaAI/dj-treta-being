"""Tests for the Mixxx ↔ session boundary in playback_applier.

The big invariant: paths flowing IN from Mixxx (which speaks absolute) must
land in our world (which speaks relative-to-music_dir post-v9-migration).
get_deck_paths is the single normalization point — break that, and dedup
silently fails everywhere it's consumed (planner exclude_paths, heartbeat
duplicate detection).
"""

from unittest.mock import patch, MagicMock

from agent.playback_applier import get_deck_paths


def _mock_resp(file_path: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"file_path": file_path}
    return r


class TestGetDeckPaths:
    def test_normalizes_absolute_to_relative(self, config):
        """Mixxx returns absolute; we return relative-to-music_dir."""
        music_dir = config.library.music_dir
        abs_path = f"{music_dir}/melodic-techno/Foo.mp3"

        with patch("agent.playback_applier.httpx.get",
                   return_value=_mock_resp(abs_path)), \
             patch("agent.config.load_config", return_value=config):
            paths = get_deck_paths("http://localhost:7778")

        assert paths[1] == "melodic-techno/Foo.mp3"
        assert paths[2] == "melodic-techno/Foo.mp3"

    def test_relative_passthrough(self, config):
        """If Mixxx ever returns a relative path, leave it alone."""
        with patch("agent.playback_applier.httpx.get",
                   return_value=_mock_resp("melodic-techno/Foo.mp3")), \
             patch("agent.config.load_config", return_value=config):
            paths = get_deck_paths("http://localhost:7778")
        assert paths[1] == "melodic-techno/Foo.mp3"

    def test_empty_when_mixxx_unreachable(self, config):
        """Network error → empty strings, no raise."""
        with patch("agent.playback_applier.httpx.get",
                   side_effect=Exception("connection refused")), \
             patch("agent.config.load_config", return_value=config):
            paths = get_deck_paths("http://localhost:7778")
        assert paths == {1: "", 2: ""}

    def test_empty_file_path_preserved(self, config):
        """Mixxx returns empty file_path (deck empty) → empty string out."""
        with patch("agent.playback_applier.httpx.get",
                   return_value=_mock_resp("")), \
             patch("agent.config.load_config", return_value=config):
            paths = get_deck_paths("http://localhost:7778")
        assert paths == {1: "", 2: ""}

    def test_path_outside_music_dir_unchanged(self, config):
        """Ad-hoc track dropped into Mixxx from outside library: leave as-is.

        It will fail to match any playlist entry (correct — it's not a
        library track), but we don't crash or mangle it.
        """
        outside = "/tmp/some-other-file.mp3"
        with patch("agent.playback_applier.httpx.get",
                   return_value=_mock_resp(outside)), \
             patch("agent.config.load_config", return_value=config):
            paths = get_deck_paths("http://localhost:7778")
        assert paths[1] == outside


class TestDedupRegression:
    """End-to-end check of the path-form mismatch that caused
    same-track-on-both-decks: planner's exclude_paths (sourced via
    get_deck_paths) must intersect with playlist entry paths
    (relative since v9 migration). Without normalization these are
    different strings and the duplicate gets picked.
    """

    def test_planner_exclude_intersects_relative_playlist(self, config):
        from agent.playlist_schema import pick_next_candidate

        music_dir = config.library.music_dir
        rel_path = "melodic-techno/Foo.mp3"
        abs_path = f"{music_dir}/{rel_path}"

        with patch("agent.playback_applier.httpx.get",
                   return_value=_mock_resp(abs_path)), \
             patch("agent.config.load_config", return_value=config):
            deck_paths = get_deck_paths("http://localhost:7778")

        exclude = {p for p in deck_paths.values() if p}

        playlist = {
            "tracks": [
                {"rank": 1, "path": rel_path, "title": "Foo",
                 "downloaded": True},
                {"rank": 2, "path": "melodic-techno/Bar.mp3",
                 "title": "Bar", "downloaded": True},
            ]
        }
        pick = pick_next_candidate(playlist, exclude, [])
        assert pick is not None
        assert pick["path"] == "melodic-techno/Bar.mp3", (
            "Rank-1 should be excluded because Mixxx already has it loaded"
        )
