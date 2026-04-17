"""Tests for agent.canonicalize — LLM track identity resolution + fallback.

Covers the 6 scenarios from the v8 Phase 0 plan:
  1. Original Mix defaulting (no version + no remixer → Original Mix)
  2. Remix detection (remixer extracted from title)
  3. Topic-channel stripping (YouTube auto-channel " - Topic" suffix)
  4. Emoji/decoration stripping via fallback heuristic
  5. LLM-unavailable fallback (exception path)
  6. Case-insensitive canonical 4-tuple match via DB lookup
"""

from unittest.mock import patch, MagicMock

import pytest

from agent.canonicalize import (
    llm_canonicalize,
    canonical_filename,
    _fallback_parse,
    _strip_topic,
)


def _mock_litellm_response(payload: dict):
    """Build a litellm.completion() stand-in returning `payload` as JSON."""
    import json
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps(payload)
    return resp


class TestOriginalMixDefault:
    """A plain-titled track (no explicit version, no remixer) should canonicalize
    to version='Original Mix' so it dedups against explicit 'Original Mix' rows."""

    def test_plain_title_defaults_to_original_mix(self):
        payload = {
            "artist": "Stephan Jolk", "song": "Morgen",
            "version": None, "remixer": None, "confidence": 0.98,
        }
        with patch("litellm.completion", return_value=_mock_litellm_response(payload)):
            out = llm_canonicalize("Stephan Jolk - Morgen", "Stephan Jolk - Topic", 410)

        assert out["canonical_artist"] == "Stephan Jolk"
        assert out["canonical_song"] == "Morgen"
        assert out["canonical_version"] == "Original Mix"  # defaulted from None
        assert out["remixer"] is None

    def test_explicit_original_mix_preserved(self):
        payload = {
            "artist": "Stephan Jolk", "song": "Morgen",
            "version": "Original Mix", "remixer": None, "confidence": 0.97,
        }
        with patch("litellm.completion", return_value=_mock_litellm_response(payload)):
            out = llm_canonicalize(
                "Stephan Jolk - Morgen (Original Mix) | MELODIC TECHNO",
                "Running Clouds", 412,
            )
        # Both inputs (plain + explicit) produce identical canonical — dedup works.
        assert out["canonical_version"] == "Original Mix"


class TestRemixDetection:
    """When title contains '(X Remix)', LLM should extract remixer and version."""

    def test_remixer_extracted(self):
        payload = {
            "artist": "Ellie Goulding, blackbear", "song": "Worry About Me",
            "version": "Remix", "remixer": "Lost Frequencies", "confidence": 0.96,
        }
        with patch("litellm.completion", return_value=_mock_litellm_response(payload)):
            out = llm_canonicalize(
                "Ellie Goulding, blackbear - Worry About Me (Lost Frequencies Remix)",
                "Lost Frequencies", 200,
            )

        assert out["canonical_artist"] == "Ellie Goulding, blackbear"
        assert out["canonical_song"] == "Worry About Me"
        assert out["remixer"] == "Lost Frequencies"
        assert out["canonical_version"] == "Remix"
        # When remixer present, Original Mix default does NOT fire
        assert out["canonical_version"] != "Original Mix"


class TestTopicChannelStrip:
    """YouTube auto-channels append ' - Topic' to artist name.

    _strip_topic is a pure function — tested directly.
    """

    @pytest.mark.parametrize("uploader, expected", [
        ("Stephan Jolk - Topic", "Stephan Jolk"),
        ("Artist Name - Topic", "Artist Name"),
        ("stephan jolk - topic", "stephan jolk"),  # case-insensitive match
        ("Not A Topic Channel", "Not A Topic Channel"),  # untouched
        ("Stephan Jolk - Topical", "Stephan Jolk - Topical"),  # word boundary
        ("", ""),
        (None, ""),
    ])
    def test_strip_variants(self, uploader, expected):
        assert _strip_topic(uploader) == expected


class TestFallbackDecorationStrip:
    """Fallback heuristic should strip (...), [...], and trailing | decorations."""

    def test_strips_parens_and_pipe_tail(self):
        out = _fallback_parse(
            "Stephan Jolk - Morgen (Original Mix) | MELODIC TECHNO",
            "Running Clouds",
        )
        # Fallback parse collapses "(Original Mix)" + "| MELODIC TECHNO"
        assert out["canonical_artist"] == "Stephan Jolk"
        assert out["canonical_song"] == "Morgen"
        assert out["canonical_version"] is None  # fallback doesn't detect version
        assert out["canonical_confidence"] == 0.3

    def test_strips_brackets(self):
        out = _fallback_parse(
            "YOTTO - Is This Trance? [Odd One Out]", "Odd One Out",
        )
        # Brackets stripped, dash-split recognises Artist - Song.
        # Note: "Is This Trance?" keeps the question mark (not a decoration).
        assert out["canonical_artist"] == "YOTTO"
        assert "Is This Trance" in out["canonical_song"]

    def test_uploader_used_when_no_dash_in_title(self):
        out = _fallback_parse("Some Song Name (Official Audio)", "ArtistChannel")
        assert out["canonical_artist"] == "ArtistChannel"


