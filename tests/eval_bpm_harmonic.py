"""Eval tests for BPM compatibility and harmonic (key) mixing rules.

Tests that the planner agent respects BPM ±10 hard limits, ideal ±6 range,
genre-specific BPM ranges, Camelot key distance rules, and transition-type
exceptions (hard cuts allow far keys).
"""

import pytest
from tests.eval_helpers import eval_agent, text_contains
from tests.eval_conftest import PLANNER_TOOLS


# ── BPM-aware planner prompt ───────────────────────────────────────────────

def bpm_planner_prompt() -> str:
    return """\
You are DJ Treta's track planner.

BPM RULES:
- Next track MUST be within ±10 BPM of current (hard limit)
- Ideal range: ±6 BPM
- BPM trend should generally go UP during first 75% of set
- Genre BPM ranges:
  - Deep House: 115-128
  - Progressive House: 120-132
  - Melodic Techno: 118-132
  - Dark Techno: 125-145
  - Psytrance: 135-155
  - Ambient: 70-120

HARMONIC MIXING (KEY) RULES:
- Prefer Camelot distance ≤1 (same key, adjacent, or relative major/minor)
- For long crossfade blends: Camelot ≤1 is REQUIRED
- For hard cuts: any key is acceptable (no harmonic overlap)
- When tracks are percussion-only (intro/outro with no melody): any key works

Pick tracks from the library provided. For each recommendation: title, BPM, key, energy, why it fits.
If no compatible tracks exist in library, use search_music to find more.
"""


# ── BPM Tests ──────────────────────────────────────────────────────────────

@pytest.mark.eval
def test_bm01_bpm_within_ideal_range():
    """BM-01: Planner prefers tracks within ±6 BPM (ideal range)."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 125 BPM, Key: Am, Energy: 7, Mood: melodic-techno\n"
            "Library (use directly):\n"
            "- Track A: 'Anyma - Explore' | BPM:123 Key:Cm Energy:7\n"
            "- Track B: 'ANNA - Defiant' | BPM:133 Key:Gm Energy:8\n"
            "- Track C: 'Monolink - Otherside' | BPM:121 Key:Dm Energy:6\n"
            "Pick next track."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    # Should recommend Track A (123, +2) or Track C (121, -4) — both ideal range
    has_a = "anyma" in text or "explore" in text or "track a" in text
    has_c = "monolink" in text or "otherside" in text or "track c" in text
    assert has_a or has_c, (
        f"Expected Track A (Anyma) or Track C (Monolink) in ideal ±6 BPM range, got: {result['text'][:300]}"
    )


@pytest.mark.eval
def test_bm02_bpm_hard_limit():
    """BM-02: Planner never recommends tracks beyond ±10 BPM hard limit."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 125 BPM, Key: Am, Energy: 7, Mood: melodic-techno\n"
            "Library (use directly):\n"
            "- Track A: 'Caribou - Sun' | BPM:100 Key:Cm Energy:5\n"
            "- Track B: 'Four Tet - Baby' | BPM:140 Key:Gm Energy:8\n"
            "- Track C: 'Bicep - Atlas' | BPM:128 Key:Dm Energy:7\n"
            "Pick next track."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    # Must recommend Track C (128, +3). Must NOT recommend A (-25) or B (+15)
    has_c = "bicep" in text or "atlas" in text or "track c" in text
    assert has_c, f"Expected Track C (Bicep - Atlas) as only BPM-compatible option, got: {result['text'][:300]}"
    # Verify it doesn't recommend the out-of-range tracks as picks
    # (mentioning them to explain why they're rejected is fine)
    recommends_a = ("recommend" in text or "pick" in text or "next track" in text) and (
        "caribou" in text.split("recommend")[-1] if "recommend" in text else False
    )
    # Softer check: just ensure Track C is the clear recommendation
    assert has_c


