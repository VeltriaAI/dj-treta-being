"""Library management -- list tracks, get set history."""

import json
from pathlib import Path

from .helpers import _music_dir
from ..runtime_paths import runtime_path

def list_library_tracks() -> list:
    """List all tracks in the music library with file path, filename, and genre folder."""
    tracks = []
    for genre_dir in sorted(_music_dir().iterdir()):
        if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
            continue
        genre = genre_dir.name
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                tracks.append({
                    "path": str(f),
                    "filename": f.stem,
                    "genre": genre,
                })
    return tracks


def get_set_history() -> list:
    """Get the list of tracks played in the current set with titles and timestamps."""
    state_file = runtime_path("state.json")
    if state_file.exists():
        data = json.loads(state_file.read_text())
        return data.get("tracks_played", [])
    return []
