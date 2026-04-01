"""Shared pytest fixtures for DJ Treta / DJClaw test suite.

Provides:
- mock_mixxx: intercept all httpx calls to Mixxx HTTP API
- test_db: temporary SQLite database with seed data
- config: test Config with sensible defaults
- being: DJTretaBeing wired to mocked Mixxx + temp DB, no LLM
- test_mp3: generate a short sine-wave MP3 for audio tests
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure agent package is importable from repo root
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Mixxx Mock Data ──────────────────────────────────────────────────

DECK1_PLAYING = {
    "playing": True,
    "track_loaded": True,
    "bpm": 128.0,
    "file_bpm": 128.0,
    "key": 13,
    "position_seconds": 90.0,
    "remaining_seconds": 210.0,
    "duration": 300.0,
    "title": "Test Track A",
}

DECK2_IDLE_LOADED = {
    "playing": False,
    "track_loaded": True,
    "bpm": 126.0,
    "file_bpm": 126.0,
    "key": 15,
    "position_seconds": 0.0,
    "remaining_seconds": 280.0,
    "duration": 280.0,
    "title": "Test Track B",
}

DECK2_IDLE_EMPTY = {
    "playing": False,
    "track_loaded": False,
    "bpm": 0,
    "file_bpm": 0,
    "key": 0,
    "position_seconds": 0.0,
    "remaining_seconds": 0.0,
    "duration": 0.0,
    "title": "",
}

DEFAULT_STATUS = {
    "deck1": DECK1_PLAYING,
    "deck2": DECK2_IDLE_LOADED,
    "crossfader": 0.0,
}

TRACK_INFO_A = {
    "title": "Test Track A",
    "artist": "DJ Test",
    "file_path": "/music/techno/Test Track A.mp3",
    "bpm": 128.0,
    "key": 13,
    "duration": 300.0,
}

TRACK_INFO_B = {
    "title": "Test Track B",
    "artist": "DJ Test",
    "file_path": "/music/techno/Test Track B.mp3",
    "bpm": 126.0,
    "key": 15,
    "duration": 280.0,
}


class FakeMixxxResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _route_mixxx(method: str, url: str, **kwargs) -> FakeMixxxResponse:
    """Route httpx GET/POST to fake Mixxx responses."""
    if "/api/status" in url:
        return FakeMixxxResponse(DEFAULT_STATUS)
    if "/api/deck/1/track_info" in url:
        return FakeMixxxResponse(TRACK_INFO_A)
    if "/api/deck/2/track_info" in url:
        return FakeMixxxResponse(TRACK_INFO_B)
    if "/api/live" in url:
        return FakeMixxxResponse({"vu_left": 0.6, "vu_right": 0.7, "beat_distance": 0.3})
    # POST endpoints (load, play, pause, crossfade, eq, volume, control, etc.)
    if method == "POST":
        return FakeMixxxResponse({"ok": True})
    # Fallback
    return FakeMixxxResponse({"ok": True})


@pytest.fixture
def mock_mixxx(monkeypatch):
    """Intercept all httpx.get / httpx.post calls destined for Mixxx.

    Returns a dict you can mutate to change what Mixxx 'reports':
        mock_mixxx["status"]["deck1"]["remaining_seconds"] = 10
    """
    state = {"status": json.loads(json.dumps(DEFAULT_STATUS))}  # deep copy

    def fake_get(url, **kwargs):
        if "/api/status" in url:
            return FakeMixxxResponse(state["status"])
        return _route_mixxx("GET", url, **kwargs)

    def fake_post(url, **kwargs):
        return _route_mixxx("POST", url, **kwargs)

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", fake_get)
    monkeypatch.setattr(_httpx, "post", fake_post)
    return state


# ── Test Database ────────────────────────────────────────────────────

SEED_TRACKS = [
    {"path": "/music/techno/Track_A.mp3", "title": "Track A", "artist": "Artist 1",
     "genre": "techno", "bpm": 128.0, "key_musical": "Cm", "key_camelot": "5A",
     "energy_peak": 7, "duration_seconds": 300.0, "analyzed_at": time.time()},
    {"path": "/music/techno/Track_B.mp3", "title": "Track B", "artist": "Artist 2",
     "genre": "techno", "bpm": 130.0, "key_musical": "Dm", "key_camelot": "7A",
     "energy_peak": 8, "duration_seconds": 280.0, "analyzed_at": time.time()},
    {"path": "/music/deep/Track_C.mp3", "title": "Track C", "artist": "Artist 3",
     "genre": "deep", "bpm": 122.0, "key_musical": "Am", "key_camelot": "8A",
     "energy_peak": 5, "duration_seconds": 350.0, "analyzed_at": time.time()},
    {"path": "/music/melodic/Track_D.mp3", "title": "Track D", "artist": "Artist 4",
     "genre": "melodic", "bpm": 124.0, "key_musical": "Em", "key_camelot": "9A",
     "energy_peak": 6, "duration_seconds": 320.0, "analyzed_at": time.time()},
    {"path": "/music/ambient/Track_E.mp3", "title": "Track E", "artist": "Artist 5",
     "genre": "ambient", "bpm": 100.0, "key_musical": "F", "key_camelot": "7B",
     "energy_peak": 3, "duration_seconds": 400.0, "analyzed_at": time.time()},
]

SEED_SET = {
    "id": "set-20260401-120000",
    "set_number": 1,
    "title": "Test Session",
    "started_at": time.time() - 3600,
    "mood": "techno",
    "genre": "techno",
    "target_duration": 120,
    "status": "live",
}


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary SQLite DB matching DJ Treta schema, seeded with test data.

    Patches agent.db.DB_PATH so all db functions use the temp DB.
    Returns the Path to the DB file.
    """
    db_path = tmp_path / "djtreta_test.db"

    import agent.db as db_mod
    original_path = db_mod.DB_PATH

    # Patch DB_PATH at module level
    db_mod.DB_PATH = db_path
    db_mod.init_db()

    # Seed tracks
    db = db_mod.get_db()
    for t in SEED_TRACKS:
        cols = list(t.keys())
        vals = list(t.values())
        placeholders = ",".join("?" * len(cols))
        db.execute(f"INSERT INTO tracks ({','.join(cols)}) VALUES ({placeholders})", vals)

    # Seed set
    s = SEED_SET
    db.execute(
        "INSERT INTO sets (id, set_number, title, started_at, mood, genre, "
        "target_duration_minutes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (s["id"], s["set_number"], s["title"], s["started_at"],
         s["mood"], s["genre"], s["target_duration"], s["status"]),
    )
    db.commit()
    db.close()

    yield db_path

    # Restore
    db_mod.DB_PATH = original_path


