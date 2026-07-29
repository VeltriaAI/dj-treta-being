"""Regression tests for the 2026-07-29 review findings.

Each test pins a defect the adversarial review confirmed, so it cannot
silently come back.
"""
import subprocess
import unittest
import unittest.mock as mock

import agent.brain_providers as bp
import agent.flight_recorder as fr
import agent.tools.transitions as tr


class TestPhraseWaitRealSleepPath(unittest.TestCase):
    """Gap #2: every existing test mocked _wait_bar_boundary, so the actual
    sleep path never ran. Exercise it with a patched clock."""

    def _status(self, bpm=128.0, pos=60.0, rem=300.0):
        return {"deck1": {"bpm": bpm, "position_seconds": pos,
                          "remaining_seconds": rem}}

    @staticmethod
    def _fake_clock():
        """Stand-in for transitions._time_mod.

        NEVER patch the real time.sleep here — _time_mod IS the time module,
        and daemon threads left running by other test modules would spin hot
        on a mocked sleep, perturbing unrelated tests. Swap the alias instead.
        """
        import time as _real
        clock = mock.Mock()
        clock.monotonic = _real.monotonic
        return clock

    def test_sleeps_until_phrase_boundary_then_micro_snaps(self):
        # 128 BPM, exactly on a phrase → next boundary 15.0s away.
        # Implementation sleeps (wait - 0.5) then micro-polls the beat.
        clock = self._fake_clock()
        with mock.patch.object(tr, "_mixxx_get", return_value=self._status()), \
             mock.patch.object(tr, "_mixxx_failed", return_value=None), \
             mock.patch.object(tr, "_time_mod", clock), \
             mock.patch.object(tr, "_wait_phrase_boundary", return_value=True) as micro:
            self.assertTrue(tr._wait_true_phrase_boundary(1, max_wait_s=20.0))
        clock.sleep.assert_called_once()
        self.assertAlmostEqual(clock.sleep.call_args[0][0], 14.5, places=3)
        micro.assert_called_once()

    def test_reserve_guard_boundary_is_exclusive(self):
        # wait=15.0, reserve=60 → needs rem >= 15+60+2 = 77.0 to proceed.
        for rem, should_degrade in ((76.9, True), (77.1, False)):
            with mock.patch.object(tr, "_mixxx_get",
                                   return_value=self._status(rem=rem)), \
                 mock.patch.object(tr, "_mixxx_failed", return_value=None), \
                 mock.patch.object(tr, "_time_mod", self._fake_clock()), \
                 mock.patch.object(tr, "_wait_phrase_boundary", return_value=True), \
                 mock.patch.object(tr, "_wait_bar_boundary", return_value=True) as bar:
                tr._wait_true_phrase_boundary(1, reserve_s=60.0)
            self.assertEqual(bar.called, should_degrade, f"rem={rem}")


class TestIncomingDeckSilencedBeforePlay(unittest.TestCase):
    """Finding 1.2: the incoming fader must be zeroed BEFORE /api/play, or the
    phrase hold (up to ~20s) plays both tracks at full volume."""

    def test_source_order(self):
        import inspect
        src = inspect.getsource(tr.do_transition)
        play = src.index('_mixxx_post("/api/play", {"deck": to_deck})')
        vol = src.index('_mixxx_post("/api/volume", {"deck": to_deck, "level": 0.0})')
        eq = src.index('_mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})')
        self.assertLess(vol, play, "incoming volume must be zeroed before play")
        self.assertLess(eq, play, "incoming bass must be cut before play")


class TestFlightRecorderBounded(unittest.TestCase):
    """Finding 1.1: unbounded transcript reached 174MB in 19h and could
    ENOSPC the runtime dir (which also holds deck state + the DB)."""

    def test_rotates_at_cap(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "llm-transcript.jsonl"
            p.write_bytes(b"x" * 200)
            with mock.patch.object(fr, "_path", return_value=p), \
                 mock.patch.object(fr, "_MAX_BYTES", 100):
                fr._write({"ts": 1, "model": "m"})
            self.assertTrue(p.with_suffix(".1.jsonl").exists(), "backup kept")
            self.assertLess(p.stat().st_size, 200, "active file restarted")

    def test_message_bodies_truncated(self):
        with mock.patch.object(fr, "_MAX_MSG_CHARS", 10):
            out = fr._truncate_messages([{"role": "user", "content": "x" * 500}])
        self.assertLess(len(out[0]["content"]), 60)
        self.assertEqual(out[0]["content_len"], 500)

    def test_write_never_raises(self):
        from pathlib import Path
        with mock.patch.object(fr, "_path",
                               return_value=Path("/nonexistent-dir/x.jsonl")):
            fr._write({"ts": 1})  # must not raise


class TestCliBrainSandboxing(unittest.TestCase):
    """Finding 1.3: --allowedTools is an allow-list and restricts nothing; the
    review empirically wrote a file into the live repo through this call."""

    def _ok(self):
        return subprocess.CompletedProcess([], 0, stdout="ok", stderr="")

    def test_claude_denies_write_tools_and_leaves_the_repo(self):
        with mock.patch.object(bp.subprocess, "run", return_value=self._ok()) as r:
            bp.run_cli_brain({"provider": "claude-cli"}, "curate")
        cmd, kw = r.call_args[0][0], r.call_args[1]
        self.assertIn("--disallowedTools", cmd)
        denied = cmd[cmd.index("--disallowedTools") + 1]
        for tool in ("Bash", "Write", "Edit"):
            self.assertIn(tool, denied)
        self.assertIn("--permission-mode", cmd)
        self.assertNotIn("dj-treta", kw.get("cwd", ""), "must not run in the repo")
        self.assertEqual(kw.get("stdin"), subprocess.DEVNULL)

    def test_codex_read_only_and_leaves_the_repo(self):
        def fake_run(cmd, **kw):
            with open(cmd[cmd.index("--output-last-message") + 1], "w") as f:
                f.write("ok")
            self.assertIn("read-only", cmd)
            self.assertNotIn("dj-treta", kw.get("cwd", ""))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(bp.subprocess, "run", side_effect=fake_run):
            self.assertEqual(bp.run_cli_brain({"provider": "codex-cli"}, "x"), "ok")


if __name__ == "__main__":
    unittest.main()
