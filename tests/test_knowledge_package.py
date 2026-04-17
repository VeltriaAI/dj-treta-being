"""Tests for agent.knowledge package scaffolding (v8 Phase 3.5).

All queries return safe defaults when config.knowledge.enabled is False
(v8 default) — empty lists / None — and update KnowledgeHealth with an
explicit reason. No silent empty strings.
"""

from unittest.mock import patch

import pytest

from agent.knowledge import (
    CanonicalRef,
    KnowledgeClient,
    KnowledgeHealth,
    discover_candidates,
    similar_to,
    genre_context,
    gap_analysis,
    merge_candidate,
    local_row_to_canonical,
)
from agent.knowledge.models import KnowledgeTrack


@pytest.fixture(autouse=True)
def reset_client():
    """Fresh singleton per test to isolate health state."""
    KnowledgeClient.reset()
    yield
    KnowledgeClient.reset()


def _disabled_config():
    """Patch load_config to report knowledge.enabled=False."""
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.knowledge.enabled = False
    cfg.knowledge.data_dir = "/tmp/fake"
    return cfg


class TestDisabled:

    def test_discover_candidates_returns_empty_when_disabled(self):
        with patch("agent.config.load_config", return_value=_disabled_config()):
            result = discover_candidates(mood_profile={"canonical_slug": "bollyafro"})
        assert result == []
        health = KnowledgeClient.instance().health
        assert health.available is False
        assert health.last_error  # non-empty, explicit reason

    def test_similar_to_returns_empty_when_disabled(self):
        seed = CanonicalRef("Artist", "Song")
        with patch("agent.config.load_config", return_value=_disabled_config()):
            result = similar_to(seed)
        assert result == []

    def test_genre_context_returns_none_when_disabled(self):
        with patch("agent.config.load_config", return_value=_disabled_config()):
            result = genre_context("melodic-techno")
        assert result is None

    def test_gap_analysis_returns_degenerate_report_when_disabled(self):
        with patch("agent.config.load_config", return_value=_disabled_config()):
            report = gap_analysis({"canonical_slug": "psy"}, [])
        assert report is not None
        assert report.dataset_count == 0
        assert report.saturation == 1.0  # "fully saturated" signal: nothing to fetch


class TestCanonicalRef:

    def test_case_insensitive_key(self):
        a = CanonicalRef("Stephan Jolk", "Morgen", "Original Mix", None)
        b = CanonicalRef("STEPHAN JOLK", "morgen", "ORIGINAL MIX", None)
        assert a.key() == b.key()

    def test_null_remixer_distinguished(self):
        a = CanonicalRef("X", "Y", None, None)
        b = CanonicalRef("X", "Y", None, "Someone")
        assert a.key() != b.key()


class TestMerge:

    def test_local_row_canonical_roundtrip(self):
        row = {
            "canonical_artist": "Artist",
            "canonical_song": "Song",
            "canonical_version": "Original Mix",
            "remixer": None,
        }
        ref = local_row_to_canonical(row)
        assert ref is not None
        assert ref.artist == "Artist"
        assert ref.version == "Original Mix"
        assert ref.remixer is None

    def test_local_row_missing_canonical_returns_none(self):
        assert local_row_to_canonical({"title": "x"}) is None

    def test_merge_prefers_local_bpm_over_kb_hint(self):
        kb = KnowledgeTrack(
            canonical=CanonicalRef("A", "B"),
            bpm_hint=125,
            subgenre="Afro House",
            label="Defected",
            year=2024,
        )
        local = {"bpm": 124.5, "key_camelot": "8A", "genre": "afro-house",
                 "path": "/music/x.mp3", "energy_peak": 7}
        merged = merge_candidate(kb, local)
        assert merged.bpm == 124.5        # local measured wins
        assert merged.subgenre == "Afro House"  # KB-only field kept
        assert merged.label == "Defected"
        assert merged.downloaded is True
        assert merged.path == "/music/x.mp3"

    def test_merge_without_local(self):
        kb = KnowledgeTrack(
            canonical=CanonicalRef("A", "B"),
            bpm_hint=125,
            search_query="A - B official",
        )
        merged = merge_candidate(kb, local_row=None)
        assert merged.bpm == 125
        assert merged.downloaded is False
        assert merged.path == ""
        assert merged.search_query == "A - B official"


class TestClientHealth:

    def test_ensure_loaded_disabled_marks_offline(self):
        client = KnowledgeClient.instance()
        ok = client.ensure_loaded(enabled=False)
        assert ok is False
        assert client.health.available is False
        assert client.health.last_error == "disabled"

    def test_repeated_disabled_calls_stable(self):
        """Calling ensure_loaded repeatedly should not spam error logs."""
        client = KnowledgeClient.instance()
        client.ensure_loaded(enabled=False)
        first_err = client.health.last_error
        client.ensure_loaded(enabled=False)
        client.ensure_loaded(enabled=False)
        assert client.health.last_error == first_err
