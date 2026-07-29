"""Setlist mode — a prepared set overrides selection, she still mixes it."""
import json
import tempfile
import unittest
from pathlib import Path

from agent.playlist_schema import validate_playlist
from agent.setlist import (load_setlist, setlist_position, setlist_status,
                           setlist_to_playlist)


class _Set:
    """A throwaway set folder: 4 silent 'tracks' + m3u8 + cues.json."""

    def __enter__(self):
        self.d = Path(tempfile.mkdtemp())
        self.files = []
        for i in range(1, 5):
            f = self.d / f"{i:02d} - Track {i}.mp3"
            f.write_bytes(b"\0" * 16)
            self.files.append(f)
        (self.d / "playlist.m3u8").write_text(
            "#EXTM3U\n" + "\n".join(f"#EXTINF:200,T{i}\n{f.name}"
                                   for i, f in enumerate(self.files, 1)) + "\n")
        (self.d / "cues.json").write_text(json.dumps([
            {"pos": i, "file": f.name, "title": f"Track {i}",
             "phase": "PEAK" if i == 3 else "BUILD", "bpm": 128.0,
             "camelot": "8A", "energy": 7,
             "blend_s": 30 if i == 3 else 60, "blend_bars": 16 if i == 3 else 32,
             "cues": [{"name": "MIX IN", "seconds": 10.0}]}
            for i, f in enumerate(self.files, 1)]))
        return self

    def __exit__(self, *a):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)


class TestLoad(unittest.TestCase):
    def test_loads_m3u8_with_metadata(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"), name="Test Set")
        self.assertEqual(len(sl["tracks"]), 4)
        self.assertEqual(sl["name"], "Test Set")
        t = sl["tracks"][0]
        self.assertEqual(t["title"], "Track 1")
        self.assertEqual(t["bpm"], 128.0)
        self.assertEqual(t["key_camelot"], "8A")
        self.assertEqual(len(t["cues"]), 1)

    def test_peak_blend_carried_through(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"))
        self.assertEqual(sl["tracks"][2]["blend_s"], 30)  # PEAK, 16 bars
        self.assertEqual(sl["tracks"][1]["blend_s"], 60)  # BUILD, 32 bars

    def test_folder_source_and_missing_entries(self):
        with _Set() as s:
            sl = load_setlist(str(s.d))          # folder → finds the m3u8
            self.assertEqual(len(sl["tracks"]), 4)
            s.files[1].unlink()                   # a track vanishes
            sl2 = load_setlist(str(s.d / "playlist.m3u8"))
        self.assertEqual(len(sl2["tracks"]), 3)   # skipped, not fatal
        self.assertEqual(sl2["missing_count"], 1)

    def test_bad_source_raises_readable_error(self):
        with self.assertRaises(ValueError):
            load_setlist("/nope/does-not-exist.m3u8")


class TestQueue(unittest.TestCase):
    def test_emits_a_valid_playlist(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"), name="X")
            pl = setlist_to_playlist(sl, set())
            v = validate_playlist(pl)  # same validation the planner must pass
        self.assertEqual([t["rank"] for t in v["tracks"]], [1, 2, 3, 4])
        self.assertIn("SETLIST MODE", v["reasoning_summary"])

    def test_order_is_exactly_the_setlist(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"))
            pl = setlist_to_playlist(sl, set())
        self.assertEqual([t["path"] for t in pl["tracks"]],
                         [t["path"] for t in sl["tracks"]])

    def test_position_is_derived_from_played(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"))
            played = {sl["tracks"][0]["path"], sl["tracks"][1]["path"]}
            self.assertEqual(setlist_position(sl, played), 2)
            pl = setlist_to_playlist(sl, played)
        self.assertEqual(pl["tracks"][0]["path"], sl["tracks"][2]["path"])

    def test_out_of_order_play_self_corrects(self):
        # Manish loads track 3 by hand; position must jump past it, not desync.
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"))
            played = {t["path"] for t in sl["tracks"][:3]}
            self.assertEqual(setlist_position(sl, played), 3)

    def test_finished_returns_none_so_planner_resumes(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"))
            allp = {t["path"] for t in sl["tracks"]}
            self.assertIsNone(setlist_to_playlist(sl, allp))

    def test_loop_restarts_instead_of_ending(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"), loop=True)
            allp = {t["path"] for t in sl["tracks"]}
            pl = setlist_to_playlist(sl, allp)
        self.assertIsNotNone(pl)
        self.assertEqual(pl["tracks"][0]["path"], sl["tracks"][0]["path"])

    def test_transition_hint_carries_the_planned_blend(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"))
            played = {t["path"] for t in sl["tracks"][:2]}
            pl = setlist_to_playlist(sl, played)
        self.assertEqual(pl["tracks"][0]["transition_hint"]["duration"], 30)

    def test_status_string(self):
        with _Set() as s:
            sl = load_setlist(str(s.d / "playlist.m3u8"), name="X")
            self.assertIn("0/4", setlist_status(sl, set()))
        self.assertIn("No setlist", setlist_status(None, set()))


if __name__ == "__main__":
    unittest.main()
