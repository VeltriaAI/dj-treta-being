"""Tests for agent.audio_files.is_audio_file — the dotfile/AppleDouble guard.

Regression coverage for the macOS exFAT bug: ``._Track.mp3`` AppleDouble
sidecars carry an audio extension but are not playable. A bare suffix check
matched them and handed them to Mixxx ("could not be loaded"). is_audio_file
must reject any dotfile while accepting real tracks.
"""

from pathlib import Path

from agent.audio_files import is_audio_file


def test_real_audio_accepted():
    for name in (
        "Maxim Lany - Shifter (Original Mix).mp3",
        "Lane 8 - Closer.flac",
        "track.wav",
        "track.ogg",
        "track.m4a",
        "UPPER.MP3",  # case-insensitive
    ):
        assert is_audio_file(Path("/lib/genre") / name) is True, name


def test_appledouble_rejected():
    # The exact files from the production incident
    for name in (
        "._Maxim Lany - Shifter (Original Mix).mp3",
        "._Eric Luttrell - LOVE (Extended Mix).mp3",
        "._Helsloot - Hideaway (Extended Mix).mp3",
    ):
        assert is_audio_file(Path("/lib/genre") / name) is False, name


def test_other_dotfiles_rejected():
    for name in (".DS_Store", ".hidden.mp3", "._whatever", "."):
        assert is_audio_file(Path("/lib/genre") / name) is False, name


def test_non_audio_rejected():
    for name in ("cover.jpg", "notes.txt", "playlist.m3u", "folder"):
        assert is_audio_file(Path("/lib/genre") / name) is False, name
