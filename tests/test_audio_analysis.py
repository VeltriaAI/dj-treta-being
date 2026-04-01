"""Tests for agent.audio_analysis — librosa-based signal processing.

Uses the test_mp3 fixture (pydub-generated sine wave) to test:
- BPM detection
- Key detection
- Section detection
- Mix point calculation

These tests require librosa and pydub. They are skipped if not installed.
"""

import pytest


def _has_librosa():
    try:
        import librosa
        return True
    except ImportError:
        return False


def _has_pydub():
    try:
        from pydub import AudioSegment
        from pydub.generators import Sine
        return True
    except ImportError:
        return False


needs_librosa = pytest.mark.skipif(not _has_librosa(), reason="librosa not installed")
needs_pydub = pytest.mark.skipif(not _has_pydub(), reason="pydub not installed")


@needs_librosa
@needs_pydub
class TestAnalyzeAudio:

    def test_analyze_audio_returns_bpm(self, test_mp3):
        """analyze_audio should return a numeric BPM value.
        Test fixture generates 120 BPM clicks — librosa may detect double/half time."""
        from agent.audio_analysis import analyze_audio

        result = analyze_audio(str(test_mp3))

        assert "bpm" in result
        assert isinstance(result["bpm"], float)
        # librosa might detect 120, 60 (half), or 240 (double) — any positive value is valid
        assert result["bpm"] > 0

    def test_analyze_audio_returns_key(self, test_mp3):
        """analyze_audio should return a musical key string."""
        from agent.audio_analysis import analyze_audio

        result = analyze_audio(str(test_mp3))

        assert "key" in result
        assert isinstance(result["key"], str)
        assert len(result["key"]) >= 1  # e.g. "Am", "C", "F#m"

    def test_analyze_audio_returns_sections(self, test_mp3):
        """analyze_audio should return a timeline with section dicts."""
        from agent.audio_analysis import analyze_audio

        result = analyze_audio(str(test_mp3))

        assert "timeline" in result
        assert isinstance(result["timeline"], list)
        assert len(result["timeline"]) >= 1

        # Each section should have start, end, section name, energy
        for section in result["timeline"]:
            assert "start" in section
            assert "end" in section
            assert "section" in section
            assert "energy" in section
            assert section["end"] >= section["start"]

    def test_analyze_audio_returns_mix_points(self, test_mp3):
        """analyze_audio should return mix_in_seconds and mix_out_seconds."""
        from agent.audio_analysis import analyze_audio

        result = analyze_audio(str(test_mp3))

        assert "mix_in_seconds" in result
        assert "mix_out_seconds" in result
        assert isinstance(result["mix_in_seconds"], (int, float))
        assert isinstance(result["mix_out_seconds"], (int, float))
        assert result["mix_out_seconds"] >= result["mix_in_seconds"]

    def test_analyze_audio_returns_energy(self, test_mp3):
        """analyze_audio should return energy_peak (1-10) and energy_curve."""
        from agent.audio_analysis import analyze_audio

        result = analyze_audio(str(test_mp3))

        assert "energy_peak" in result
        assert 1 <= result["energy_peak"] <= 10
        assert "energy_curve" in result
        assert isinstance(result["energy_curve"], list)

    def test_analyze_audio_returns_duration(self, test_mp3):
        """Duration should be approximately 10 seconds (test_mp3 is 10s)."""
        from agent.audio_analysis import analyze_audio

        result = analyze_audio(str(test_mp3))

        assert "duration_seconds" in result
        # 10-second sine wave — allow some tolerance
        assert 9.0 <= result["duration_seconds"] <= 11.0


@needs_librosa
@needs_pydub
class TestSectionDetection:

    def test_section_detection_has_intro_outro(self, tmp_path):
        """A longer track should have intro and outro sections detected.

        Creates a 60-second track with varying energy: quiet→loud→quiet
        to simulate intro/drop/outro structure.
        """
        try:
            from pydub import AudioSegment
            from pydub.generators import Sine, WhiteNoise
        except ImportError:
            pytest.skip("pydub not installed")

        # Build a 60-second track: 15s quiet intro, 30s loud drop, 15s quiet outro
        quiet = Sine(220).to_audio_segment(duration=15000) - 20  # -20dB
        loud = Sine(440).to_audio_segment(duration=30000)        # full volume
        outro = Sine(220).to_audio_segment(duration=15000) - 20  # -20dB
        track = quiet + loud + outro

        mp3_path = tmp_path / "structured_track.mp3"
        track.export(str(mp3_path), format="mp3")

        from agent.audio_analysis import analyze_audio
        result = analyze_audio(str(mp3_path))

        sections = result["timeline"]
        section_names = [s["section"] for s in sections]

        # Should detect at least intro and outro based on position heuristics
        assert "intro" in section_names, f"No intro detected in sections: {sections}"
        # Outro detection depends on position ratio > 0.88
        # The 60s track with last 15s quiet should trigger it
        assert any(s in section_names for s in ["outro", "breakdown"]), \
            f"No outro/breakdown at end: {sections}"
