"""NS-009 — planner candidate menu (agent/planner_menu.py) tests.

The menu is the code pre-filter applied to the v8 full-library dump when
llm.planner_candidate_cap > 0. Pure function — tested against a synthetic
library with a known current bpm/key.
"""

from agent.planner_menu import select_planner_candidates


def _track(path, mood="melodic-techno", genre="Melodic Techno", bpm=124.0,
           key="8A", title=None):
    return {
        "path": path,
        "title": title or path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
        "genre": genre,
        "bpm": bpm,
        "key_camelot": key,
        "mood": mood,
    }


def _synthetic_library():
    lib = []
    # 30 mood-matched melodic techno tracks, bpm 118..147
    for i in range(30):
        lib.append(_track(f"Melodic Techno/mt-{i:02d}.mp3", bpm=118 + i))
    # 30 afro tracks (wrong mood), bpm right on target
    for i in range(30):
        lib.append(_track(f"Afro/af-{i:02d}.mp3", mood="afro", genre="Afro",
                          bpm=124))
    return lib


def test_cap_respected_and_mood_matched_first():
    lib = _synthetic_library()
    out = select_planner_candidates(
        lib, cap=10, mood="melodic-techno", current_bpm=124.0,
        current_key_camelot="8A",
    )
    assert len(out) == 10
    # 30 mood matches exist, so every slot goes to melodic techno.
    assert all(t["mood"] == "melodic-techno" for t in out)


def test_bpm_window_respected():
    lib = _synthetic_library()
    out = select_planner_candidates(
        lib, cap=5, mood="melodic-techno", current_bpm=124.0,
        current_key_camelot=None,
    )
    # The 5 closest-BPM mood matches win: 122..126 (dist ≤ 2); nothing
    # from the far end of the range (118 or 140+) sneaks in.
    bpms = sorted(t["bpm"] for t in out)
    assert bpms == [122, 123, 124, 125, 126]


def test_camelot_bonus_breaks_bpm_ties():
    lib = [
        _track("Melodic Techno/off-key.mp3", bpm=124, key="3B"),   # incompatible
        _track("Melodic Techno/in-key.mp3", bpm=126, key="8A"),    # same key
        _track("Melodic Techno/adjacent.mp3", bpm=126, key="9A"),  # +1 on wheel
        _track("Melodic Techno/swap.mp3", bpm=126, key="8B"),      # A↔B swap
    ]
    out = select_planner_candidates(
        lib, cap=3, mood="melodic-techno", current_bpm=124.0,
        current_key_camelot="8A",
    )
    # Compatible keys at 2-BPM distance outrank the incompatible exact-BPM
    # track (key bonus is worth ~6 BPM).
    paths = {t["path"] for t in out}
    assert "Melodic Techno/off-key.mp3" not in paths
    assert len(out) == 3


def test_genre_contains_fallback_when_mood_slug_absent():
    # Rows whose mood column is empty still match via genre-string contains.
    lib = [_track(f"Melodic Techno/x-{i}.mp3", mood="") for i in range(5)]
    lib += [_track(f"Afro/y-{i}.mp3", mood="", genre="Afro") for i in range(5)]
    out = select_planner_candidates(
        lib, cap=5, mood="melodic-techno", current_bpm=124.0,
    )
    assert all(t["genre"] == "Melodic Techno" for t in out)


def test_played_titles_excluded():
    lib = _synthetic_library()
    played = ["mt-05", "mt-06"]
    out = select_planner_candidates(
        lib, cap=40, mood="melodic-techno", current_bpm=124.0,
        played_titles=played,
    )
    titles = {t["title"] for t in out}
    assert "mt-05" not in titles and "mt-06" not in titles


def test_mood_profile_midpoint_used_when_no_current_bpm():
    lib = _synthetic_library()
    out = select_planner_candidates(
        lib, cap=3, mood="melodic-techno", current_bpm=None,
        mood_profile={"bpm_range": [120, 128]},  # midpoint 124
    )
    bpms = sorted(t["bpm"] for t in out)
    assert bpms == [123, 124, 125]


def test_cap_zero_is_off_switch():
    lib = _synthetic_library()
    out = select_planner_candidates(lib, cap=0, mood="melodic-techno")
    assert out == lib  # untouched — today's cloud behavior


def test_deterministic_and_stable():
    lib = _synthetic_library()
    kw = dict(cap=12, mood="melodic-techno", current_bpm=124.0,
              current_key_camelot="8A")
    a = select_planner_candidates(lib, **kw)
    b = select_planner_candidates(list(lib), **kw)
    assert [t["path"] for t in a] == [t["path"] for t in b]
