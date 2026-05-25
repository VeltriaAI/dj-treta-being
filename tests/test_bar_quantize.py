"""Unit tests for E1 bar-quantize pure math (no live Mixxx required).

Covers _beats_to_next_bar / _seconds_until_bar — the bar-boundary logic the
master-clock transition fire depends on.
"""

import math

import pytest

from agent.tools.transitions import (
    _beats_to_next_bar,
    _seconds_until_bar,
    _bars_to_seconds,
)


class TestBeatsToNextBar:
    def test_on_downbeat_is_full_bar_away(self):
        # Exactly on the downbeat (beat 0, distance 0) → next bar is 4 beats off,
        # never 0 (would be a no-op wait landing on the current downbeat).
        assert _beats_to_next_bar(0, 0.0) == 4.0

    def test_one_beat_in(self):
        # On beat 1 of the bar, exactly on the beat → 3 beats remain.
        assert _beats_to_next_bar(1, 0.0) == pytest.approx(3.0)

    def test_mid_beat(self):
        # Beat 2, halfway through the beat → 4 - (2 + 0.5) = 1.5 beats remain.
        assert _beats_to_next_bar(2, 0.5) == pytest.approx(1.5)

    def test_last_beat_almost_done(self):
        # Beat 3, 90% through → 4 - 3.9 = 0.1 beats remain.
        assert _beats_to_next_bar(3, 0.9) == pytest.approx(0.1)

    def test_beats_into_bar_wraps_modulo(self):
        # beat 4 == beat 0 of next bar.
        assert _beats_to_next_bar(4, 0.0) == _beats_to_next_bar(0, 0.0)

    def test_beat_distance_clamped(self):
        # Out-of-range beat_distance is clamped to [0,1].
        assert _beats_to_next_bar(0, 1.5) == _beats_to_next_bar(0, 1.0)
        assert _beats_to_next_bar(0, -0.5) == _beats_to_next_bar(0, 0.0)

    def test_result_always_in_valid_range(self):
        for bib in range(0, 4):
            for bd in (0.0, 0.25, 0.5, 0.75, 0.99):
                r = _beats_to_next_bar(bib, bd)
                assert 0.0 < r <= 4.0

    def test_three_four_time_signature(self):
        # beats_per_bar=3 (waltz) → on downbeat, a full bar = 3 beats.
        assert _beats_to_next_bar(0, 0.0, beats_per_bar=3) == 3.0
        assert _beats_to_next_bar(2, 0.5, beats_per_bar=3) == pytest.approx(0.5)


class TestSecondsUntilBar:
    def test_128bpm_full_bar(self):
        # On downbeat at 128 BPM: 4 beats * 60/128 = 1.875s.
        assert _seconds_until_bar(0, 0.0, 128.0) == pytest.approx(1.875)

    def test_matches_bars_to_seconds_for_one_bar(self):
        # A full bar from the downbeat equals _bars_to_seconds(1, bpm).
        for bpm in (90.0, 120.0, 174.0):
            assert _seconds_until_bar(0, 0.0, bpm) == pytest.approx(
                _bars_to_seconds(1, bpm)
            )

    def test_zero_bpm_falls_back_to_120(self):
        # Defensive: bpm<=0 uses 120 → 4 * 60/120 = 2.0s.
        assert _seconds_until_bar(0, 0.0, 0.0) == pytest.approx(2.0)
        assert _seconds_until_bar(0, 0.0, -5.0) == pytest.approx(2.0)

    def test_partial_bar(self):
        # Beat 2, halfway, 120 BPM → 1.5 beats * 0.5s = 0.75s.
        assert _seconds_until_bar(2, 0.5, 120.0) == pytest.approx(0.75)
