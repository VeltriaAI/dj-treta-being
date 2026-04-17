"""Canonical track identity — LLM-resolved artist/song/version/remixer.

Called at download time to dedup against library regardless of YouTube title variance.
Output schema is stable so DB lookups on (canonical_artist, canonical_song,
canonical_version, remixer) work reliably.
"""

import json
import logging
import re

log = logging.getLogger("dj-treta")


def _strip_topic(uploader: str) -> str:
    """YouTube auto-channels append ' - Topic' to artist name. Strip it."""
    if uploader and uploader.lower().endswith(" - topic"):
        return uploader[: -len(" - topic")].strip()
    return (uploader or "").strip()


def _fallback_parse(title: str, uploader: str) -> dict:
    """Cheap heuristic parse when LLM is unavailable.

    Not great — LLM path should be preferred. This exists so downloads
    never fail silently if the LLM is unreachable.
    """
    artist = _strip_topic(uploader)
    song = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title).strip()
    song = re.sub(r"\s*\|.*$", "", song).strip()
    # If title already contains "Artist - Song", prefer that
    if " - " in song:
        left, right = song.split(" - ", 1)
        if len(left) < 60:
            artist = left.strip() or artist
            song = right.strip()
    return {
        "canonical_artist": artist,
        "canonical_song": song,
        "canonical_version": None,
        "remixer": None,
        "canonical_confidence": 0.3,
        "notes": "fallback heuristic (no LLM)",
    }


def llm_canonicalize(title: str, uploader: str = "",
                     duration_seconds: float = 0) -> dict:
    """Ask LLM to resolve YouTube title+uploader into canonical identity.

    Returns dict with keys:
        canonical_artist, canonical_song, canonical_version, remixer,
        canonical_confidence, notes

    Falls back to a heuristic parse if LLM call fails.
    """
    title = (title or "").strip()
    uploader = _strip_topic(uploader)
    if not title:
        return {
            "canonical_artist": "",
            "canonical_song": "",
            "canonical_version": None,
            "remixer": None,
            "canonical_confidence": 0.0,
            "notes": "empty title",
        }

    prompt = f"""You normalize track identity from a YouTube upload.
Return STRICT JSON only, no prose, no markdown fences.

Input:
  title: "{title}"
  uploader: "{uploader}"
  duration_seconds: {duration_seconds or 'unknown'}

Output schema (all fields REQUIRED, use null where unknown):
{{
  "artist": "primary performing artist (original creator; for a remix this is still the original artist)",
  "song": "the song title, cleaned — strip decorations like 'Official Audio', 'MELODIC TECHNO', '| Free Download'",
  "version": "Original Mix | Extended Mix | Radio Edit | Club Mix | Instrumental | Acoustic | Live | null — null if no explicit version label",
  "remixer": "artist who remixed it, or null if not a remix",
  "confidence": 0.0-1.0
}}

Rules:
- Strip YouTube-only markers: "(Official Audio)", "[HD]", "| MELODIC TECHNO", "FREE DOWNLOAD", uploader name duplicated in title, emoji prefixes like ​@handle, tier labels, trailing dashes.
- If title is "A - B (Remix)" where A is the original artist and B is the song, that's artist=A, song=B, version="Remix" and infer remixer from context if present.
- Never invent remixer. If unclear, set remixer=null.
- Do NOT include the remixer's name in the artist field.
- Normalize multiple artists with ", " (comma + space), e.g. "Ellie Goulding, blackbear".
- Song field should not repeat the artist name.

Examples:
  Input: title="Stephan Jolk - Morgen (Original Mix) | MELODIC TECHNO", uploader="Running Clouds"
  → {{"artist":"Stephan Jolk","song":"Morgen","version":"Original Mix","remixer":null,"confidence":0.97}}

  Input: title="Ellie Goulding, blackbear - Worry About Me (Lost Frequencies Remix)", uploader="Lost Frequencies"
  → {{"artist":"Ellie Goulding, blackbear","song":"Worry About Me","version":"Remix","remixer":"Lost Frequencies","confidence":0.96}}

  Input: title="​@afusic  - Pal Pal (MADOC AFRO EDIT) | 2025 PUNJABI BOLLY AFRO", uploader="madocofficial"
  → {{"artist":"afusic","song":"Pal Pal","version":"Edit","remixer":"Madoc","confidence":0.9}}

Now return JSON for the input above."""

    try:
        from .config import load_config
        from litellm import completion as _completion
        cfg = load_config()
        resp = _completion(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
            temperature=0.1, timeout=15,
        )
        raw = resp.choices[0].message.content.strip()
        if "```" in raw:
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.split("```")[0].strip()
        data = json.loads(raw)
    except Exception as e:
        log.warning(f"Canonicalize LLM failed ({type(e).__name__}): {e} — falling back to heuristic")
        return _fallback_parse(title, uploader)

    def _nullish(v):
        """Coerce string 'null'/'none'/'' and other falsies to real None."""
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in ("", "null", "none", "n/a", "unknown"):
            return None
        return s

    artist = _nullish(data.get("artist")) or _strip_topic(uploader)
    song = _nullish(data.get("song")) or title
    version = _nullish(data.get("version"))
    remixer = _nullish(data.get("remixer"))

    # Default: a plain-titled track with no explicit version and no remixer is
    # the Original Mix. This makes "Stephan Jolk - Morgen" dedup against
    # "Stephan Jolk - Morgen (Original Mix)".
    if version is None and remixer is None:
        version = "Original Mix"

    return {
        "canonical_artist": artist,
        "canonical_song": song,
        "canonical_version": version,
        "remixer": remixer,
        "canonical_confidence": float(data.get("confidence", 0.0) or 0.0),
        "notes": "",
    }


def canonical_filename(canon: dict, fallback: str = "") -> str:
    """Build a human-friendly canonical filename stem (no extension).

    Examples:
        Stephan Jolk - Morgen (Original Mix)
        Ellie Goulding, blackbear - Worry About Me (Lost Frequencies Remix)
    """
    artist = canon.get("canonical_artist") or ""
    song = canon.get("canonical_song") or fallback
    version = canon.get("canonical_version")
    remixer = canon.get("remixer")

    base = f"{artist} - {song}" if artist else song
    if remixer:
        base += f" ({remixer} Remix)"
    elif version:
        base += f" ({version})"

    # Filesystem-safe
    base = re.sub(r'[/\\:*?"<>|]', "_", base).strip()
    base = re.sub(r"\s+", " ", base)
    return base[:180] or fallback or "track"
