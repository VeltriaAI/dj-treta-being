"""Tests for DJTretaBeing._handle_command() — TUI/CLI command dispatch.

Commands arrive as JSON files written by the TUI/CLI. The Being polls for them,
parses, and dispatches. These tests verify the handler logic directly.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestMoodChange:

    def test_mood_change_updates_set(self, being):
        """change_mood command should update both being.mood and current_set mood."""
        result = being._handle_command("change_mood", {"mood": "melodic-techno"}, "cmd-1")

        assert being.mood == "melodic-techno"
        assert being.current_set["mood"] == "melodic-techno"
        assert being.current_set["genre"] == "melodic-techno"
        assert "melodic-techno" in result

    def test_mood_change_triggers_replan(self, being):
        """change_mood should set _tracks_since_plan to trigger immediate replanning."""
        being._tracks_since_plan = 0
        being._handle_command("change_mood", {"mood": "dark-techno"}, "cmd-2")

        assert being._tracks_since_plan == being.config.planner.replan_every_n_tracks

    def test_mood_change_sets_user_intent(self, being):
        """change_mood should set user_intent so the planner knows why mood changed."""
        being._handle_command("change_mood", {"mood": "ambient"}, "cmd-3")

        assert "ambient" in being.user_intent.lower()
        assert "Switch to" in being.user_intent or "ambient" in being.user_intent


class TestSkipCommand:

    def test_skip_does_direct_transition(self, being):
        """skip command should launch _agent_skip in a thread, not _invoke_agent directly."""
        with patch.object(being, "_agent_skip") as mock_skip:
            with patch("agent.main.threading.Thread") as mock_thread:
                mock_thread_instance = MagicMock()
                mock_thread.return_value = mock_thread_instance

                result = being._handle_command("skip", {}, "cmd-4")

                assert result == "processing..."
                mock_thread.assert_called_once()
                # Verify _agent_skip is the target
                call_kwargs = mock_thread.call_args
                assert call_kwargs[1]["target"] == being._agent_skip


class TestTalkCommand:

    def test_talk_captures_user_intent_on_play_request(self, being):
        """When user says 'play some bhojpuri', talk handler should capture
        the message as user_intent and extract mood."""
        with patch.object(being, "_agent_talk"):
            with patch("agent.main.threading.Thread") as mock_thread:
                mock_thread.return_value = MagicMock()

                being._handle_command("talk", {"message": "play some deep house"}, "cmd-5")

                # user_intent should capture the full message
                assert being.user_intent == "play some deep house"

    def test_talk_returns_processing(self, being):
        """talk command should return 'processing...' immediately (async)."""
        with patch("agent.main.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()

            result = being._handle_command("talk", {"message": "what are you playing?"}, "cmd-6")
            assert result == "processing..."

    def test_talk_no_message_returns_error(self, being):
        """talk with empty message should return an error, not crash."""
        result = being._handle_command("talk", {"message": ""}, "cmd-7")
        assert "No message" in result


class TestSourcesChange:

    def test_sources_change_rebuilds_agents(self, being):
        """Changing a source (youtube/originals) should recreate ADK agents."""
        with patch("agent.main.create_agents", return_value=(MagicMock(), MagicMock())) as mock_create:
            with patch("agent.main.App"), \
                 patch("agent.main.Runner"), \
                 patch("agent.main.EventsCompactionConfig"), \
                 patch.object(being, "_run_async"):
                being._handle_command("change_sources", {"source": "youtube", "enabled": False}, "cmd-8")

                assert being.config.sources.youtube is False
                mock_create.assert_called_once_with(being.config)
