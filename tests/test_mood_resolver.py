"""Tests for agent.mood_resolver — LLM mood → MoodProfile resolution.

Covers the 8 scenarios from the v8 Phase 2 plan:
  1. Canonical mood parses cleanly (BollyAffro → bollyafro)
  2. Case/whitespace variants normalize to same canonical
  3. LLM-unavailable path returns low-confidence fallback
  4. Bad JSON from LLM falls back cleanly
  5. Empty string → "unknown" low-confidence profile
  6. SQLite cache hit on repeat call (single LLM invocation for N requests)
  7. MoodProfile round-trips through to_dict / from_dict
  8. clear_cache() wipes the table
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from agent.mood_resolver import (
    MoodProfile,
    resolve_mood,
    clear_cache,
    _fallback_profile,
    RESOLVER_VERSION,
)


def _mock_llm_response(payload: dict):
    """Build a litellm.completion() stand-in returning `payload` as JSON."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps(payload)
    return resp


class TestCanonicalResolution:

    def test_bollyaffro_resolves_to_bollyafro(self, test_db):
        payload = {
            "canonical_slug": "bollyafro",
            "bpm_range": [115, 125],
            "energy_range": [6, 8],
            "vibe_keywords": ["punjabi", "afro", "vocal", "danceable"],
            "confidence": 0.95,
        }
        with patch("litellm.completion", return_value=_mock_llm_response(payload)):
            profile = resolve_mood("BollyAffro")

        assert profile.canonical_slug == "bollyafro"
        assert profile.bpm_range == (115, 125)
        assert profile.energy_range == (6, 8)
        assert "punjabi" in profile.vibe_keywords
        assert profile.confidence == 0.95
        assert profile.resolver_version == RESOLVER_VERSION


class TestCaseVariants:

    def test_typo_and_caps_both_cache_by_lowercase(self, test_db):
        """Raw input 'BollyAffro' and 'bollyaffro' should share a cache key."""
        payload = {
            "canonical_slug": "bollyafro",
            "bpm_range": [115, 125],
            "energy_range": [6, 8],
            "vibe_keywords": ["afro"],
            "confidence": 0.95,
        }
        mock = MagicMock(return_value=_mock_llm_response(payload))
        with patch("litellm.completion", mock):
            resolve_mood("BollyAffro")
            resolve_mood("bollyaffro")  # same cache key after lower()
            resolve_mood("BOLLYAFFRO")

        # Only one LLM call despite three resolve_mood invocations
        assert mock.call_count == 1


class TestLLMUnavailableFallback:

    def test_network_error_returns_fallback(self, test_db):
        def boom(*a, **kw):
            raise ConnectionError("LiteLLM unreachable")

        with patch("litellm.completion", side_effect=boom):
            profile = resolve_mood("psytrance")

        assert profile.confidence == 0.0
        assert profile.canonical_slug == "psytrance"
        # BPM/energy defaulted
        assert profile.bpm_range == (120, 128)
        assert profile.energy_range == (5, 8)


class TestBadJsonFallback:

    def test_invalid_json_returns_fallback(self, test_db):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "this is not json"

        with patch("litellm.completion", return_value=resp):
            profile = resolve_mood("deep-house")

        assert profile.confidence == 0.0
        assert profile.canonical_slug == "deep-house"

    def test_partial_json_missing_fields_uses_defaults(self, test_db):
        """LLM returning a valid JSON but missing some fields should fall
        back per-field, not blow up entirely."""
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = json.dumps({
            "canonical_slug": "minimal-techno",
            # missing bpm_range, energy_range, etc
        })

        with patch("litellm.completion", return_value=resp):
            profile = resolve_mood("minimal techno")

        # Defensive parsing — either the fallback or best-effort values.
        # canonical_slug honored, numeric ranges present and sane.
        assert profile.canonical_slug == "minimal-techno"
        assert len(profile.bpm_range) == 2
        assert len(profile.energy_range) == 2


class TestEmptyInput:

    def test_empty_string_returns_unknown(self, test_db):
        profile = resolve_mood("")
        assert profile.canonical_slug == "unknown"
        assert profile.confidence == 0.0

    def test_whitespace_only_returns_unknown(self, test_db):
        profile = resolve_mood("   ")
        assert profile.canonical_slug == "unknown"


class TestSQLiteCache:

    def test_repeat_call_hits_cache(self, test_db):
        """Second resolve_mood for the same raw should make zero LLM calls."""
        payload = {
            "canonical_slug": "melodic-techno",
            "bpm_range": [120, 125],
            "energy_range": [6, 9],
            "vibe_keywords": ["atmospheric", "driving"],
            "confidence": 0.97,
        }
        mock = MagicMock(return_value=_mock_llm_response(payload))
        with patch("litellm.completion", mock):
            p1 = resolve_mood("melodic techno")
            p2 = resolve_mood("melodic techno")
            p3 = resolve_mood("Melodic Techno")  # cache key is lowercased

        assert mock.call_count == 1
        assert p1.canonical_slug == p2.canonical_slug == p3.canonical_slug == "melodic-techno"

    def test_clear_cache_forces_reresolve(self, test_db):
        payload = {
            "canonical_slug": "psytrance",
            "bpm_range": [138, 142],
            "energy_range": [8, 10],
            "vibe_keywords": ["driving"],
            "confidence": 0.98,
        }
        mock = MagicMock(return_value=_mock_llm_response(payload))
        with patch("litellm.completion", mock):
            resolve_mood("psytrance")
            assert mock.call_count == 1

            clear_cache()

            resolve_mood("psytrance")
            assert mock.call_count == 2

    def test_fallback_is_also_cached(self, test_db):
        """LLM-unavailable fallback is cached so a down LLM doesn't cause
        retry storms across many mood reads."""
        def boom(*a, **kw):
            raise ConnectionError("offline")

        mock = MagicMock(side_effect=boom)
        with patch("litellm.completion", mock):
            resolve_mood("afro-house")
            resolve_mood("afro-house")
            resolve_mood("afro-house")

        assert mock.call_count == 1  # fallback stored in cache


