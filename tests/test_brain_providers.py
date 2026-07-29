"""Pluggable slow-loop brains: resolution, CLI arg construction, fallbacks."""
import subprocess
import unittest
import unittest.mock as mock

from agent import brain_providers as bp


class _LLM:
    def __init__(self, brains=None):
        self.brains = brains


class TestBrainFor(unittest.TestCase):
    def test_absent_config_means_litellm_path(self):
        self.assertIsNone(bp.brain_for(_LLM(None), "library_manager"))
        self.assertIsNone(bp.brain_for(_LLM({}), "library_manager"))

    def test_explicit_litellm_means_litellm_path(self):
        cfg = _LLM({"library_manager": {"provider": "litellm"}})
        self.assertIsNone(bp.brain_for(cfg, "library_manager"))

    def test_unknown_provider_degrades_loudly(self):
        cfg = _LLM({"library_manager": {"provider": "gpt-magic"}})
        self.assertIsNone(bp.brain_for(cfg, "library_manager"))

    def test_missing_binary_degrades(self):
        cfg = _LLM({"library_manager": {"provider": "claude-cli"}})
        with mock.patch.object(bp.shutil, "which", return_value=None):
            self.assertIsNone(bp.brain_for(cfg, "library_manager"))

    def test_resolves_claude(self):
        cfg = _LLM({"library_manager": {"provider": "claude-cli", "model": "sonnet"}})
        with mock.patch.object(bp.shutil, "which", return_value="/bin/claude"):
            brain = bp.brain_for(cfg, "library_manager")
        self.assertEqual(brain["provider"], "claude-cli")
        self.assertEqual(brain["model"], "sonnet")

    def test_other_roles_unaffected(self):
        cfg = _LLM({"library_manager": {"provider": "claude-cli"}})
        with mock.patch.object(bp.shutil, "which", return_value="/bin/claude"):
            self.assertIsNone(bp.brain_for(cfg, "planner"))


class TestRunCliBrain(unittest.TestCase):
    def _ok(self, stdout="hello"):
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def test_claude_args(self):
        brain = {"provider": "claude-cli", "model": "sonnet"}
        with mock.patch.object(bp.subprocess, "run",
                               return_value=self._ok()) as r:
            out = bp.run_cli_brain(brain, "curate")
        cmd = r.call_args[0][0]
        self.assertEqual(cmd[:3], ["claude", "-p", "curate"])
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("WebSearch,WebFetch", cmd)
        self.assertEqual(out, "hello")

    def test_claude_no_model_flag_when_empty(self):
        brain = {"provider": "claude-cli"}
        with mock.patch.object(bp.subprocess, "run",
                               return_value=self._ok()) as r:
            bp.run_cli_brain(brain, "x")
        self.assertNotIn("--model", r.call_args[0][0])

    def test_nonzero_exit_raises(self):
        brain = {"provider": "claude-cli"}
        fail = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch.object(bp.subprocess, "run", return_value=fail):
            with self.assertRaises(RuntimeError):
                bp.run_cli_brain(brain, "x")

    def test_empty_output_raises(self):
        brain = {"provider": "claude-cli"}
        with mock.patch.object(bp.subprocess, "run",
                               return_value=self._ok(stdout="  ")):
            with self.assertRaises(RuntimeError):
                bp.run_cli_brain(brain, "x")

    def test_codex_reads_last_message_file(self):
        brain = {"provider": "codex-cli", "model": "gpt-5"}

        def fake_run(cmd, **kw):
            i = cmd.index("--output-last-message")
            with open(cmd[i + 1], "w") as f:
                f.write("codex says hi")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(bp.subprocess, "run", side_effect=fake_run) as r:
            out = bp.run_cli_brain(brain, "curate")
        cmd = r.call_args[0][0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("read-only", cmd)
        self.assertIn("-m", cmd)
        self.assertEqual(out, "codex says hi")


if __name__ == "__main__":
    unittest.main()
