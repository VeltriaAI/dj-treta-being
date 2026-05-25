"""E4 — unit tests for agent/state_sequence.py.

Tests:
- capture_state builds correct State from a Mixxx /api/status payload
- capture_state handles partial/missing payload gracefully
- apply_state calls the correct Mixxx HTTP endpoints with correct payloads
- StateSequence.record appends on first call and on meaningful change
- StateSequence.record skips when change is below thresholds
- StateSequence.replay iterates and calls apply_state per entry
- StateSequence.to_dict / from_dict round-trips correctly
- archive_set writes a valid JSONL line
- get_set_archive reads newest-first
- replay_set returns an error for an unknown set_id
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — fake Mixxx API response
# ---------------------------------------------------------------------------

FULL_STATUS = {
    "deck1": {
        "volume": 0.8,
        "eq": {"hi": 1.0, "mid": 0.9, "lo": 1.1},
        "filter": 0.5,
        "bpm": 128.0,
        "playing": True,
        "track_loaded": True,
        "title": "Track A",
    },
    "deck2": {
        "volume": 0.0,
        "eq": {"hi": 1.0, "mid": 1.0, "lo": 1.0},
        "filter": 0.5,
        "bpm": 0.0,
        "playing": False,
        "track_loaded": False,
    },
    "crossfader": 0.0,
    "bpm": 128.0,
}

CHANGED_STATUS = {
    "deck1": {
        "volume": 0.5,   # meaningful change (>0.02)
        "eq": {"hi": 0.5, "mid": 0.9, "lo": 1.1},  # hi changed >0.05
        "filter": 0.5,
    },
    "deck2": {
        "volume": 0.8,
        "eq": {"hi": 1.0, "mid": 1.0, "lo": 1.0},
        "filter": 0.5,
    },
    "crossfader": 0.5,
    "bpm": 128.0,
}

TINY_CHANGE_STATUS = {
    "deck1": {
        "volume": 0.801,   # 0.001 change — below 0.02 threshold
        "eq": {"hi": 1.001, "mid": 0.901, "lo": 1.101},  # below 0.05 EQ threshold
        "filter": 0.501,   # below 0.03 filter threshold
    },
    "deck2": {
        "volume": 0.001,
        "eq": {"hi": 1.0, "mid": 1.0, "lo": 1.0},
        "filter": 0.501,
    },
    "crossfader": 0.001,   # below 0.02 threshold
    "bpm": 128.0,
}


# ---------------------------------------------------------------------------
# capture_state
# ---------------------------------------------------------------------------

def test_capture_state_full_payload():
    from agent.state_sequence import capture_state
    state = capture_state(FULL_STATUS, label="test")
    assert state.deck1.volume == pytest.approx(0.8)
    assert state.deck1.eq.hi == pytest.approx(1.0)
    assert state.deck1.eq.mid == pytest.approx(0.9)
    assert state.deck1.eq.lo == pytest.approx(1.1)
    assert state.deck1.filter == pytest.approx(0.5)
    assert state.deck2.volume == pytest.approx(0.0)
    assert state.crossfader == pytest.approx(0.0)
    assert state.bpm == pytest.approx(128.0)
    assert state.label == "test"
    assert state.ts > 0


def test_capture_state_partial_payload():
    from agent.state_sequence import capture_state
    # Only deck1, no deck2, no crossfader/bpm
    partial = {"deck1": {"volume": 0.6}}
    state = capture_state(partial)
    assert state.deck1.volume == pytest.approx(0.6)
    assert state.deck1.eq.hi == pytest.approx(1.0)    # default
    assert state.deck2.volume == pytest.approx(1.0)   # default
    assert state.crossfader == pytest.approx(0.5)     # default neutral
    assert state.bpm == pytest.approx(0.0)


def test_capture_state_empty_payload():
    from agent.state_sequence import capture_state
    state = capture_state({})
    assert state.deck1.volume == pytest.approx(1.0)
    assert state.deck2.volume == pytest.approx(1.0)
    assert state.crossfader == pytest.approx(0.5)
    assert state.bpm == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# apply_state
# ---------------------------------------------------------------------------

def test_apply_state_calls_correct_endpoints():
    from agent.state_sequence import capture_state, apply_state

    posted_calls: list[tuple[str, dict]] = []

    def fake_dj_post(path: str, data: dict | None = None) -> dict:
        posted_calls.append((path, data or {}))
        return {"ok": True}

    state = capture_state(FULL_STATUS)

    with patch("agent.state_sequence._dj_post", side_effect=fake_dj_post):
        result = apply_state(state)

    paths = [c[0] for c in posted_calls]
    assert "/api/volume" in paths
    assert "/api/eq" in paths
    assert "/api/filter" in paths
    assert "/api/crossfade" in paths

    # Check deck 1 volume payload
    vol_calls = [c for c in posted_calls if c[0] == "/api/volume"]
    decks = {c[1]["deck"] for c in vol_calls}
    assert decks == {1, 2}

    assert result["errors"] == []
    assert len(result["applied"]) == 7   # 3 endpoints × 2 decks + crossfader


def test_apply_state_handles_mixxx_error():
    from agent.state_sequence import capture_state, apply_state

    def fake_dj_post(path: str, data: dict | None = None) -> dict:
        return {"error": "Mixxx offline"}

    state = capture_state(FULL_STATUS)
    with patch("agent.state_sequence._dj_post", side_effect=fake_dj_post):
        result = apply_state(state)

    assert len(result["errors"]) > 0
    assert "Mixxx offline" in result["errors"][0]


# ---------------------------------------------------------------------------
# StateSequence.record — change detection
# ---------------------------------------------------------------------------

def test_record_appends_first_state():
    from agent.state_sequence import StateSequence
    seq = StateSequence()
    state = seq.record(FULL_STATUS, label="first")
    assert state is not None
    assert len(seq) == 1


def test_record_appends_on_meaningful_change():
    from agent.state_sequence import StateSequence
    seq = StateSequence()
    seq.record(FULL_STATUS)
    state2 = seq.record(CHANGED_STATUS, label="changed")
    assert state2 is not None
    assert len(seq) == 2


def test_record_skips_tiny_change():
    from agent.state_sequence import StateSequence
    seq = StateSequence()
    seq.record(FULL_STATUS)
    skipped = seq.record(TINY_CHANGE_STATUS)
    assert skipped is None
    assert len(seq) == 1


def test_record_force_overrides_threshold():
    from agent.state_sequence import StateSequence
    seq = StateSequence()
    seq.record(FULL_STATUS)
    forced = seq.record(TINY_CHANGE_STATUS, force=True)
    assert forced is not None
    assert len(seq) == 2


# ---------------------------------------------------------------------------
# StateSequence.replay
# ---------------------------------------------------------------------------

def test_replay_calls_apply_state_for_each_entry():
    from agent.state_sequence import StateSequence

    apply_calls = []

    def fake_apply(state):
        apply_calls.append(state)
        return {"applied": [], "errors": []}

    seq = StateSequence()
    seq.record(FULL_STATUS, force=True)
    seq.record(CHANGED_STATUS, force=True)

    with patch("agent.state_sequence.apply_state", side_effect=fake_apply):
        with patch("agent.state_sequence.time") as mock_time:
            mock_time.time.return_value = time.time()
            mock_time.sleep = MagicMock()
            seq.replay()

    assert len(apply_calls) == 2


def test_replay_respects_stop_event():
    from agent.state_sequence import StateSequence

    apply_calls = []

    def fake_apply(state):
        apply_calls.append(state)
        return {"applied": [], "errors": []}

    seq = StateSequence()
    for _ in range(5):
        seq.record(FULL_STATUS, force=True)

    stop = threading.Event()
    stop.set()   # already stopped — should apply nothing

    with patch("agent.state_sequence.apply_state", side_effect=fake_apply):
        seq.replay(stop_event=stop)

    assert len(apply_calls) == 0


def test_replay_empty_sequence_is_noop():
    from agent.state_sequence import StateSequence
    seq = StateSequence()
    # Should not raise
    seq.replay()


# ---------------------------------------------------------------------------
# StateSequence round-trip serialization
# ---------------------------------------------------------------------------

def test_to_dict_from_dict_roundtrip():
    from agent.state_sequence import StateSequence
    seq = StateSequence()
    seq.record(FULL_STATUS, bar_duration=8, label="drop", force=True)
    seq.record(CHANGED_STATUS, bar_duration=4, label="breakdown", force=True)

    data = seq.to_dict()
    assert len(data) == 2
    assert data[0]["bar_duration"] == 8
    assert data[0]["state"]["label"] == "drop"

    seq2 = StateSequence.from_dict(data)
    assert len(seq2) == 2
    entry = seq2._entries[0]
    assert entry.bar_duration == 8
    assert entry.state.label == "drop"
    assert entry.state.deck1.volume == pytest.approx(0.8)
    assert entry.state.deck1.eq.mid == pytest.approx(0.9)
    assert entry.state.crossfader == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# archive_set / get_set_archive / replay_set
# ---------------------------------------------------------------------------

def test_archive_set_writes_jsonl(tmp_path):
    from agent.state_sequence import StateSequence, archive_set, _ARCHIVE_FILENAME
    import agent.state_sequence as ss_mod

    archive_file = tmp_path / _ARCHIVE_FILENAME

    with patch.object(ss_mod, "runtime_dir", return_value=tmp_path):
        seq = StateSequence()
        seq.record(FULL_STATUS, force=True)
        path = archive_set(
            set_id="set-test-001",
            started_at=1000.0,
            ended_at=2000.0,
            mood="melodic-techno",
            state_sequence=seq,
            tracks_played=[{"title": "Track A"}],
            recording_path="/tmp/recording.wav",
        )

    assert archive_file.exists()
    lines = archive_file.read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["set_id"] == "set-test-001"
    assert obj["mood"] == "melodic-techno"
    assert len(obj["state_sequence"]) == 1
    assert obj["recording_path"] == "/tmp/recording.wav"
    assert obj["tracks_played"] == [{"title": "Track A"}]


def test_get_set_archive_newest_first(tmp_path):
    from agent.state_sequence import StateSequence, archive_set, get_set_archive
    import agent.state_sequence as ss_mod

    with patch.object(ss_mod, "runtime_dir", return_value=tmp_path):
        for i in range(3):
            seq = StateSequence()
            archive_set(
                set_id=f"set-{i:03d}",
                started_at=float(i * 1000),
                ended_at=float(i * 1000 + 900),
                mood="techno",
                state_sequence=seq,
                tracks_played=[],
            )
        sets = get_set_archive(n=10)

    # Newest first → last written = "set-002" should be first
    assert sets[0]["set_id"] == "set-002"
    assert sets[-1]["set_id"] == "set-000"


def test_get_set_archive_empty(tmp_path):
    from agent.state_sequence import get_set_archive
    import agent.state_sequence as ss_mod

    with patch.object(ss_mod, "runtime_dir", return_value=tmp_path):
        result = get_set_archive(n=5)
    assert result == []


def test_replay_set_unknown_id(tmp_path):
    from agent.state_sequence import replay_set
    import agent.state_sequence as ss_mod

    with patch.object(ss_mod, "runtime_dir", return_value=tmp_path):
        result = replay_set("nonexistent-set-id")

    assert "error" in result
    assert "not found" in result["error"].lower() or "no archive" in result["error"].lower()


def test_replay_set_known_id(tmp_path):
    from agent.state_sequence import StateSequence, archive_set, replay_set
    import agent.state_sequence as ss_mod

    apply_calls = []

    def fake_apply(state):
        apply_calls.append(state)
        return {"applied": [], "errors": []}

    with patch.object(ss_mod, "runtime_dir", return_value=tmp_path):
        seq = StateSequence()
        seq.record(FULL_STATUS, force=True)
        seq.record(CHANGED_STATUS, force=True)
        archive_set(
            set_id="set-known",
            started_at=1000.0,
            ended_at=2000.0,
            mood="psy-trance",
            state_sequence=seq,
            tracks_played=[],
        )

        stop = threading.Event()
        with patch("agent.state_sequence.apply_state", side_effect=fake_apply):
            with patch("agent.state_sequence.time") as mock_time:
                mock_time.time.return_value = time.time()
                mock_time.sleep = MagicMock()
                result = replay_set("set-known", stop_event=stop)

    assert result["set_id"] == "set-known"
    assert result["states_replayed"] == 2
    assert len(apply_calls) == 2