class TestMoodProfileRoundtrip:

    def test_to_dict_from_dict_identity(self):
        original = MoodProfile(
            raw="BollyAfro",
            canonical_slug="bollyafro",
            bpm_range=(115, 125),
            energy_range=(6, 8),
            vibe_keywords=["punjabi", "afro"],
            confidence=0.95,
            resolved_at=1000.0,
        )
        data = original.to_dict()
        reloaded = MoodProfile.from_dict(data)

        assert reloaded.raw == original.raw
        assert reloaded.canonical_slug == original.canonical_slug
        assert reloaded.bpm_range == original.bpm_range
        assert reloaded.energy_range == original.energy_range
        assert reloaded.vibe_keywords == original.vibe_keywords
        assert reloaded.confidence == original.confidence
        assert reloaded.resolved_at == original.resolved_at

    def test_json_round_trip(self):
        """The to_dict output should survive a JSON round-trip without loss."""
        original = MoodProfile(
            raw="melodic techno",
            canonical_slug="melodic-techno",
            bpm_range=(120, 125),
            energy_range=(6, 9),
            vibe_keywords=["atmospheric"],
            confidence=0.97,
        )
        serialized = json.dumps(original.to_dict())
        reloaded = MoodProfile.from_dict(json.loads(serialized))

        # After JSON, bpm_range becomes a list then back to tuple via from_dict
        assert reloaded.bpm_range == (120, 125)
        assert reloaded.energy_range == (6, 9)


class TestFallbackProfile:

    def test_fallback_slug_normalization(self):
        assert _fallback_profile("Deep House").canonical_slug == "deep-house"
        assert _fallback_profile("Melodic_Techno").canonical_slug == "melodic-techno"
        assert _fallback_profile("  whitespace  ").canonical_slug == "whitespace"
        assert _fallback_profile("").canonical_slug == "unknown"


class TestDiscogsReferenceWiring:
    """Phase 3.6: MoodProfile includes discogs_primary_genre + discogs_subgenres
    populated by LLM against the checked-in Discogs reference JSON."""

    def test_llm_discogs_fields_validated_against_reference(self, test_db):
        payload = {
            "canonical_slug": "bollyafro",
            "bpm_range": [115, 125],
            "energy_range": [6, 8],
            "vibe_keywords": ["afro"],
            "discogs_primary_genre": "Electronic",
            "discogs_subgenres": ["Afro House", "Bollywood House"],
            "confidence": 0.95,
        }
        with patch("litellm.completion", return_value=_mock_llm_response(payload)):
            profile = resolve_mood("BollyAfro")

        assert profile.discogs_primary_genre == "Electronic"
        assert "Afro House" in profile.discogs_subgenres
        assert "Bollywood House" in profile.discogs_subgenres

    def test_invented_primary_genre_dropped(self, test_db):
        """If LLM invents a primary genre not in our reference, drop it."""
        payload = {
            "canonical_slug": "psytrance",
            "bpm_range": [138, 142],
            "energy_range": [8, 10],
            "vibe_keywords": [],
            "discogs_primary_genre": "Fake Invented Genre",  # not in reference
            "discogs_subgenres": ["Psy-Trance"],
            "confidence": 0.95,
        }
        with patch("litellm.completion", return_value=_mock_llm_response(payload)):
            profile = resolve_mood("psytrance")

        assert profile.discogs_primary_genre is None
        assert profile.discogs_subgenres == ["Psy-Trance"]

    def test_invented_subgenres_filtered(self, test_db):
        """LLM-invented subgenres not in our reference are filtered out."""
        payload = {
            "canonical_slug": "house",
            "bpm_range": [120, 128],
            "energy_range": [6, 8],
            "vibe_keywords": [],
            "discogs_primary_genre": "Electronic",
            "discogs_subgenres": ["House", "Totally Made Up Subgenre", "Deep House"],
            "confidence": 0.9,
        }
        with patch("litellm.completion", return_value=_mock_llm_response(payload)):
            profile = resolve_mood("house")

        assert "House" in profile.discogs_subgenres
        assert "Deep House" in profile.discogs_subgenres
        assert "Totally Made Up Subgenre" not in profile.discogs_subgenres

    def test_reference_file_parses(self):
        """The checked-in reference JSON must be loadable."""
        from agent.mood_resolver import _load_discogs_reference
        ref = _load_discogs_reference()
        assert "Electronic" in ref["primary_genres"]
        assert "Afro House" in ref["subgenres"]["Electronic"]