# ── Config ───────────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path):
    """A test Config with sensible defaults and a temp music dir."""
    from agent.config import Config, MixxxConfig, LLMConfig, LibraryConfig

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    # Create a genre subdir with a dummy file for scan_library
    techno = music_dir / "techno"
    techno.mkdir()
    (techno / "dummy.mp3").write_bytes(b"\x00" * 100)

    return Config(
        mixxx=MixxxConfig(url="http://localhost:7778", timeout=2, auto_start=False),
        llm=LLMConfig(model="test/fake", api_base="http://localhost:4000", api_key="test-key"),
        library=LibraryConfig(music_dir=str(music_dir)),
    )


# ── Being ────────────────────────────────────────────────────────────

@pytest.fixture
def being(config, test_db, mock_mixxx):
    """A DJTretaBeing with mocked Mixxx, real temp DB, no LLM.

    The Being is constructed but NOT started (start() launches threads).
    Use it for testing heartbeat logic, command handling, state writes.
    """
    # Prevent ADK imports from failing in test
    with patch("agent.main.InMemorySessionService"), \
         patch("agent.main.create_agents", return_value=(MagicMock(), MagicMock())):
        from agent.main import DJTretaBeing
        b = DJTretaBeing(config)
        b.agent = MagicMock()
        b.planner_agent = MagicMock()
        b._running = True
        b.current_set = {
            "id": "set-test-001",
            "set_number": 1,
            "title": "Test Set",
            "started_at": time.time() - 600,
            "mood": "techno",
            "genre": "techno",
            "target_duration": 120,
            "energy_arc": [],
        }
        b._tracks_since_plan = 0
        yield b
        b._running = False


# ── Test MP3 Generation ──────────────────────────────────────────────

@pytest.fixture
def test_mp3(tmp_path):
    """Generate a short MP3 with a beat-like pattern for audio analysis tests.

    Creates a 10-second track with periodic clicks (simulating kick drums)
    at ~120 BPM plus a tonal element. This gives librosa enough signal
    to detect BPM, key, and energy.
    Requires pydub + ffmpeg.
    """
    try:
        from pydub import AudioSegment
        from pydub.generators import Sine
    except ImportError:
        pytest.skip("pydub not installed — skipping audio test")

    # Build a rhythmic pattern: short click every 500ms = 120 BPM
    click = Sine(80).to_audio_segment(duration=30)   # 30ms low thump
    silence = AudioSegment.silent(duration=470)       # 470ms gap
    beat = click + silence  # 500ms total = 120 BPM

    # 10 seconds = 20 beats
    track = beat * 20

    # Layer a tonal element for key detection
    tone = Sine(440).to_audio_segment(duration=10000) - 12  # quieter
    track = track.overlay(tone)

    mp3_path = tmp_path / "test_beat.mp3"
    track.export(str(mp3_path), format="mp3")
    return mp3_path
