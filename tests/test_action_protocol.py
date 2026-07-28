"""07-28 guards: DJ filler suppression + menu strict-genre mode."""
import unittest

from agent.heartbeat import _is_dj_filler
from agent.planner_menu import select_planner_candidates


def _lib(n_mood=50, n_off=50):
    lib = [{"title": f"m{i}", "path": f"/m{i}.aiff", "mood": "melodic-techno",
            "genre": "Melodic Techno", "bpm": 150, "key_camelot": "8A"}
           for i in range(n_mood)]
    lib += [{"title": f"x{i}", "path": f"/x{i}.aiff", "mood": "big-room",
             "genre": "Big Room", "bpm": 126, "key_camelot": "8A"}
            for i in range(n_off)]
    return lib


class TestDjFiller(unittest.TestCase):
    FILLER = [
        "What would you like me to do? I can search for tracks...",
        "How can I help you right now?",
        "What track are you interested in? Please provide the file path.",
        "I am ready to assist with your DJ mixing tasks.",
        "Please let me know your next command!",
        "What is the track path or name you want me to look for?",
    ]
    REAL = [
        "holding — deck 2 mid-track, next check at 4:10",
        "Scheduled crossfade to deck 1 at 405s, 60s blend.",
        "Transitioning from ARTBAT to Boris Brejcha to maintain the melodic flow.",
        "",
    ]

    def test_filler_detected(self):
        for t in self.FILLER:
            self.assertTrue(_is_dj_filler(t), t)

    def test_real_decisions_survive(self):
        for t in self.REAL:
            self.assertFalse(_is_dj_filler(t), t)


class TestMenuStrictGenre(unittest.TestCase):
    def test_strict_when_pool_fills_cap(self):
        # Off-genre tracks sit CLOSER to current BPM — under the old ranking
        # they'd ride in on proximity. Strict mode must exclude all of them.
        menu = select_planner_candidates(
            _lib(), cap=40, mood="melodic techno", current_bpm=126)
        self.assertEqual(len(menu), 40)
        self.assertTrue(all(r["genre"] == "Melodic Techno" for r in menu))

    def test_backfill_when_pool_scarce(self):
        # Only 10 true matches → BPM backfill is allowed to fill the cap.
        lib = _lib(n_mood=10)
        menu = select_planner_candidates(
            lib, cap=40, mood="melodic techno", current_bpm=126)
        self.assertEqual(len(menu), 40)
        self.assertEqual(
            sum(1 for r in menu if r["genre"] == "Melodic Techno"), 10)


if __name__ == "__main__":
    unittest.main()
