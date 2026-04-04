"""Tests for evolution.py — pattern detection and self-learning."""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestCollectData:

    def test_returns_structured_dict(self, being):
        data = being._collect_evolution_data()
        assert "recent_tracks" in data
        assert "transition_quality" in data
        assert "feedback_summary" in data
        assert "energy_summary" in data

    def test_recent_tracks_from_played(self, being):
        being.tracks_played = [{"title": "Track A"}, {"title": "Track B"}]
        data = being._collect_evolution_data()
        assert "Track A" in data["recent_tracks"]


class TestDetectPatterns:

    def test_detects_auto_transition_dominance(self, being):
        data = {
            "transition_quality": {"agent": 2, "auto": 8, "emergency": 0},
            "feedback_summary": {"likes": 0, "dislikes": 0, "liked_genres": []},
            "energy_summary": "",
            "recent_tracks": [],
        }
        patterns = being._detect_patterns(data)
        assert any(p["type"] == "transition_timing" for p in patterns)

    def test_detects_emergency_frequency(self, being):
        being._emergency_count = 5
        data = {
            "transition_quality": {"agent": 5, "auto": 2, "emergency": 5},
            "feedback_summary": {"likes": 0, "dislikes": 0, "liked_genres": []},
            "energy_summary": "",
            "recent_tracks": [],
        }
        patterns = being._detect_patterns(data)
        assert any(p["type"] == "emergency_frequency" for p in patterns)

    def test_no_patterns_when_healthy(self, being):
        data = {
            "transition_quality": {"agent": 8, "auto": 2, "emergency": 0},
            "feedback_summary": {"likes": 5, "dislikes": 1, "liked_genres": ["techno"]},
            "energy_summary": "avg:6.5 peak:9 samples:30",
            "recent_tracks": [],
        }
        patterns = being._detect_patterns(data)
        assert len(patterns) == 0


class TestShouldTriggerEvolution:

    def test_disabled_returns_false(self, being):
        being.config.evolution.auto_evolve = False
        patterns = [{"type": "test", "confidence": 0.9, "occurrences": 10, "suggested_action": "fix"}]
        should, _ = being._should_trigger_evolution(patterns)
        assert not should

    def test_low_confidence_returns_false(self, being):
        being.config.evolution.auto_evolve = True
        patterns = [{"type": "test", "confidence": 0.5, "occurrences": 10, "suggested_action": "fix"}]
        should, _ = being._should_trigger_evolution(patterns)
        assert not should

    def test_high_confidence_returns_true(self, being, test_db):
        being.config.evolution.auto_evolve = True
        patterns = [{"type": "test", "confidence": 0.9, "occurrences": 10, "suggested_action": "fix something"}]
        should, goal = being._should_trigger_evolution(patterns)
        assert should
        assert "fix something" in goal
