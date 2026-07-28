"""Phrase-lock: pure-math tests for phrase_clock (no live Mixxx needed)."""
import unittest
import unittest.mock as mock

import agent.tools.transitions as tr


def _patch(fake_status):
    return (
        mock.patch.object(tr, "_mixxx_get", return_value=fake_status),
        mock.patch.object(tr, "_mixxx_failed", return_value=None),
    )


class TestPhraseClock(unittest.TestCase):
    def test_on_phrase_boundary(self):
        # 128 BPM, 60s in → 128 beats elapsed → exactly on a phrase boundary;
        # the NEXT boundary is a full 32 beats (15.0s) away.
        p1, p2 = _patch({"deck1": {"bpm": 128, "position_seconds": 60.0,
                                   "remaining_seconds": 200}})
        with p1, p2:
            pc = tr.phrase_clock(1)
        self.assertAlmostEqual(pc["phrase_beat"], 0.0, places=6)
        self.assertAlmostEqual(pc["seconds_to_next_phrase"], 15.0, places=6)

    def test_mid_phrase(self):
        # 124 BPM, 100s in → 206.67 beats → phrase_beat 14.67 → 8.39s to next.
        p1, p2 = _patch({"deck2": {"bpm": 124, "position_seconds": 100.0,
                                   "remaining_seconds": 150}})
        with p1, p2:
            pc = tr.phrase_clock(2)
        self.assertAlmostEqual(pc["phrase_beat"], (100 * 124 / 60) % 32, places=6)
        self.assertTrue(8.3 < pc["seconds_to_next_phrase"] < 8.5)

    def test_no_tempo_returns_none(self):
        p1, p2 = _patch({"deck1": {"bpm": 0, "position_seconds": 10}})
        with p1, p2:
            self.assertIsNone(tr.phrase_clock(1))

    def test_unreachable_returns_none(self):
        with mock.patch.object(tr, "_mixxx_get", return_value=None), \
             mock.patch.object(tr, "_mixxx_failed", return_value="down"):
            self.assertIsNone(tr.phrase_clock(1))

    def test_wait_degrades_when_boundary_too_far(self):
        # Boundary 15s away but max_wait 10s → must degrade to bar wait.
        p1, p2 = _patch({"deck1": {"bpm": 128, "position_seconds": 60.0,
                                   "remaining_seconds": 200}})
        with p1, p2, mock.patch.object(tr, "_wait_bar_boundary",
                                       return_value=True) as bar:
            self.assertTrue(tr._wait_true_phrase_boundary(1, max_wait_s=10.0))
            bar.assert_called_once()

    def test_wait_degrades_when_reserve_would_overrun(self):
        # 8.4s to boundary, 60s blend reserved, only 50s remaining → bar wait.
        p1, p2 = _patch({"deck2": {"bpm": 124, "position_seconds": 100.0,
                                   "remaining_seconds": 50}})
        with p1, p2, mock.patch.object(tr, "_wait_bar_boundary",
                                       return_value=True) as bar:
            self.assertTrue(tr._wait_true_phrase_boundary(2, reserve_s=60.0))
            bar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
