"""Tests for v6.0 directive system — Being → Agent communication."""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


DIRECTIVE_FILE = Path("/tmp/dj-treta-directives.json")
MOOD_CHANGE_FILE = Path("/tmp/dj-treta-mood-change.json")


class TestDirectiveTools:

    def setup_method(self):
        DIRECTIVE_FILE.unlink(missing_ok=True)
        MOOD_CHANGE_FILE.unlink(missing_ok=True)

    def teardown_method(self):
        DIRECTIVE_FILE.unlink(missing_ok=True)
        MOOD_CHANGE_FILE.unlink(missing_ok=True)

    def test_set_dj_directive_writes_file(self):
        from agent.tools.directives import set_dj_directive

        result = set_dj_directive("hard_cut when bhojpuri loads")
        assert "hard_cut" in result
        assert DIRECTIVE_FILE.exists()
        data = json.loads(DIRECTIVE_FILE.read_text())
        assert data["dj"]["instruction"] == "hard_cut when bhojpuri loads"
        assert "set_at" in data["dj"]

    def test_set_planner_directive_writes_file(self):
        from agent.tools.directives import set_planner_directive

        result = set_planner_directive("Download 3 bhojpuri tracks")
        assert "bhojpuri" in result
        data = json.loads(DIRECTIVE_FILE.read_text())
        assert data["planner"]["instruction"] == "Download 3 bhojpuri tracks"

    def test_set_mood_writes_temp_file(self):
        from agent.tools.directives import set_mood

        result = set_mood("bhojpuri")
        assert "bhojpuri" in result
        assert MOOD_CHANGE_FILE.exists()
        data = json.loads(MOOD_CHANGE_FILE.read_text())
        assert data["mood"] == "bhojpuri"

    def test_directives_accumulate(self):
        """Setting DJ then planner should keep both."""
        from agent.tools.directives import set_dj_directive, set_planner_directive

        set_dj_directive("use bass_swap")
        set_planner_directive("find dark tracks")

        data = json.loads(DIRECTIVE_FILE.read_text())
        assert data["dj"]["instruction"] == "use bass_swap"
        assert data["planner"]["instruction"] == "find dark tracks"

    def test_clear_directives(self):
        from agent.tools.directives import set_dj_directive, clear_directives

        set_dj_directive("something")
        assert DIRECTIVE_FILE.exists()

        clear_directives()
        assert not DIRECTIVE_FILE.exists()

    def test_get_directives_empty(self):
        from agent.tools.directives import get_directives

        result = get_directives()
        assert "No active" in result


class TestDirectivePickup:
    """Test that Being picks up directives from temp files."""

    def setup_method(self):
        DIRECTIVE_FILE.unlink(missing_ok=True)
        MOOD_CHANGE_FILE.unlink(missing_ok=True)

    def teardown_method(self):
        DIRECTIVE_FILE.unlink(missing_ok=True)
        MOOD_CHANGE_FILE.unlink(missing_ok=True)

    def test_pickup_mood_change(self, being):
        """Being should pick up mood change from set_mood tool."""
        MOOD_CHANGE_FILE.write_text(json.dumps({"mood": "bhojpuri", "set_at": time.time()}))

        being._pick_up_directives()

        assert being.mood == "bhojpuri"
        assert being.current_set["mood"] == "bhojpuri"
        assert not MOOD_CHANGE_FILE.exists()  # consumed

    def test_pickup_dj_directive(self, being):
        """Being should pick up DJ directive from file."""
        DIRECTIVE_FILE.write_text(json.dumps({
            "dj": {"instruction": "use hard_cut", "set_at": time.time()}
        }))

        being._pick_up_directives()

        assert being.dj_directive == "use hard_cut"

    def test_pickup_planner_directive(self, being):
        """Being should pick up planner directive from file."""
        DIRECTIVE_FILE.write_text(json.dumps({
            "planner": {"instruction": "find ambient tracks", "set_at": time.time()}
        }))

        being._pick_up_directives()

        assert being.planner_directive == "find ambient tracks"

    def test_mood_triggers_replan(self, being):
        """Mood change should force planner replan."""
        being._tracks_since_plan = 0
        MOOD_CHANGE_FILE.write_text(json.dumps({"mood": "psytrance", "set_at": time.time()}))

        being._pick_up_directives()

        assert being._tracks_since_plan == being.config.planner.replan_every_n_tracks


class TestReadonlyTalk:
    """Test readonly talk mode for live web listeners."""

    def test_talk_readonly_flag_passed(self, being):
        """readonly flag should be passed through to _being_talk."""
        with patch("agent.commands.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()

            being._handle_command("talk", {"message": "what's playing?", "readonly": True}, "cmd-ro-1")

            call_kwargs = mock_thread.call_args
            assert call_kwargs[1]["args"] == ("what's playing?", "cmd-ro-1", True)

    def test_talk_default_not_readonly(self, being):
        """Default talk should be readonly=False."""
        with patch("agent.commands.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()

            being._handle_command("talk", {"message": "go darker"}, "cmd-rw-1")

            call_kwargs = mock_thread.call_args
            assert call_kwargs[1]["args"] == ("go darker", "cmd-rw-1", False)


class TestBeingAgentCreation:
    """Test that create_agents returns 3 agents."""

    def test_create_agents_returns_three(self):
        """create_agents should return (being_agent, dj_agent, planner_agent)."""
        from agent.config import Config, MixxxConfig, LLMConfig, LibraryConfig
        from unittest.mock import patch
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            music_dir = Path(tmpdir) / "music"
            music_dir.mkdir()
            config = Config(
                mixxx=MixxxConfig(url="http://localhost:7778"),
                llm=LLMConfig(model="test/fake", api_base="http://localhost:4000", api_key="test"),
                library=LibraryConfig(music_dir=str(music_dir)),
            )

            with patch("agent.agents.LlmAgent") as mock_agent, \
                 patch("agent.agents.LiteLlm"):
                mock_agent.return_value = MagicMock()
                from agent.agents import create_agents
                result = create_agents(config)

                assert len(result) == 3
                # being_agent, dj_agent, planner_agent
                being, dj, planner = result
