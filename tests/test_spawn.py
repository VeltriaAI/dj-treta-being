"""Tests for spawn.py — subagent spawning."""

from unittest.mock import patch, MagicMock

import pytest


class TestToolSetValidation:

    def test_invalid_tool_set_rejected(self):
        from agent.tools.spawn import spawn_agent
        result = spawn_agent("test task", tool_set="hacking")
        assert "ERROR" in result
        assert "Invalid" in result

    def test_valid_tool_sets(self):
        from agent.tools.spawn import _TOOL_SETS
        assert "research" in _TOOL_SETS
        assert "analysis" in _TOOL_SETS
        assert "production" in _TOOL_SETS
        assert "introspection" in _TOOL_SETS

    def test_research_has_no_transitions(self):
        from agent.tools.spawn import _TOOL_SETS
        assert "do_transition" not in _TOOL_SETS["research"]
        assert "schedule_transition" not in _TOOL_SETS["research"]

    def test_tool_resolution(self):
        from agent.tools.spawn import _resolve_tools
        tools = _resolve_tools("research")
        assert len(tools) > 0
        # All should be FunctionTool instances
        for t in tools:
            assert hasattr(t, 'func')


class TestSpawnResult:

    def test_unknown_spawn_id(self):
        from agent.tools.spawn import get_spawn_result
        result = get_spawn_result("nonexistent")
        assert "No spawn found" in result
