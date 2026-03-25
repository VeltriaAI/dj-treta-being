"""Camelot Wheel — Key compatibility for harmonic mixing.

Python port of skills/dj/mcp-server/src/camelot.ts.
"""

# Mixxx key codes → musical keys
MIXXX_KEY_MAP = {
    0: "INVALID",
    1: "C", 2: "Db", 3: "D", 4: "Eb", 5: "E", 6: "F",
    7: "F#", 8: "G", 9: "Ab", 10: "A", 11: "Bb", 12: "B",
    13: "Cm", 14: "C#m", 15: "Dm", 16: "Ebm", 17: "Em", 18: "Fm",
    19: "F#m", 20: "Gm", 21: "G#m", 22: "Am", 23: "Bbm", 24: "Bm",
}

# Musical key → Camelot code
KEY_TO_CAMELOT = {
    # Major (B side)
    "C": "8B", "Db": "3B", "D": "10B", "Eb": "5B",
    "E": "12B", "F": "7B", "F#": "2B", "G": "9B",
    "Ab": "4B", "A": "11B", "Bb": "6B", "B": "1B",
    # Minor (A side)
    "Cm": "5A", "C#m": "12A", "Dm": "7A", "Ebm": "2A",
    "Em": "9A", "Fm": "4A", "F#m": "11A", "Gm": "6A",
    "G#m": "1A", "Am": "8A", "Bbm": "3A", "Bm": "10A",
}

CAMELOT_TO_KEY = {v: k for k, v in KEY_TO_CAMELOT.items()}


def mixxx_key_to_camelot(key_code: int) -> str | None:
    musical = MIXXX_KEY_MAP.get(key_code)
    if not musical or musical == "INVALID":
        return None
    return KEY_TO_CAMELOT.get(musical)


def mixxx_key_to_musical(key_code: int) -> str | None:
    key = MIXXX_KEY_MAP.get(key_code)
    if not key or key == "INVALID":
        return None
    return key


def get_compatible_keys(camelot_code: str) -> list[str]:
    """Get all compatible Camelot codes for harmonic mixing."""
    if not camelot_code or len(camelot_code) < 2:
        return []

    # Parse "8B" → num=8, letter="B"
    letter = camelot_code[-1]
    try:
        num = int(camelot_code[:-1])
    except ValueError:
        return []

    if letter not in ("A", "B") or not (1 <= num <= 12):
        return []

    compatible = [camelot_code]  # same key
    up = (num % 12) + 1
    down = ((num - 2) % 12) + 1
    compatible.append(f"{up}{letter}")   # +1 on wheel
    compatible.append(f"{down}{letter}") # -1 on wheel
    other = "B" if letter == "A" else "A"
    compatible.append(f"{num}{other}")   # relative major/minor

    return compatible


def key_compatibility_score(key1: str, key2: str) -> int:
    """Score key compatibility 0-10.

    10 = same key, 8 = adjacent/relative, 5 = 2 steps, 0 = incompatible.
    """
    if not key1 or not key2:
        return 5  # unknown keys, neutral score

    cam1 = KEY_TO_CAMELOT.get(key1)
    cam2 = KEY_TO_CAMELOT.get(key2)
    if not cam1 or not cam2:
        return 5

    if cam1 == cam2:
        return 10

    compatible = get_compatible_keys(cam1)
    if cam2 in compatible:
        return 8

    # Check 2 steps away
    for c in compatible:
        if cam2 in get_compatible_keys(c):
            return 5

    return 2  # far apart


def format_key(key_code: int) -> str:
    musical = mixxx_key_to_musical(key_code)
    camelot = mixxx_key_to_camelot(key_code)
    if not musical or not camelot:
        return "Unknown"
    return f"{musical} ({camelot})"
