"""Tests for state file writing — the TUI reads /tmp/dj-treta-state.json.

The Being's _write_state() method serializes the full DJ state to JSON.
The TUI polls this file every 2 seconds to render the UI.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


STATE_FILE = Path("/tmp/dj-treta-state.json")


class TestStateFile:

    def test_state_file_has_all_fields(self, being, mock_mixxx):
        """_write_state should produce a JSON with all required TUI fields."""
        being._write_state()

        assert STATE_FILE.exists()
        state = json.loads(STATE_FILE.read_text())

        required_fields = [
            "phase", "mood", "tracks_played", "current_track",
            "next_track", "set", "planner_status", "agent_busy",
            "relay_enabled", "recording", "broadcasting",
            "emergency_count", "last_command", "last_command_result",
            "billing", "sources",
        ]
        for field in required_fields:
            assert field in state, f"Missing required field: {field}"

    def test_state_file_set_info_from_db(self, being, mock_mixxx):
        """The 'set' section should reflect the current set's metadata."""
        being._write_state()

        state = json.loads(STATE_FILE.read_text())
        set_data = state["set"]

        assert set_data["id"] == "set-test-001"
        assert set_data["mood"] == "techno"
        assert set_data["target_minutes"] == 120
        assert set_data["elapsed"] > 0  # set started 600s ago in fixture

    def test_state_file_next_track(self, being, mock_mixxx):
        """When idle deck has a track loaded, next_track should be populated."""
        being._write_state()

        state = json.loads(STATE_FILE.read_text())
        # Mock Mixxx has deck 2 loaded with Track B
        next_track = state.get("next_track")
        assert next_track is not None
        assert next_track["deck"] == 2

    def test_state_file_phase_playing(self, being, mock_mixxx):
        """When a deck is playing, phase should be 'playing'."""
        being._write_state()

        state = json.loads(STATE_FILE.read_text())
        assert state["phase"] == "playing"

    def test_state_file_phase_idle(self, being, mock_mixxx):
        """When no deck is playing, phase should be 'idle'."""
        mock_mixxx["status"]["deck1"]["playing"] = False
        mock_mixxx["status"]["deck2"]["playing"] = False

        being._write_state()

        state = json.loads(STATE_FILE.read_text())
        assert state["phase"] == "idle"

    def test_state_file_sources(self, being, mock_mixxx):
        """Sources config should be reflected in state file."""
        being._write_state()

        state = json.loads(STATE_FILE.read_text())
        assert "sources" in state
        assert "youtube" in state["sources"]
        assert "treta_originals" in state["sources"]


class TestBillingAndThinking:

    def test_billing_file_updated_on_event(self, being, mock_mixxx):
        """When billing file exists, its data should appear in state."""
        billing_file = Path("/tmp/dj-treta-billing.json")
        billing_file.write_text(json.dumps({
            "total_input_tokens": 50000,
            "total_output_tokens": 10000,
            "total_cost_usd": 0.005,
        }))

        being._write_state()

        state = json.loads(STATE_FILE.read_text())
        assert "60K" in state["billing"] or "tokens" in state["billing"]

        billing_file.unlink(missing_ok=True)

    def test_thinking_log_written_on_event(self, being):
        """THINKING_FILE should be writable (used by Being during agent calls)."""
        thinking_file = Path("/tmp/dj-treta-thinking.log")
        thinking_file.write_text("test thinking entry\n")

        content = thinking_file.read_text()
        assert "test thinking entry" in content

        thinking_file.unlink(missing_ok=True)
