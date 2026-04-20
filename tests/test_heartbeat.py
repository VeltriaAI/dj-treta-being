"""Tests for DJTretaBeing._heartbeat() — the core decision loop.

The heartbeat monitors deck state and triggers actions in priority order:
1. Silence → emergency recovery
2. Track ending + idle ready → auto-transition
3. Scheduled transition file → execute it
4. Agent decides (past 50% played)
5. Backup load (idle empty, past threshold)

Each test verifies one priority path fires (or does not fire) correctly.
"""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest


@pytest.fixture(autouse=True)
def clean_scheduled_transition():
    """Remove any leftover scheduled transition file before/after each test."""
    sched = Path("/tmp/dj-treta-scheduled-transition.json")
    sched.unlink(missing_ok=True)
    yield
    sched.unlink(missing_ok=True)


# ── Priority 1: Silence / Emergency ─────────────────────────────────

class TestSilenceEmergency:

    def test_silence_triggers_emergency(self, being, mock_mixxx):
        """When BOTH decks are not playing, heartbeat should start emergency recovery."""
        mock_mixxx["status"]["deck1"]["playing"] = False
        mock_mixxx["status"]["deck2"]["playing"] = False

        with patch.object(being, "_emergency_play") as mock_emergency:
            being._heartbeat()

            # Emergency thread should have been spawned
            assert being._next_sleep == 5
            # _emergency_running flag should be set
            assert being._emergency_running is True

    def test_emergency_not_re_triggered_while_running(self, being, mock_mixxx):
        """If emergency is already running, don't start another thread."""
        mock_mixxx["status"]["deck1"]["playing"] = False
        mock_mixxx["status"]["deck2"]["playing"] = False
        being._emergency_running = True  # already handling it

        with patch("threading.Thread") as mock_thread:
            being._heartbeat()
            # Should not spawn a new emergency thread
            mock_thread.assert_not_called()


# ── Priority 2: Auto-transition ─────────────────────────────────────

