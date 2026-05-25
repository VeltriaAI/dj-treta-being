"""E6 — Context-driven color palettes.

Maps genre / key / energy combinations → a named visual palette.
Consumed by both the OSC emitter (sends palette name as a string param)
and the Omni prompt builder (injects the palette into the visual brief).

Palette names are intentionally evocative rather than hex-precise so the
downstream renderer (TouchDesigner, p5.js, Omni, etc.) can interpret them
with its own look.  The OSC message always also carries the raw
genre/energy/section so a custom renderer can ignore the name and drive
its own mapping.

Design rules
------------
* Genre wins over key.  Key nudges within a genre bucket.
* Energy level (1-10) biases toward lighter/brighter (high) or
  darker/moodier (low) variants.
* Fallback: ``"Midnight"`` — safe dark-neutral for anything unmapped.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Palette registry
# Each entry: (name, hex_primary, hex_secondary, hex_accent, mood_hint)
# hex fields are here for future coloured-waveform use (E3 extension).
# ---------------------------------------------------------------------------

PALETTE_REGISTRY: dict[str, dict] = {
    # Warm / golden
    "Sunset":       {"primary": "#FF6B35", "secondary": "#F7931E", "accent": "#FFD700", "mood": "warm-amber"},
    "Ember":        {"primary": "#C0392B", "secondary": "#E74C3C", "accent": "#F39C12", "mood": "intense-red"},
    # Cool / blue
    "Glacier":      {"primary": "#1ABC9C", "secondary": "#3498DB", "accent": "#ECF0F1", "mood": "cool-cyan"},
    "Horizon":      {"primary": "#2980B9", "secondary": "#6C3483", "accent": "#AED6F1", "mood": "blue-purple"},
    # Laser / neon
    "Laser":        {"primary": "#00FF41", "secondary": "#FF00FF", "accent": "#00FFFF", "mood": "neon-matrix"},
    "UV":           {"primary": "#7D00FF", "secondary": "#FF007F", "accent": "#FFFFFF", "mood": "ultra-violet"},
    # Dark / moody
    "Midnight":     {"primary": "#1A1A2E", "secondary": "#16213E", "accent": "#533483", "mood": "dark-deep"},
    "Abyss":        {"primary": "#0D0D0D", "secondary": "#1C1C1C", "accent": "#FF4500", "mood": "pitch-dark"},
    # Earth / organic
    "Forest":       {"primary": "#27AE60", "secondary": "#145A32", "accent": "#ABEBC6", "mood": "earthy-green"},
    "Desert":       {"primary": "#E67E22", "secondary": "#784212", "accent": "#FAD7A0", "mood": "warm-earth"},
    # Psychedelic
    "Fractal":      {"primary": "#FF00FF", "secondary": "#00FFFF", "accent": "#FFFF00", "mood": "psychedelic"},
    "Plasma":       {"primary": "#FF6EFF", "secondary": "#FFD700", "accent": "#00FF7F", "mood": "acid"},
    # Minimal
    "Monolith":     {"primary": "#2C3E50", "secondary": "#ECF0F1", "accent": "#E74C3C", "mood": "stark-minimal"},
    "Ivory":        {"primary": "#FDFEFE", "secondary": "#D5D8DC", "accent": "#1A1A2E", "mood": "light-minimal"},
}

# ---------------------------------------------------------------------------
# Mapping rules — evaluated top-to-bottom, first match wins.
# Each rule: (genre_keywords, key_mode, energy_min, energy_max, palette_name)
# - genre_keywords: list of lowercase substrings matched against genre
# - key_mode: "major" | "minor" | None (any)
# - energy_min/max: inclusive bounds on energy (1-10)
# ---------------------------------------------------------------------------

_RULES: list[tuple[list[str], str | None, int, int, str]] = [
    # ── Psytrance / Goa ────────────────────────────────────────────────────
    (["psy", "goa"],                None,    8, 10, "Fractal"),
    (["psy", "goa"],                None,    1,  7, "Plasma"),

    # ── Drum & Bass / Jungle ───────────────────────────────────────────────
    (["dnb", "drum-n-bass", "drum and bass", "jungle"],
                                    None,    7, 10, "Laser"),
    (["dnb", "drum-n-bass", "drum and bass", "jungle"],
                                    None,    1,  6, "Midnight"),

    # ── Hardstyle / Hard Techno / Big Room ────────────────────────────────
    (["hardstyle", "hard-techno", "hard techno", "big-room", "big room"],
                                    None,    7, 10, "UV"),
    (["hardstyle", "hard-techno", "hard techno", "big-room", "big room"],
                                    None,    1,  6, "Ember"),

    # ── Peak-time Techno ──────────────────────────────────────────────────
    (["peak-time", "peaktime", "peak time"],
                                    None,    7, 10, "Laser"),
    (["peak-time", "peaktime", "peak time"],
                                    None,    1,  6, "Abyss"),

    # ── Melodic Techno / Progressive ──────────────────────────────────────
    (["melodic techno", "melodic-techno"],
                                    "minor", 6, 10, "Horizon"),
    (["melodic techno", "melodic-techno"],
                                    "major", 6, 10, "Sunset"),
    (["melodic techno", "melodic-techno"],
                                    None,    1,  5, "Midnight"),
    (["progressive"],               "minor", 5, 10, "Horizon"),
    (["progressive"],               "major", 5, 10, "Sunset"),

    # ── Deep House ────────────────────────────────────────────────────────
    (["deep house", "deep-house"],  "minor", 4, 10, "Sunset"),
    (["deep house", "deep-house"],  "major", 4, 10, "Desert"),
    (["deep house", "deep-house"],  None,    1,  3, "Midnight"),

    # ── Afro / Tribal ─────────────────────────────────────────────────────
    (["afro", "tribal", "bollyafro", "afrobeats"],
                                    None,    5, 10, "Desert"),
    (["afro", "tribal", "bollyafro", "afrobeats"],
                                    None,    1,  4, "Forest"),

    # ── Organic / Downtempo / Ambient ─────────────────────────────────────
    (["organic", "downtempo", "ambient", "lo-fi", "lofi"],
                                    None,    1,  4, "Forest"),
    (["organic", "downtempo", "ambient"],
                                    None,    5, 10, "Glacier"),

    # ── Techno (generic) ──────────────────────────────────────────────────
    (["techno"],                    None,    7, 10, "Abyss"),
    (["techno"],                    "minor", 4,  6, "Monolith"),
    (["techno"],                    "major", 4,  6, "Glacier"),

    # ── House (generic) ───────────────────────────────────────────────────
    (["house"],                     "major", 6, 10, "Ivory"),
    (["house"],                     "minor", 6, 10, "Sunset"),
    (["house"],                     None,    1,  5, "Midnight"),

    # ── Trance ────────────────────────────────────────────────────────────
    (["trance"],                    None,    7, 10, "UV"),
    (["trance"],                    None,    1,  6, "Horizon"),
]


def _key_mode(key: str) -> str:
    """Derive 'major' or 'minor' from a musical key string like 'Am' or 'F#'."""
    if not key:
        return "major"
    # Lower-case suffix 'm' → minor (e.g. "Am", "Dm", "F#m")
    stripped = key.strip()
    if stripped.endswith("m") and not stripped.lower() in ("am",):
        # Avoid treating "Am" as ambiguous — 'A' uppercase + 'm' → minor
        pass
    # Simple heuristic: if last char is 'm' (and length > 1) → minor
    if len(stripped) >= 2 and stripped[-1] == "m":
        return "minor"
    return "major"


def get_palette(
    genre: str = "",
    key: str = "",
    energy: int = 5,
) -> str:
    """Return the best palette name for the given audio context.

    Parameters
    ----------
    genre:  Canonical genre string, e.g. "melodic-techno", "deep-house".
    key:    Musical key string, e.g. "Am", "F#", "Dm".
    energy: Current energy level 1-10.

    Returns the palette *name* (key into PALETTE_REGISTRY).
    Falls back to ``"Midnight"`` for unmapped combinations.
    """
    genre_lc = (genre or "").lower()
    mode = _key_mode(key)
    energy = max(1, min(10, int(energy)))

    for keywords, key_mode, e_min, e_max, palette in _RULES:
        # Genre match — any keyword must appear in genre string
        if not any(kw in genre_lc for kw in keywords):
            continue
        # Key mode match (None = any)
        if key_mode is not None and key_mode != mode:
            continue
        # Energy range
        if not (e_min <= energy <= e_max):
            continue
        return palette

    return "Midnight"


def palette_colors(name: str) -> dict:
    """Return the hex color dict for a palette name (or Midnight fallback)."""
    return PALETTE_REGISTRY.get(name, PALETTE_REGISTRY["Midnight"])
