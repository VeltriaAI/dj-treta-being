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

    def test_skip_command_dispatches_agent_skip(self, being):
        """skip command should launch _agent_skip in a thread."""
        with patch("agent.commands.threading.Thread") as mock_thread:
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance

            result = being._handle_command("skip", {}, "cmd-4")

            assert result == "processing..."
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs[1]["target"] == being._agent_skip

    def test_agent_skip_emits_user_skip_signal(self, being):
        """Phase A2: _agent_skip writes session.user_skip signal and returns
        immediately. No direct do_transition call; the DJ agent consumes
        the signal on the next heartbeat P4 tick. Watchdog P2 is the
        fallback if DJ hangs >5s."""
        # Session singleton can carry state across tests in the same run —
        # reset the signal so the pre-condition holds.
        being.session.user_skip = None

        being._agent_skip()

        # Signal emitted with correct shape.
        sig = being.session.user_skip
        assert sig is not None
        assert sig["style"] == "fast"
        assert isinstance(sig["ts"], float)
        assert sig["directive"] is None
        assert "signaled" in being._last_result.lower()


class TestTalkCommand:

    def test_talk_routes_to_being_talk(self, being):
        """talk command should route to _being_talk (Being agent handles conversation)."""
        with patch("agent.commands.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()

            result = being._handle_command("talk", {"message": "play some deep house"}, "cmd-5")

            assert result == "processing..."
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs[1]["target"] == being._being_talk

    def test_talk_readonly_passes_flag(self, being):
        """talk with readonly=True should pass the flag to _being_talk."""
        with patch("agent.commands.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()

            being._handle_command("talk", {"message": "what are you playing?", "readonly": True}, "cmd-6")

            call_kwargs = mock_thread.call_args
            # Args should include readonly=True
            assert call_kwargs[1]["args"] == ("what are you playing?", "cmd-6", True)

    def test_talk_returns_processing(self, being):
        """talk command should return 'processing...' immediately (async)."""
        with patch("agent.commands.threading.Thread") as mock_thread:
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
        with patch("agent.commands.create_agents", return_value=(MagicMock(), MagicMock(), MagicMock())) as mock_create:
            with patch("agent.commands.App"), \
                 patch("agent.commands.Runner"), \
                 patch("agent.commands.EventsCompactionConfig"), \
                 patch.object(being, "_run_async"):
                being._handle_command("change_sources", {"source": "youtube", "enabled": False}, "cmd-8")

                assert being.config.sources.youtube is False
                mock_create.assert_called_once_with(being.config)
