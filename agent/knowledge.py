"""Music knowledge integration for DJ Treta's planner.

Provides knowledge-enriched track discovery using the 18M-track electronic music
dataset (Discogs + Ishkur). When enabled, injects real track recommendations into
the planner prompt so the LLM picks from REAL tracks instead of blind YouTube search.

The knowledge base is opt-in via config.yaml:

    knowledge:
      enabled: true
      data_dir: "~/workspace/music-intelligence/data/unified"

Falls back gracefully if the knowledge base is unavailable — planner works as before.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("dj-treta")

# Singleton — loaded once, reused across planner cycles
_kb_instance = None
_kb_load_failed = False


def _get_kb(data_dir: Optional[str] = None):
    """Get or create the MusicKnowledge singleton."""
    global _kb_instance, _kb_load_failed

    if _kb_load_failed:
        return None
    if _kb_instance is not None:
        return _kb_instance

    try:
        # Add music-intelligence repo to path if needed
        mi_path = Path.home() / "workspace" / "music-intelligence"
        if mi_path.exists() and str(mi_path) not in sys.path:
            sys.path.insert(0, str(mi_path))

        from integration.knowledge_query import MusicKnowledge

        resolved_dir = str(Path(data_dir).expanduser()) if data_dir else None
        _kb_instance = MusicKnowledge(resolved_dir)
        # Trigger lazy load to catch errors early
        count = len(_kb_instance.tracks)
        log.info(f"Music knowledge loaded: {count:,} tracks")
        return _kb_instance

    except Exception as e:
        _kb_load_failed = True
        log.warning(f"Music knowledge unavailable: {e}")
        return None


def _genre_from_mood(mood: str) -> str:
    """Map DJ Treta mood strings to knowledge base genre queries.

    Mood strings are freeform (e.g. 'melodic-techno', 'deep-house', 'techno-dark').
    The knowledge base uses Discogs subgenre labels.
    """
    mood_lower = mood.lower().replace("-", " ").replace("_", " ")

    # Direct mappings for common DJ Treta mood strings
    mapping = {
        "melodic techno": "Techno",
        "techno deep": "Techno",
        "deep techno": "Deep House",
        "dark techno": "Techno",
        "techno dark": "Techno",
        "bollyafro": "House",
        "bollytechno": "Techno",
        "psytrance": "Psy-Trance",
        "psy trance": "Psy-Trance",
        "psychill": "Downtempo",
        "progressive house": "Progressive House",
        "deep house": "Deep House",
        "minimal techno": "Minimal Techno",
        "acid techno": "Acid",
        "trance": "Trance",
        "progressive trance": "Progressive Trance",
        "tech house": "Tech House",
        "afro house": "Afro House",
        "organic house": "Organic House",
        "indie dance": "Indie Dance",
        "electronica": "Electronica",
        "ambient": "Ambient",
        "breaks": "Breaks",
        "drum and bass": "Drum n Bass",
        "dnb": "Drum n Bass",
        "downtempo": "Downtempo",
        "dub techno": "Dub Techno",
    }

    for key, value in mapping.items():
        if key in mood_lower:
            return value

    # Fallback: capitalize words and hope for a match
    return mood.replace("-", " ").replace("_", " ").title()


def get_knowledge_context(
    current_track: str,
    current_genre: str,
    mood: str,
    played_tracks: list[str],
    data_dir: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Get knowledge-enriched context for the planner prompt.

    Queries the 18M-track dataset for real tracks matching the current mood/genre.
    Returns a formatted string to inject into the planner's LLM prompt, including
    artist names, track titles, and YouTube search queries.

    Falls back gracefully if the knowledge base is not available — returns empty
    string, and the planner works exactly as before.

    Args:
        current_track: Title of currently playing track.
        current_genre: Genre/subgenre of current track (from DB analysis).
        mood: Current set mood (e.g. 'melodic-techno').
        played_tracks: List of already-played track titles (to exclude).
        data_dir: Path to parquet data directory (from config).
        limit: Max tracks to return.

    Returns:
        Formatted string for prompt injection, or "" if unavailable.
    """
    try:
        kb = _get_kb(data_dir)
        if kb is None:
            return ""

        genre_query = _genre_from_mood(mood) if mood else None

        # Extract artist names from played tracks to encourage diversity
        played_artists = []
        for title in played_tracks:
            if " - " in title:
                played_artists.append(title.split(" - ")[0].strip())

        # Query for tracks matching the mood/genre
        tracks = kb.discover_tracks(
            genre=genre_query,
            year_min=2020,  # Prefer recent tracks
            exclude_artists=played_artists[-5:] if played_artists else None,
            exclude_titles=played_tracks[-10:] if played_tracks else None,
            limit=limit,
        )

        if not tracks:
            # Broader search without year filter
            tracks = kb.discover_tracks(
                genre=genre_query,
                exclude_titles=played_tracks[-10:] if played_tracks else None,
                limit=limit,
            )

        if not tracks:
            return ""

        # Get genre info for context
        genre_info_text = ""
        if genre_query:
            gi = kb.genre_info(genre_query)
            if gi:
                bpm_range = ""
                if gi.get("bpm_low") and gi.get("bpm_high"):
                    bpm_range = f" (typical BPM: {gi['bpm_low']}-{gi['bpm_high']})"
                desc = gi.get("description", "")[:150] if gi.get("description") else ""
                genre_info_text = (
                    f"\nGENRE CONTEXT: {gi.get('name', genre_query)}{bpm_range}"
                )
                if desc:
                    genre_info_text += f"\n  {desc}"
                genre_info_text += "\n"

        # Format track recommendations
        lines = [
            f"\n{'='*60}",
            "KNOWLEDGE BASE RECOMMENDATIONS (real tracks from 18M electronic music dataset):",
            f"Genre: {genre_query or 'general'} | Mood: {mood}",
            genre_info_text,
            "These are REAL tracks by REAL artists. Use their search_query for YouTube download:",
            "",
        ]

        for i, t in enumerate(tracks, 1):
            artist = t.get("artist_name", "Unknown")
            title = t.get("title", "Unknown")
            query = t.get("search_query", f"{artist} - {title}")
            subgenre = t.get("subgenre", "")
            label = t.get("label", "")
            year = t.get("year", "")

            meta_parts = []
            if subgenre:
                meta_parts.append(subgenre)
            if label:
                meta_parts.append(f"Label: {label}")
            if year:
                meta_parts.append(str(year))
            meta = f" [{', '.join(meta_parts)}]" if meta_parts else ""

            lines.append(f"  {i}. {artist} - {title}{meta}")
            lines.append(f"     search: \"{query}\"")

        lines.append("")
        lines.append(
            "INSTRUCTIONS: Prefer these knowledge base tracks over random YouTube searches. "
            "Use the search_query field as-is for download_track. "
            "Pick tracks that fit the energy arc and BPM range."
        )
        lines.append(f"{'='*60}\n")

        return "\n".join(lines)

    except Exception as e:
        log.warning(f"Knowledge context error: {e}")
        return ""  # Graceful fallback


def reset():
    """Reset the singleton (for testing or config changes)."""
    global _kb_instance, _kb_load_failed
    _kb_instance = None
    _kb_load_failed = False