class TestLLMUnavailableFallback:
    """When litellm.completion raises, llm_canonicalize falls back to heuristic.

    This is the safety net so downloads never fail when LLM is unreachable.
    """

    def test_network_error_uses_fallback(self):
        def boom(*a, **kw):
            raise ConnectionError("LiteLLM unreachable")

        with patch("litellm.completion", side_effect=boom):
            out = llm_canonicalize(
                "Artist X - Song Y (Original Mix)", "Channel X", 200,
            )

        # Fallback parse returns confidence=0.3 with note indicating heuristic
        assert out["canonical_confidence"] == 0.3
        assert "fallback" in out["notes"].lower()
        assert out["canonical_artist"] == "Artist X"
        assert "Song Y" in out["canonical_song"]

    def test_invalid_json_response_uses_fallback(self):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "not valid json at all"

        with patch("litellm.completion", return_value=resp):
            out = llm_canonicalize("Test Artist - Test Song", "Test Ch", 180)

        assert out["canonical_confidence"] == 0.3
        assert out["canonical_artist"] == "Test Artist"


class TestCanonicalLookupCaseInsensitive:
    """find_track_by_canonical should match regardless of case on the 4-tuple."""

    def test_case_insensitive_match(self, test_db):
        from agent.db import upsert_track, find_track_by_canonical

        upsert_track(
            path="/music/bollyafro/afusic - Pal Pal (Madoc Remix).mp3",
            title="afusic - Pal Pal (Madoc Remix)",
            genre="bollyafro",
            source_url="https://youtu.be/HS6KeMUzQJE",
            canonical_artist="afusic",
            canonical_song="Pal Pal",
            canonical_version="Edit",
            remixer="Madoc",
            canonical_confidence=0.9,
        )

        # Exact
        row = find_track_by_canonical("afusic", "Pal Pal", "Edit", "Madoc")
        assert row is not None
        assert row["source_url"] == "https://youtu.be/HS6KeMUzQJE"

        # Differing case on every field still matches
        row = find_track_by_canonical("AFUSIC", "PAL PAL", "edit", "MADOC")
        assert row is not None
        assert row["canonical_artist"] == "afusic"

        # Different remixer — no match (correctly different track)
        row = find_track_by_canonical("afusic", "Pal Pal", "Edit", "Someone Else")
        assert row is None

    def test_null_remixer_distinguishes(self, test_db):
        """A row with remixer=NULL should not match when querying with a remixer,
        and vice versa."""
        from agent.db import upsert_track, find_track_by_canonical

        upsert_track(
            path="/music/techno/a.mp3",
            canonical_artist="Artist", canonical_song="Song",
            canonical_version="Original Mix", remixer=None,
        )

        # Looking up with NULL remixer matches
        row = find_track_by_canonical("Artist", "Song", "Original Mix", None)
        assert row is not None

        # Looking up with a remixer does NOT match the NULL-remixer row
        row = find_track_by_canonical("Artist", "Song", "Original Mix", "X")
        assert row is None


class TestCanonicalFilename:
    """canonical_filename builds filesystem-safe stems from canonical dict."""

    def test_plain_original_mix(self):
        canon = {
            "canonical_artist": "Stephan Jolk",
            "canonical_song": "Morgen",
            "canonical_version": "Original Mix",
            "remixer": None,
        }
        assert canonical_filename(canon) == "Stephan Jolk - Morgen (Original Mix)"

    def test_remix_uses_remixer_name(self):
        canon = {
            "canonical_artist": "Ellie Goulding, blackbear",
            "canonical_song": "Worry About Me",
            "canonical_version": "Remix",
            "remixer": "Lost Frequencies",
        }
        expected = "Ellie Goulding, blackbear - Worry About Me (Lost Frequencies Remix)"
        assert canonical_filename(canon) == expected

    def test_filesystem_unsafe_chars_replaced(self):
        canon = {
            "canonical_artist": "Artist/X",
            "canonical_song": "Song: Part 1?",
            "canonical_version": "Original Mix",
            "remixer": None,
        }
        # /, :, ? are filesystem-unsafe on some OSes — must be replaced
        out = canonical_filename(canon)
        assert "/" not in out
        assert ":" not in out
        assert "?" not in out