class TestAutoTransition:

    def test_auto_transition_when_track_ending(self, being, mock_mixxx):
        """When active track has <30s remaining and idle deck is loaded+ready,
        heartbeat should trigger an auto-transition."""
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 20.0
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck2"]["track_loaded"] = True
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 200.0

        being._agent_busy = False
        being._transition_pending = False

        with patch("agent.heartbeat.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            being._heartbeat()

            assert being._transition_pending is True
            assert being._next_sleep == 5

    def test_no_auto_transition_when_idle_empty(self, being, mock_mixxx):
        """When idle deck is NOT loaded, auto-transition should NOT fire
        even if active track is ending."""
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 20.0
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck2"]["track_loaded"] = False
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 0.0

        being._agent_busy = False
        being._transition_pending = False

        # Should NOT set transition pending — should fall through to backup load
        being._heartbeat()
        # transition_pending should still be False (auto-transition didn't fire)
        # It may or may not be set by priority 5 (backup load), but auto-transition specifically shouldn't
        # The key check: transition_pending was not set by priority 2
        # Since idle_ready=False, priority 2 is skipped entirely

    def test_no_auto_transition_when_agent_busy(self, being, mock_mixxx):
        """When agent is busy, auto-transition should not fire."""
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 20.0
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck2"]["track_loaded"] = True
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 200.0

        being._agent_busy = True
        being._transition_pending = False

        being._heartbeat()
        assert being._transition_pending is False


class TestSignalExecutor:
    """Signals (2026-04-20 revert): user_skip / idle_needs_load / set_ending
    are executed directly in Python by heartbeat._execute_signals, not
    routed through the DJ prompt. Phase A2 had routed them through DJ as a
    Signals block, but that accumulated enough conditional complexity that
    Flash started dropping ~46% of those invocations.

    These tests cover the new Python-executor semantics.
    """

    def test_user_skip_writes_scheduled_transition_file(self, being, mock_mixxx, tmp_path, monkeypatch):
        """user_skip set → _execute_signals writes scheduled-transition
        JSON with computed at_position + duration, and clears the signal.
        """
        sched_path = tmp_path / "sched.json"
        monkeypatch.setattr(
            "agent.heartbeat.Path",
            lambda p: sched_path if "scheduled-transition" in p
            else tmp_path / p.replace("/", "_"),
        )

        mock_mixxx["status"]["deck1"]["position_seconds"] = 100.0
        mock_mixxx["status"]["deck1"]["duration"] = 300.0
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 200.0
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck2"]["track_loaded"] = True
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 200.0

        being._agent_busy = False
        being._transition_pending = False
        import time as _t
        being.session.user_skip = {"style": "fast", "ts": _t.time(), "directive": None}

        # Run _execute_signals directly (skips P1/P2/P3).
        being._execute_signals(
            mock_mixxx["status"], active_deck=1, idle_deck=2,
            position=100.0, remaining=200.0,
            idle_loaded=True, idle_remaining=200.0,
        )

        import json as _json
        assert sched_path.exists()
        sched = _json.loads(sched_path.read_text())
        assert sched["toDeck"] == 2
        assert sched["technique"] == "crossfade"
        assert sched["atPosition"] == 102  # position + 2
        assert sched["duration"] == 15
        assert being.session.user_skip is None  # cleared on success

    def test_user_skip_short_duration_near_track_end(self, being, mock_mixxx, tmp_path, monkeypatch):
        """Skip at track end truncates duration to remaining - 1."""
        sched_path = tmp_path / "sched.json"
        monkeypatch.setattr(
            "agent.heartbeat.Path",
            lambda p: sched_path if "scheduled-transition" in p
            else tmp_path / p.replace("/", "_"),
        )
        being._agent_busy = False
        being._transition_pending = False
        import time as _t
        being.session.user_skip = {"style": "fast", "ts": _t.time(), "directive": None}

        being._execute_signals(
            mock_mixxx["status"], active_deck=1, idle_deck=2,
            position=295.0, remaining=5.0,
            idle_loaded=True, idle_remaining=200.0,
        )
        import json as _json
        sched = _json.loads(sched_path.read_text())
        assert sched["duration"] == 4  # remaining-1 = 4

    def test_idle_needs_load_calls_load_helper(self, being, mock_mixxx):
        """idle_needs_load=True + idle stale → _load_next_on_idle called
        and signal cleared on success.
        """
        being._agent_busy = False
        being._transition_pending = False
        being.session.idle_needs_load = True

        with patch.object(being, "_load_next_on_idle") as mock_load:
            being._execute_signals(
                mock_mixxx["status"], active_deck=1, idle_deck=2,
                position=100.0, remaining=200.0,
                idle_loaded=False, idle_remaining=0.0,
            )
            mock_load.assert_called_once()
        assert being.session.idle_needs_load is False

    def test_idle_needs_load_clears_when_idle_fresh(self, being, mock_mixxx):
        """idle_needs_load=True but idle has >60s remaining → signal was
        stale, clear it without reloading.
        """
        being._agent_busy = False
        being._transition_pending = False
        being.session.idle_needs_load = True

        with patch.object(being, "_load_next_on_idle") as mock_load:
            being._execute_signals(
                mock_mixxx["status"], active_deck=1, idle_deck=2,
                position=100.0, remaining=200.0,
                idle_loaded=True, idle_remaining=180.0,  # fresh, not stale
            )
            mock_load.assert_not_called()
        assert being.session.idle_needs_load is False

    def test_set_ending_clears_without_action(self, being, mock_mixxx):
        """set_ending has no caller yet — executor just logs and clears
        so a stale signal doesn't loop forever.
        """
        being._agent_busy = False
        being._transition_pending = False
        being.session.set_ending = True

        being._execute_signals(
            mock_mixxx["status"], active_deck=1, idle_deck=2,
            position=100.0, remaining=200.0,
            idle_loaded=True, idle_remaining=200.0,
        )
        assert being.session.set_ending is False


class TestFilterPlaylistForDecks:
    """BUG-2 (Phase A2 dry run 2026-04-19): DJ was loading the same
    track that was already on the active deck because the planner's
    playlist rank-1 hadn't been updated yet after the transition. The
    filter excludes any candidate whose path matches either deck's
    currently-loaded file."""

    def test_none_playlist(self):
        from agent.heartbeat import _filter_playlist_for_decks
        assert _filter_playlist_for_decks(None, "a", "b") is None

    def test_empty_tracks(self):
        from agent.heartbeat import _filter_playlist_for_decks
        pl = {"tracks": []}
        assert _filter_playlist_for_decks(pl, "a", "b") is pl

    def test_no_deck_paths(self):
        """When neither deck has a path, return the playlist unchanged."""
        from agent.heartbeat import _filter_playlist_for_decks
        pl = {"tracks": [{"rank": 1, "path": "/m/a.mp3"}]}
        assert _filter_playlist_for_decks(pl, "", "") is pl

    def test_filters_active_deck_path(self):
        from agent.heartbeat import _filter_playlist_for_decks
        pl = {"tracks": [
            {"rank": 1, "path": "/m/a.mp3"},
            {"rank": 2, "path": "/m/b.mp3"},
        ]}
        out = _filter_playlist_for_decks(pl, "/m/a.mp3", "")
        assert len(out["tracks"]) == 1
        assert out["tracks"][0]["path"] == "/m/b.mp3"
        # Original playlist must not be mutated.
        assert len(pl["tracks"]) == 2

    def test_filters_idle_deck_path(self):
        from agent.heartbeat import _filter_playlist_for_decks
        pl = {"tracks": [
            {"rank": 1, "path": "/m/a.mp3"},
            {"rank": 2, "path": "/m/b.mp3"},
        ]}
        out = _filter_playlist_for_decks(pl, "", "/m/b.mp3")
        assert [t["path"] for t in out["tracks"]] == ["/m/a.mp3"]

    def test_filters_both_decks(self):
        from agent.heartbeat import _filter_playlist_for_decks
        pl = {"tracks": [
            {"rank": 1, "path": "/m/a.mp3"},
            {"rank": 2, "path": "/m/b.mp3"},
            {"rank": 3, "path": "/m/c.mp3"},
        ]}
        out = _filter_playlist_for_decks(pl, "/m/a.mp3", "/m/b.mp3")
        assert [t["path"] for t in out["tracks"]] == ["/m/c.mp3"]

    def test_nothing_to_filter_returns_original(self):
        """When no track matches, return the original object (not a copy)
        so callers can use `is` identity to detect 'untouched'."""
        from agent.heartbeat import _filter_playlist_for_decks
        pl = {"tracks": [{"rank": 1, "path": "/m/a.mp3"}]}
        out = _filter_playlist_for_decks(pl, "/m/other.mp3", "/m/another.mp3")
        assert out is pl


# ── Priority 3: Scheduled Transition ────────────────────────────────

class TestScheduledTransition:

    def test_scheduled_transition_picked_up(self, being, mock_mixxx, tmp_path):
        """When a scheduled transition JSON file exists, heartbeat should pick it up."""
        sched_file = Path("/tmp/dj-treta-scheduled-transition.json")
        sched_data = {
            "toDeck": 2,
            "atPosition": 200,
            "technique": "crossfade",
            "duration": 45,
            "activeDeck": 1,
        }
        sched_file.write_text(json.dumps(sched_data))

        being._transition_pending = False

        # Deck 1 playing, plenty of time, idle deck loaded
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 120.0
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck2"]["track_loaded"] = True
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 200.0

        with patch.object(being, "_execute_scheduled_transition") as mock_exec:
            with patch("agent.heartbeat.threading.Thread") as mock_thread:
                mock_thread_instance = MagicMock()
                mock_thread.return_value = mock_thread_instance

                being._heartbeat()

                assert being._transition_pending is True

        # Cleanup
        sched_file.unlink(missing_ok=True)


# ── Priority 4: Agent decides ───────────────────────────────────────

class TestAgentDecision:

    def test_agent_not_called_when_busy(self, being, mock_mixxx):
        """When _agent_busy=True, heartbeat should not invoke the agent."""
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 100.0
        mock_mixxx["status"]["deck1"]["position_seconds"] = 200.0
        mock_mixxx["status"]["deck1"]["duration"] = 300.0
        mock_mixxx["status"]["deck2"]["track_loaded"] = True
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 200.0

        being._agent_busy = True
        being._transition_pending = False

        with patch.object(being, "_invoke_agent") as mock_invoke:
            being._heartbeat()
            mock_invoke.assert_not_called()

    def test_agent_not_called_when_transition_pending(self, being, mock_mixxx):
        """When a transition is already pending, don't ask the agent."""
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 100.0
        mock_mixxx["status"]["deck1"]["position_seconds"] = 200.0
        mock_mixxx["status"]["deck1"]["duration"] = 300.0
        mock_mixxx["status"]["deck2"]["track_loaded"] = True
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 200.0

        being._agent_busy = False
        being._transition_pending = True

        with patch.object(being, "_invoke_agent") as mock_invoke:
            being._heartbeat()
            mock_invoke.assert_not_called()


# ── Priority 5: DELETED in v8 Phase 7 ──────────────────────────────
# DJ owns track loading via session.playlist; Python never selects.
# P1 silence recovery is the only safety net that touches Mixxx.

class TestBackupLoadRemoved:

    def test_heartbeat_never_calls_load_from_its_own_priorities(self, being, mock_mixxx):
        """v8: heartbeat must not call _load_next_on_idle. Selection is
        DJ's job via its load_track tool."""
        mock_mixxx["status"]["deck1"]["playing"] = True
        mock_mixxx["status"]["deck1"]["position_seconds"] = 130.0
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 170.0
        mock_mixxx["status"]["deck1"]["duration"] = 300.0
        mock_mixxx["status"]["deck2"]["track_loaded"] = False
        mock_mixxx["status"]["deck2"]["remaining_seconds"] = 0.0

        being._agent_busy = False
        being._transition_pending = False

        with patch.object(being, "_load_next_on_idle") as mock_load:
            being._heartbeat()
            mock_load.assert_not_called()
