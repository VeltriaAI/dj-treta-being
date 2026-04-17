"""Tests for v8 directive system — Being → Agent communication via Session.

In v6/v7, directives were written to /tmp/dj-treta-directives.json and
polled by CommandsMixin._pick_up_directives. In v8, directives live in
Session (single source of truth), written directly by the tool functions.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent.session_state import Session, register_session


@pytest.fixture
def registered_session(tmp_path):
    """Fresh Session registered as the module-level singleton."""
    path = tmp_path / "session.json"
    session = Session(path)
    register_session(session)
    yield session
    session.close()
    # Clean up singleton
    import agent.session_state as ss
    ss._session_instance = None


class TestDirectiveTools:
    """Tools write to Session, not to /tmp files."""

    def test_set_dj_directive_writes_to_session(self, registered_session):
        from agent.tools.directives import set_dj_directive

        result = set_dj_directive("hard_cut when bhojpuri loads")
        assert "hard_cut" in result
        assert registered_session.dj_directive == "hard_cut when bhojpuri loads"

    def test_set_planner_directive_writes_to_session(self, registered_session):
        from agent.tools.directives import set_planner_directive

        result = set_planner_directive("Download 3 bhojpuri tracks")
        assert "bhojpuri" in result
        assert registered_session.planner_directive == "Download 3 bhojpuri tracks"

    def test_set_mood_writes_to_session(self, registered_session):
        from agent.tools.directives import set_mood

        result = set_mood("bhojpuri")
        assert "bhojpuri" in result
        assert registered_session.mood == "bhojpuri"

    def test_directives_coexist(self, registered_session):
        """Setting DJ then planner should leave both set."""
        from agent.tools.directives import set_dj_directive, set_planner_directive

        set_dj_directive("use bass_swap")
        set_planner_directive("find dark tracks")

        assert registered_session.dj_directive == "use bass_swap"
        assert registered_session.planner_directive == "find dark tracks"

    def test_clear_directives(self, registered_session):
        from agent.tools.directives import set_dj_directive, clear_directives

        set_dj_directive("something")
        assert registered_session.dj_directive == "something"

        clear_directives()
        assert registered_session.dj_directive == ""
        assert registered_session.planner_directive == ""

    def test_get_directives_empty(self, registered_session):
        from agent.tools.directives import get_directives

        result = get_directives()
        assert "No active" in result

    def test_get_directives_populated(self, registered_session):
        from agent.tools.directives import set_dj_directive, get_directives

        set_dj_directive("smooth transitions only")
        result = get_directives()
        assert "smooth" in result

    def test_tools_without_registered_session(self):
        """Tools should degrade gracefully when session not registered."""
        import agent.session_state as ss
        ss._session_instance = None

        from agent.tools.directives import set_dj_directive, set_mood
        assert "not available" in set_dj_directive("x").lower()
        assert "not available" in set_mood("x").lower()


class TestMoodCallback:
    """Session's mood-change callback is how 'pickup directive' behavior
    now works — registered in main.py at startup. Tests the callback path."""

    def test_callback_fires_on_mood_change(self, registered_session):
        """Registering a callback on `mood` fires when set_mood is called."""
        from agent.tools.directives import set_mood

        events = []
        registered_session.register_callback(
            "mood", lambda name, old, new: events.append((old, new))
        )

        set_mood("psytrance")
        set_mood("psytrance")  # no-op — same value
        set_mood("deep-house")

        assert len(events) == 2
        assert events[0] == ("", "psytrance")
        assert events[1] == ("psytrance", "deep-house")


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
    """Test that create_agents returns 4 agents (v8 Phase 5 — Library is a
    root peer, not a DJ sub-agent)."""

    def test_create_agents_returns_four(self):
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

                assert len(result) == 4
                # being_agent, dj_agent, planner_agent, library_agent
                being, dj, planner, library = result
