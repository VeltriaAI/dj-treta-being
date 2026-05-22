"""Audio-file detection — single source of truth for what counts as a playable track.

Why this exists: macOS writes AppleDouble sidecar files (``._Track.mp3``) next to
real audio whenever the library lives on a non-native filesystem (exFAT/FAT32/NTFS
SSDs, USB sticks). Those sidecars carry the ``.mp3`` extension but are 4 KB metadata
stubs, not audio — Mixxx throws "could not be loaded" if asked to play one. A bare
``suffix in AUDIO_EXTENSIONS`` check matches them. Every library scanner must route
through :func:`is_audio_file` so a dotfile never reaches Mixxx, regardless of which
filesystem the library sits on.
"""

from pathlib import Path

AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a")


def is_audio_file(f: Path) -> bool:
    """True only for real audio tracks.

    Excludes any dotfile — AppleDouble (``._foo.mp3``), ``.DS_Store``, and other
    hidden files — even when the name carries an audio extension.
    """
    if f.name.startswith("."):
        return False
    return f.suffix.lower() in AUDIO_EXTENSIONS
