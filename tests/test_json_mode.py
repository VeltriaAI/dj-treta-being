"""NS-001 — JSON-mode parsing helper + response_format schema constants."""

import json

import pytest

from agent.json_extract import PARSE_STATS, parse_llm_json
from agent.canonicalize import (
    CANONICAL_TRACK_RESPONSE_FORMAT,
    GENRE_MATCH_RESPONSE_FORMAT,
)
from agent.mood_resolver import MOOD_PROFILE_RESPONSE_FORMAT
from agent.playlist_schema import PLAYLIST_V1_RESPONSE_FORMAT


@pytest.fixture(autouse=True)
def _clear_stats():
    PARSE_STATS.clear()
    yield
    PARSE_STATS.clear()


def test_clean_json_parses_direct():
    data = parse_llm_json('{"match": true, "reason": "fits"}', "genre_gate")
    assert data == {"match": True, "reason": "fits"}
    assert PARSE_STATS["genre_gate"] == {"direct": 1, "fallback": 0}


def test_fenced_json_uses_fallback():
    text = 'Here you go:\n```json\n{"canonical_slug": "bollyafro"}\n```'
    data = parse_llm_json(text, "mood_resolver")
    assert data == {"canonical_slug": "bollyafro"}
    assert PARSE_STATS["mood_resolver"] == {"direct": 0, "fallback": 1}


def test_thinking_preamble_uses_fallback():
    text = (
        "Thinking Process: the user wants melodic techno {not json here}\n"
        'Final answer: {"tracks": [], "mood_snapshot": "melodic-techno", '
        '"reasoning_summary": "thin library"}'
    )
    data = parse_llm_json(text, "planner")
    assert data["mood_snapshot"] == "melodic-techno"
    assert PARSE_STATS["planner"] == {"direct": 0, "fallback": 1}


def test_parse_stats_increment_per_site():
    parse_llm_json('{"a": 1}', "planner")
    parse_llm_json('{"a": 2}', "planner")
    parse_llm_json('```json\n{"a": 3}\n```', "planner")
    parse_llm_json('{"b": 1}', "canonicalize")
    assert PARSE_STATS["planner"] == {"direct": 2, "fallback": 1}
    assert PARSE_STATS["canonicalize"] == {"direct": 1, "fallback": 0}


def test_unparseable_raises():
    with pytest.raises(Exception):
        parse_llm_json("no json anywhere", "planner")


@pytest.mark.parametrize(
    "fmt",
    [
        MOOD_PROFILE_RESPONSE_FORMAT,
        CANONICAL_TRACK_RESPONSE_FORMAT,
        GENRE_MATCH_RESPONSE_FORMAT,
        PLAYLIST_V1_RESPONSE_FORMAT,
    ],
)
def test_schema_constants_well_formed(fmt):
    # json.dumps round-trips
    assert json.loads(json.dumps(fmt)) == fmt
    assert fmt["type"] == "json_schema"
    js = fmt["json_schema"]
    assert isinstance(js["name"], str) and js["name"]
    schema = js["schema"]
    assert schema["type"] == "object"
    # Non-strict on purpose — no additionalProperties:false anywhere.
    assert "additionalProperties" not in json.dumps(fmt)


def test_playlist_schema_deliberately_loose():
    schema = PLAYLIST_V1_RESPONSE_FORMAT["json_schema"]["schema"]
    assert "planned_at" not in schema.get("required", [])
    track_required = schema["properties"]["tracks"]["items"]["required"]
    for optional in ("transition_hint", "bpm", "energy", "downloaded",
                     "video_id", "mbid"):
        assert optional not in track_required
