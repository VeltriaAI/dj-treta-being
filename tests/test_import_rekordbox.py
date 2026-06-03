"""Unit tests for E3 Rekordbox import + E3/E5 section/loop/arrangement model.

No live DB / Mixxx needed — parse_collection() is a pure function, and the
arrangement layer operates on plain dicts.
"""
import os
import tempfile

import pytest

from ops.import_rekordbox import parse_collection
from agent.audio_analysis import (
    map_section_to_block, block_color, annotate_blocks,
)
from agent.arrangement import (
    ArrangementIntent, ArrangementPlan,
    loop_cues_from_track, phantom_grid_cue, ARRANGEMENT_TECHNIQUES,
)


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <COLLECTION Entries="2">
    <TRACK TrackID="101" Name="Strobe" Artist="deadmau5" Genre="Progressive"
           Location="file://localhost/Users/dj/Music/Strobe.mp3"
           AverageBpm="128.00" Tonality="Bbm" TotalTime="600">
      <TEMPO Inizio="0.050" Bpm="128.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="Intro" Type="0" Start="0.05" Num="0"
                     Red="40" Green="226" Blue="20"/>
      <POSITION_MARK Name="Vocal Loop" Type="4" Start="120.0" End="150.0" Num="2"/>
      <POSITION_MARK Name="Outro" Type="0" Start="560.0" Num="3"
                     Red="230" Green="40" Blue="40"/>
    </TRACK>
    <TRACK TrackID="102" Name="Ghosts n Stuff" Artist="deadmau5" Genre="Electro"
           Location="file://localhost/Users/dj/Music/Ghosts%20n%20Stuff.mp3"
           AverageBpm="128.00" Tonality="Fm" TotalTime="240">
      <TEMPO Inizio="0.010" Bpm="128.00"/>
    </TRACK>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="1">
      <NODE Type="0" Name="Sets" Count="1">
        <NODE Type="1" Name="Warmup" KeyType="0" Entries="2">
          <TRACK Key="101"/>
          <TRACK Key="102"/>
        </NODE>
      </NODE>
    </NODE>
  </PLAYLISTS>
