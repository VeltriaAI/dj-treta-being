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

# Enharmonic spellings that differ from KEY_TO_CAMELOT's canonical keys. The
# librosa analyzer (audio_analysis.KEY_NAMES) emits sharps like "C#" and "Abm"
# that the table — keyed by flats ("Db", "G#m") — lacks, so a raw
# KEY_TO_CAMELOT.get() returned "" and the key was SILENTLY dropped (~27% of a
# real library: every C#-major and Ab-minor track). Map each alias to its
# canonical equivalent. Covers all 12 sharp/flat pairs in both modes so any
# upstream source (Mixxx import, dataset, generation) resolves too.
ENHARMONIC_ALIASES = {
    # Major
    "C#": "Db", "D#": "Eb", "G#": "Ab", "A#": "Bb",
    "Gb": "F#", "Cb": "B", "Fb": "E", "E#": "F", "B#": "C",
    # Minor
    "Dbm": "C#m", "Abm": "G#m", "D#m": "Ebm", "A#m": "Bbm", "Gbm": "F#m",
}

import re as _re

# A Camelot code: 1-12 followed by A or B (e.g. "8A", "12B").
_CAMELOT_CODE_RE = _re.compile(r"^(1[0-2]|[1-9])[AB]$")


def to_camelot(key: str) -> str:
    """Musical key name -> Camelot code, tolerant of enharmonic spellings.

    Returns "" when the key is unknown. Use this everywhere instead of a raw
    ``KEY_TO_CAMELOT.get(key, "")`` so sharp/flat spelling never drops a key.
    """
    if not key:
        return ""
    key = key.strip()
    code = KEY_TO_CAMELOT.get(key)
    if code:
        return code
    canon = ENHARMONIC_ALIASES.get(key)
    if canon:
        return KEY_TO_CAMELOT.get(canon, "")
    return ""


def as_camelot(key: str) -> str:
    """Normalize EITHER a Camelot code ("8A") OR a musical key name ("Am") to a
    Camelot code. Returns "" if unresolvable.

    The live callers (planner topup, prompt builders) pass Camelot codes; the
    test suite passes musical names. Accepting both is what makes
    ``key_compatibility_score`` actually work on real input instead of silently
    returning the neutral score for every pair.
    """
    if not key:
        return ""
    key = key.strip()
    if _CAMELOT_CODE_RE.match(key.upper()):
        return key.upper()
    return to_camelot(key)


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

    10 = same key, 8 = adjacent/relative, 5 = 2 steps, 2 = far apart.
    Accepts either Camelot codes ("8A") or musical names ("Am") — the live
    planner passes codes, the tests pass names. (Previously this used
    ``KEY_TO_CAMELOT.get`` directly, which only matched musical names, so every
    real call from the planner — which passes codes — silently returned 5.)
    """
    cam1 = as_camelot(key1)
    cam2 = as_camelot(key2)
    if not cam1 or not cam2:
        return 5  # unknown keys, neutral score

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


def relationship(key1: str, key2: str) -> str:
    """Classify the harmonic relationship between two keys, matching the DJ
    agent's Camelot rubric (agents.py): COMPATIBLE (same / ±1 fifth / relative
    major-minor / +7 energy-boost like 8A->3A), BRIDGEABLE (±2 whole-step,
    mask over a breakdown), DISSONANT (everything else). Returns "UNKNOWN" when
    either key is missing/unresolvable so the consumer can degrade gracefully.

    Accepts Camelot codes or musical names.
    """
    cam1 = as_camelot(key1)
    cam2 = as_camelot(key2)
    if not cam1 or not cam2:
        return "UNKNOWN"
    if cam1 == cam2:
        return "COMPATIBLE"

    n1, l1 = int(cam1[:-1]), cam1[-1]
    n2, l2 = int(cam2[:-1]), cam2[-1]
    diff = abs(n1 - n2)
    ring = min(diff, 12 - diff)  # steps around the wheel on one ring

    if l1 == l2:
        if ring <= 1:
            return "COMPATIBLE"            # same number or ±1 (perfect fifth)
        if (n2 - n1) % 12 == 7:
            return "COMPATIBLE"            # +7 energy boost (e.g. 8A -> 3A)
        if ring == 2:
            return "BRIDGEABLE"            # whole step — bridge over a breakdown
        return "DISSONANT"
    # opposite letter: only the relative major/minor (same number) is compatible
    if n1 == n2:
        return "COMPATIBLE"                # letter swap, e.g. 8A <-> 8B
    return "DISSONANT"


def format_key(key_code: int) -> str:
    musical = mixxx_key_to_musical(key_code)
    camelot = mixxx_key_to_camelot(key_code)
    if not musical or not camelot:
        return "Unknown"
    return f"{musical} ({camelot})"