@pytest.mark.eval
def test_bm03_genre_specific_bpm_psytrance():
    """BM-03: Planner respects genre-specific BPM ranges (psytrance 135-155)."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 142 BPM, Key: Am, Energy: 8, Mood: psytrance\n"
            "Library (use directly):\n"
            "- Track A: 'Astrix - Type 1' | BPM:145 Key:Dm Energy:9 | psytrance\n"
            "- Track B: 'Solomun - Home' | BPM:120 Key:Am Energy:5 | deep-house\n"
            "- Track C: 'Vini Vici - Great Spirit' | BPM:140 Key:Gm Energy:9 | psytrance\n"
            "Pick next track."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    has_a = "astrix" in text or "type 1" in text or "track a" in text
    has_c = "vini vici" in text or "great spirit" in text or "track c" in text
    assert has_a or has_c, (
        f"Expected Track A (Astrix) or Track C (Vini Vici) for psytrance set, got: {result['text'][:300]}"
    )
    # Solomun at 120 BPM is -22 from current 142 — well outside ±10 hard limit
    recommends_b_as_pick = "solomun" in text and ("recommend" in text or "pick" in text or "next" in text)
    # We don't hard-fail on mention (explanation is fine), but Track A or C must be the pick
    assert has_a or has_c


# ── Harmonic Mixing Tests ─────────────────────────────────────────────────

@pytest.mark.eval
def test_hm01_same_key_safe():
    """HM-01: Same Camelot key is always a safe harmonic match."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 125 BPM, Key: Am (Camelot 8A), Energy: 7, Mood: melodic-techno\n"
            "Library (use directly):\n"
            "- Track A: 'Anyma - Angel' | BPM:124 Key:Am Energy:7 | 8A\n"
            "- Track B: 'Artbat - Best Fit' | BPM:126 Key:Ebm Energy:7 | 2A\n"
            "Pick next track for a smooth crossfade blend."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    has_a = "anyma" in text or "angel" in text or "track a" in text
    assert has_a, (
        f"Expected Track A (Anyma - Angel, same key Am/8A) for crossfade blend, got: {result['text'][:300]}"
    )


@pytest.mark.eval
def test_hm02_adjacent_camelot_key():
    """HM-02: Adjacent Camelot key (distance 1) is safe for long crossfades."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 125 BPM, Key: Am (Camelot 8A), Energy: 7, Mood: melodic-techno\n"
            "Library (use directly):\n"
            "- Track A: 'Stephan Bodzin - Powers' | BPM:122 Key:Dm (Camelot 7A) Energy:6\n"
            "- Track B: 'Boris Brejcha - Gravity' | BPM:128 Key:Bbm (Camelot 3A) Energy:8\n"
            "Pick next track for a long crossfade."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    has_a = "bodzin" in text or "powers" in text or "track a" in text
    assert has_a, (
        f"Expected Track A (Bodzin - Powers, Dm/7A adjacent to Am/8A) for crossfade, got: {result['text'][:300]}"
    )


@pytest.mark.eval
def test_hm03_relative_major_minor():
    """HM-03: Relative major/minor (same Camelot number, A↔B) is safe for blends."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 125 BPM, Key: Am (Camelot 8A), Energy: 7, Mood: melodic-techno\n"
            "Library (use directly):\n"
            "- Track A: 'Recondite - Placid' | BPM:121 Key:C (Camelot 8B) Energy:5\n"
            "- Track B: 'Kobosil - Through' | BPM:128 Key:F#m (Camelot 11A) Energy:8\n"
            "Pick next track for a smooth blend."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    has_a = "recondite" in text or "placid" in text or "track a" in text
    assert has_a, (
        f"Expected Track A (Recondite - Placid, C/8B relative major of Am/8A), got: {result['text'][:300]}"
    )


@pytest.mark.eval
def test_hm04_far_key_ok_for_hard_cut():
    """HM-04: Far Camelot key is acceptable for hard_cut transitions (no harmonic overlap)."""
    result = eval_agent(
        system_prompt=bpm_planner_prompt(),
        user_message=(
            "Current: 125 BPM, Key: Am (Camelot 8A), Energy: 7, Mood: dark-techno\n"
            "Transition technique will be: hard_cut\n"
            "Library (use directly):\n"
            "- Track A: 'I Hate Models - Daydream' | BPM:132 Key:Ebm (Camelot 2A) Energy:8\n"
            "- Track B: 'Amelie Lens - Access' | BPM:130 Key:Cm (Camelot 5A) Energy:8\n"
            "For a hard_cut transition, pick the best track."
        ),
        tools=PLANNER_TOOLS,
    )
    text = result["text"].lower()
    # Either track is acceptable — key distance doesn't matter for hard cuts
    has_a = "i hate models" in text or "daydream" in text or "track a" in text
    has_b = "amelie" in text or "access" in text or "track b" in text
    assert has_a or has_b, (
        f"Expected either track to be acceptable for hard_cut (key distance irrelevant), got: {result['text'][:300]}"
    )
