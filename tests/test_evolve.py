"""Tests for evolve.py — self-modification safety and workflow."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestScopeValidation:

    def test_rejects_env(self):
        from agent.tools.evolve import _validate_scope
        assert not _validate_scope(".env")

    def test_rejects_soul(self):
        from agent.tools.evolve import _validate_scope
        assert not _validate_scope(".beings/SOUL.md")

    def test_accepts_agent(self):
        from agent.tools.evolve import _validate_scope
        assert _validate_scope("agent/")

    def test_accepts_agent_tools(self):
        from agent.tools.evolve import _validate_scope
        assert _validate_scope("agent/tools/")

    def test_accepts_tests(self):
        from agent.tools.evolve import _validate_scope
        assert _validate_scope("tests/")

    def test_rejects_root(self):
        from agent.tools.evolve import _validate_scope
        assert not _validate_scope("/")

    def test_rejects_config_yaml(self):
        from agent.tools.evolve import _validate_scope
        assert not _validate_scope("config.yaml")


class TestEvolveWorkflow:

    def test_invalid_scope_returns_error(self):
        from agent.tools.evolve import evolve
        result = evolve("test", scope=".env")
        assert "ERROR" in result
        assert "not allowed" in result

    @patch("agent.tools.evolve.subprocess.run")
    def test_worktree_cleanup_on_failure(self, mock_run):
        """Worktree should be cleaned up even if Claude fails."""
        from agent.tools.evolve import evolve
        # First call (worktree add) succeeds, second (claude) fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git worktree add
            MagicMock(returncode=1, stderr="Claude error"),  # claude --print
            MagicMock(returncode=0),  # git worktree remove (cleanup)
        ]
        result = evolve("test goal", scope="agent/")
        assert "ERROR" in result
        # Verify cleanup was attempted (last call should be worktree remove)
        assert any("worktree" in str(c) and "remove" in str(c) for c in mock_run.call_args_list)


class TestProposeChange:

    def test_logs_proposal(self, test_db):
        from agent.tools.evolve import propose_change
        result = propose_change("Add better error handling", "agent/heartbeat.py")
        assert "Proposal logged" in result

        # Verify it's in DB
        from agent.db import get_db
        db = get_db()
        row = db.execute("SELECT * FROM evolution_log WHERE status='proposed'").fetchone()
        db.close()
        assert row is not None
        assert "error handling" in row["goal"]