</DJ_PLAYLISTS>
"""


@pytest.fixture
def xml_file():
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w") as f:
        f.write(SAMPLE_XML)
    yield path
    os.unlink(path)


def test_parse_tracks_basic(xml_file):
    parsed = parse_collection(xml_file)
    tracks = {t["title"]: t for t in parsed["tracks"]}
    assert "Strobe" in tracks
    strobe = tracks["Strobe"]
    assert strobe["bpm"] == 128.0
    assert strobe["artist"] == "deadmau5"
    # file:// URL → real path, with %20 decoded for the second track.
    assert strobe["path"] == "/Users/dj/Music/Strobe.mp3"
    assert tracks["Ghosts n Stuff"]["path"] == "/Users/dj/Music/Ghosts n Stuff.mp3"


def test_key_to_camelot(xml_file):
    strobe = next(t for t in parse_collection(xml_file)["tracks"] if t["title"] == "Strobe")
    # Bbm → 3A on the Camelot wheel.
    assert strobe["key_camelot"] == "3A"


def test_beatgrid(xml_file):
    strobe = next(t for t in parse_collection(xml_file)["tracks"] if t["title"] == "Strobe")
    assert strobe["beatgrid_anchor_seconds"] == 0.05
    # bar = 4 beats * (60/128) = 1.875s
    assert round(strobe["beatgrid_bar_seconds"], 3) == 1.875


def test_cues_and_loop(xml_file):
    strobe = next(t for t in parse_collection(xml_file)["tracks"] if t["title"] == "Strobe")
    cues = strobe["cues"]
    assert len(cues) == 3
    assert strobe["has_loop_cue"] is True
    loop = next(c for c in cues if c["is_loop"])
    # 150-120 = 30s; at 128bpm beat=0.46875s → ~64 beats.
    assert loop["loop_length_beats"] == pytest.approx(64.0, abs=0.5)
    # First cue color preserved from RGB (40,226,20).
    intro = next(c for c in cues if c["name"] == "Intro")
    assert intro["color"] == "#28E214"


def test_section_mapping(xml_file):
    strobe = next(t for t in parse_collection(xml_file)["tracks"] if t["title"] == "Strobe")
    cues = sorted(strobe["cues"], key=lambda c: c["start_seconds"])
    assert cues[0]["section"] == "mix_in"      # first cue
    assert any(c["section"] == "mix_loop" for c in cues)  # loop
    assert cues[-1]["section"] == "mix_out"    # last/outro cue
    # mix points resolved for schedule_transition.
    assert strobe["mix_in_seconds"] == 0.1     # round(0.05,1)
    assert strobe["mix_out_seconds"] == 560.0


def test_block_vocabulary():
    assert map_section_to_block("intro") == "START"
    assert map_section_to_block("drop") == "DROP"
    assert map_section_to_block("breakdown") == "BREAK"
    assert map_section_to_block("intro", is_loop=True) == "LOOP"
    assert block_color("START").startswith("#")
    annotated = annotate_blocks(
        [{"section": "intro", "energy": 3}, {"section": "drop", "energy": 8}],
        loop_section_indices={1},
    )
    assert annotated[0]["block"] == "START"
    assert annotated[1]["block"] == "LOOP"   # forced loop
    assert "color" in annotated[0]


def test_playlists(xml_file):
    parsed = parse_collection(xml_file)
    pls = {p["path"]: p for p in parsed["playlists"]}
    assert "Sets" in pls and pls["Sets"]["is_folder"]
    assert "Sets/Warmup" in pls
    warmup = pls["Sets/Warmup"]
    assert not warmup["is_folder"]
    assert warmup["track_rb_ids"] == ["101", "102"]


def test_timeline_has_blocks(xml_file):
    strobe = next(t for t in parse_collection(xml_file)["tracks"] if t["title"] == "Strobe")
    assert strobe["timeline"]
    for seg in strobe["timeline"]:
        assert "block" in seg and "color" in seg
        assert seg["block"] in ("START", "BREAK", "LOOP", "DROP")


# ── E5 arrangement model ────────────────────────────────────────────

def test_arrangement_intent_bars_to_seconds():
    intent = ArrangementIntent(step=0, goal="build", bars=16, technique="riser")
    # 16 bars @ 128bpm = 16 * (60/128) * 4 = 30s
    assert intent.estimated_seconds(128) == pytest.approx(30.0)
    assert intent.technique in ARRANGEMENT_TECHNIQUES
    assert "goal" in intent.to_dict()


def test_arrangement_plan_serializes():
    plan = ArrangementPlan(goal="build", intents=[
        ArrangementIntent(step=0, goal="build", track_path="techno/a.mp3", bars=16),
        ArrangementIntent(step=1, goal="peak", track_path="techno/b.mp3", bars=8),
    ], horizon_bars=24)
    d = plan.to_dict()
    assert d["goal"] == "build"
    assert len(d["intents"]) == 2
    assert d["intents"][0]["track_path"] == "techno/a.mp3"


def test_loop_cues_extraction():
    import json
    meta = {"cue_points": json.dumps([
        {"is_loop": True, "start_seconds": 120.0, "loop_length_beats": 16, "color": "#9B22E6"},
        {"is_loop": False, "start_seconds": 0.0},
    ])}
    loops = loop_cues_from_track(meta)
    assert len(loops) == 1
    assert loops[0]["length_beats"] == 16
    # Robust to missing/garbled.
    assert loop_cues_from_track({}) == []
    assert loop_cues_from_track({"cue_points": "not json"}) == []


def test_phantom_grid_cue():
    cue = phantom_grid_cue({"beatgrid_anchor_seconds": 0.05})
    assert cue["is_phantom"] and cue["start_seconds"] == 0.05
    # Defaults to 0.0 when no anchor.
    assert phantom_grid_cue({})["start_seconds"] == 0.0
