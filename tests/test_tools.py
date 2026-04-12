"""Tests for agent.tools — pure function logic, no Mixxx needed.

Tests here cover utility functions that don't require a running Mixxx:
- _normalize_for_search (unicode, emoji stripping)
- schedule_transition (writes JSON file)
- Camelot key compatibility
- _format_timeline / _get_current_section on the Being
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent.tools import _normalize_for_search
from agent.camelot import (
    get_compatible_keys,
    key_compatibility_score,
    mixxx_key_to_camelot,
    KEY_TO_CAMELOT,
)


class TestNormalizeForSearch:

    def test_basic_lowercase(self):
        """Should lowercase input."""
        assert _normalize_for_search("Hello World") == "hello world"

    def test_strip_emoji(self):
        """Should strip emoji (category So) from search strings."""
        result = _normalize_for_search("DJ 🎵 Treta 🔥 Mix")
        assert "🎵" not in result
        assert "🔥" not in result
        assert "dj" in result
        assert "treta" in result

    def test_normalize_dashes(self):
        """Should normalize en-dash and em-dash to regular hyphen."""
        assert _normalize_for_search("track–one—two") == "track-one-two"

    def test_normalize_fullwidth(self):
        """Should normalize fullwidth characters (NFKC)."""
        result = _normalize_for_search("ＤＪ Ｔｒｅｔａ")
        assert result == "dj treta"

    def test_unicode_accents_preserved(self):
        """NFKC normalization preserves accented characters."""
        result = _normalize_for_search("Café Résumé")
        assert "café" in result or "cafe" in result  # NFKC may or may not decompose

    def test_empty_string(self):
        """Empty input should return empty string."""
        assert _normalize_for_search("") == ""

    def test_only_emoji(self):
        """String of only emojis should return empty or whitespace-stripped."""
        result = _normalize_for_search("🎶🎵🔊")
        assert result.strip() == ""


class TestCamelotCompatibleKeys:

    def test_compatible_keys_returns_four(self):
        """Each Camelot code should have exactly 4 compatible keys
        (same, +1, -1, relative major/minor)."""
        result = get_compatible_keys("8A")
        assert len(result) == 4
        assert "8A" in result  # same
        assert "9A" in result  # +1
        assert "7A" in result  # -1
        assert "8B" in result  # relative major

    def test_compatible_keys_wraps_around(self):
        """Camelot wheel wraps: 12A+1 = 1A, 1A-1 = 12A."""
        result = get_compatible_keys("12A")
        assert "1A" in result   # +1 wraps
        assert "11A" in result  # -1

        result = get_compatible_keys("1B")
        assert "2B" in result   # +1
        assert "12B" in result  # -1 wraps

    def test_same_key_score_10(self):
        """Identical keys should score 10."""
        assert key_compatibility_score("Am", "Am") == 10

    def test_adjacent_key_score_8(self):
        """Adjacent/relative keys should score 8."""
        # Am = 8A, C = 8B (relative major)
        assert key_compatibility_score("Am", "C") == 8

    def test_far_key_score_2(self):
        """Distant keys should score 2."""
        # Am (8A) vs Ebm (2A) — 6 steps apart
        assert key_compatibility_score("Am", "Ebm") == 2

    def test_unknown_key_score_5(self):
        """Unknown or empty keys should get neutral score 5."""
        assert key_compatibility_score("", "Am") == 5
        assert key_compatibility_score("Am", "") == 5

    def test_invalid_camelot_returns_empty(self):
        """Invalid Camelot codes should return empty list."""
        assert get_compatible_keys("") == []
        assert get_compatible_keys("X") == []
        assert get_compatible_keys("13A") == []

    def test_mixxx_key_to_camelot(self):
        """Mixxx key codes should map to correct Camelot codes."""
        assert mixxx_key_to_camelot(13) == "5A"   # Cm
        assert mixxx_key_to_camelot(22) == "8A"   # Am
        assert mixxx_key_to_camelot(1) == "8B"    # C major
        assert mixxx_key_to_camelot(0) is None     # INVALID


class TestScheduleTransition:

    def test_schedule_transition_writes_file(self, mock_mixxx):
        """schedule_transition should write a JSON file to /tmp."""
        sched_path = Path("/tmp/dj-treta-scheduled-transition.json")
        lock_path = Path("/tmp/dj-treta-transition-pending.lock")
        sched_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)

        # Patch load_config to return test config
        from agent.config import Config
        test_cfg = Config()

        with patch("agent.tools.helpers.load_config", return_value=test_cfg):
            from agent.tools import schedule_transition
            result = schedule_transition(
                to_deck=2, at_position=180, technique="bass_swap", duration=45,
            )

        assert sched_path.exists()
        data = json.loads(sched_path.read_text())
        assert data["toDeck"] == 2
        assert data["technique"] == "bass_swap"
        assert data["duration"] == 45
        assert data.get("bpmAfter") == "reset"
        assert data.get("glideDuration") == 60
        assert "Scheduled" in result

        sched_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


class TestFormatTimeline:

    def test_format_timeline_compact(self, being):
        """_format_timeline should produce a readable timeline string."""
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
                {"start": 30, "end": 120, "section": "buildup", "energy": 6},
                {"start": 120, "end": 240, "section": "drop", "energy": 9},
                {"start": 240, "end": 300, "section": "outro", "energy": 3},
            ]),
            "bpm": 128,
            "key_musical": "Am",
            "energy_peak": 9,
        }

        result = being._format_timeline(meta)
        assert "intro" in result
        assert "drop" in result
        assert "outro" in result
        assert "→" in result  # sections joined with arrow

    def test_format_timeline_no_analysis(self, being):
        """When no timeline data, should return basic info string."""
        meta = {"bpm": 128, "key_musical": "Am", "energy_peak": 7}
        result = being._format_timeline(meta)
        assert "128" in result
        assert "Am" in result

    def test_format_timeline_none(self, being):
        """None metadata should return '(no analysis)'."""
        result = being._format_timeline(None)
        assert "no analysis" in result


class TestFindCurrentSection:

    def test_find_current_section_in_middle(self, being):
        """Should identify the section at a given position."""
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
                {"start": 30, "end": 120, "section": "buildup", "energy": 6},
                {"start": 120, "end": 240, "section": "drop", "energy": 9},
            ]),
        }

        result = being._get_current_section(meta, position=75.0)
        assert "buildup" in result

    def test_find_current_section_at_start(self, being):
        """Position 0 should be in the intro section."""
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
                {"start": 30, "end": 120, "section": "drop", "energy": 9},
            ]),
        }

        result = being._get_current_section(meta, position=5.0)
        assert "intro" in result

    def test_find_current_section_no_meta(self, being):
        """None metadata should return 'unknown'."""
        result = being._get_current_section(None, position=50.0)
        assert result == "unknown"
