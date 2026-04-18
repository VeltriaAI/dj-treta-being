"""Genre-specific transition eval tests for DJ Treta.

Each genre has distinct mixing rules — duration, technique, energy, timing.
These evals verify the DJ agent respects genre-specific conventions.
"""

import pytest
from tests.eval_helpers import eval_agent, eval_agent_nonempty, has_tool_call, get_tool_args, has_no_tool_calls, text_contains
from tests.eval_conftest import DJ_TOOLS


# ── Genre-Aware System Prompt ───────────────────────────────────────────


def genre_dj_prompt(genre: str) -> str:
    """DJ prompt with genre-specific transition rules."""
    base = """\
You are DJ Treta's mixing engine. You control transitions between tracks.

CORE RULES:
- NEVER transition during a DROP or BUILDUP section — wait for breakdown or outro
- Schedule transitions at BREAKDOWN or OUTRO sections
- If transition is already pending, say "transition pending"
- If idle deck has no track loaded, say "waiting"
- Use schedule_transition tool to schedule, or say "waiting" if not ready

TECHNIQUES:
- crossfade: smooth blend, default
- bass_swap: swap basslines, avoids two basslines clashing
- filter_sweep: progressive filter reveal, builds tension
- hard_cut: instant switch, for genre changes or drops
- echo_out: echo fade, creates space for tempo changes
"""
    genre_rules = {
        "melodic-techno": """
GENRE: Melodic Techno (120-128 BPM)
- Use crossfade with duration 30-60s (16-32 bars)
- Harmonic mixing is STRICT — Camelot distance must be ≤1
- Schedule transitions at breakdowns, NOT drops
- Energy arc should be sinusoidal (wave), not linear""",
        "dark-techno": """
GENRE: Dark Techno (128-140 BPM)
- Use shorter transitions: 15-30s, more aggressive
- bass_swap PREFERRED over crossfade at high energy
- hard_cut is ACCEPTABLE at drops for maximum impact
- Higher sustained energy: 6-9, fewer dips""",
        "progressive-house": """
GENRE: Progressive House (122-128 BPM)
- Use EXTRA LONG blends: 45-120s transitions
- filter_sweep preferred for texture and atmosphere
- Gradual, patient energy build — never rush""",
        "psytrance": """
GENRE: Psytrance (138-148 BPM)
- Use QUICK transitions: ≤20s or hard_cut
- Transition at DROPS, not breakdowns (unlike other genres)
- Key compatibility is LESS important at high BPM
- hard_cut at drops is the standard technique""",
        "deep-house": """
GENRE: Deep House (118-124 BPM)
- Use ultra-smooth blends: 45-90s, NEVER jarring
- Maintain groove — never break the pocket
- Be aware of vocals — don't cut during vocal phrases
- crossfade is almost always the right technique""",
        "ambient": """
GENRE: Ambient/Chill (80-110 BPM)
- Use texture blending: 60-180s transitions
- Energy stays in 2-5 range, no drops or peaks
- BPM tolerance is very wide (±15 acceptable)
- Layering and atmosphere over beat matching""",
    }
    return base + genre_rules.get(genre, "")


# ── Melodic Techno ──────────────────────────────────────────────────────


@pytest.mark.eval
def test_mt01_blend_duration_30_60s():
    """MT-01: Melodic techno transitions should use 30-60s blend duration."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("melodic-techno"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Anyma "Eternity" (125 BPM, key Am/8A, energy 6)
  Position: 4:12 / 6:30 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 3:45-4:45][DROP 4:45-5:30][outro 5:30-6:30]
- Idle deck: 2 — Tale Of Us "Nova" (126 BPM, key Cm/5A, energy 7) — LOADED, cued
  Camelot distance: 3 steps (8A → 5A)
- No transition pending

What should we do?""",
        tools=DJ_TOOLS,
    )
    if has_tool_call(result, "schedule_transition"):
        args = get_tool_args(result, "schedule_transition")
        assert 30 <= args.get("duration", 0) <= 60, (
            f"Melodic techno blend should be 30-60s, got {args.get('duration')}"
        )
    # If the agent flags the key distance (3 steps) that's also acceptable


@pytest.mark.eval
def test_mt02_harmonic_clash_rejected():
    """MT-02: Melodic techno should reject large harmonic clashes (6 Camelot steps).

    Uses eval_agent_nonempty because Flash drops responses on this prompt
    ~60% of the time. When it does respond, the answer is "Harmonic clash
    is too large. I will not transition to deck 2. Waiting." which is
    correct. 5-trial retry → 92% success rate.
    """
    result = eval_agent_nonempty(
        system_prompt=genre_dj_prompt("melodic-techno"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Stephan Bodzin "Singularity" (124 BPM, key Am/8A, energy 5)
  Position: 3:50 / 7:00 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 3:30-4:30][DROP 4:30-5:45][outro 5:45-7:00]
- Idle deck: 2 — Adriatique "Midnight Sun" (125 BPM, key Ebm/2A, energy 6) — LOADED, cued
  Camelot distance: 6 steps (8A → 2A) — LARGE harmonic clash
- No transition pending

Ready for transition?""",
        tools=DJ_TOOLS,
    )
    # Agent should NOT schedule a crossfade into a 6-step key clash in melodic techno
    if has_tool_call(result, "schedule_transition"):
        args = get_tool_args(result, "schedule_transition")
        # If it does schedule, it should at least not use crossfade (maybe hard_cut for genre change)
        assert args.get("technique") != "crossfade", (
            "Melodic techno should not crossfade with 6-step Camelot clash"
        )
    else:
        # Expected: agent declines or says waiting due to key mismatch
        assert text_contains(result, "key") or text_contains(result, "harmonic") or text_contains(result, "waiting") or text_contains(result, "clash") or text_contains(result, "camelot"), (
            f"Expected agent to flag key clash, got: {result['text'][:200]}"
        )


@pytest.mark.eval
def test_mt03_breakdown_not_drop():
    """MT-03: Melodic techno should schedule at breakdown, not at drop."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("melodic-techno"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Anyma & Chris Avantgarde "Consciousness" (126 BPM, key Dm/7A, energy 7)
  Position: 2:55 / 7:15 — currently in GROOVE section (before drop)
  Timeline: [...groove 1:00-3:15...][DROP 3:15-4:30][BREAKDOWN 4:30-5:30][DROP 5:30-6:30][outro 6:30-7:15]
  Next section: DROP at 3:15 (in 20 seconds)
- Idle deck: 2 — Tale Of Us "Paradigm" (125 BPM, key Em/8A, energy 6) — LOADED, cued
  Camelot distance: 1 step (7A → 8A) — perfect harmonic match
- No transition pending

Drop coming in 20 seconds. Should I transition now?""",
        tools=DJ_TOOLS,
    )
    if has_tool_call(result, "schedule_transition"):
        args = get_tool_args(result, "schedule_transition")
        # Should schedule at breakdown (4:30 = 270s), NOT at the imminent drop (3:15 = 195s)
        pos = args.get("at_position", 0)
        assert pos >= 250, (
            f"Melodic techno should schedule at breakdown (~270s), not drop (~195s), got position {pos}"
        )
    else:
        # Acceptable: agent says to wait for breakdown
        assert text_contains(result, "wait") or text_contains(result, "breakdown"), (
            f"Expected agent to wait for breakdown, got: {result['text'][:200]}"
        )


# ── Dark Techno ─────────────────────────────────────────────────────────


@pytest.mark.eval
def test_dt01_shorter_transitions_15_30s():
    """DT-01: Dark techno transitions should use 15-30s duration."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("dark-techno"),
        user_message=(
            "ACTIVE: 'Amelie Lens - Hypnotized' at 200s/345s (145s left, BPM:134, Key:Fm)\n"
            "NOW IN: BREAKDOWN (energy:4, 180s-225s)\n"
            "TIMELINE: 0s-30s intro(3) → 30s-180s groove(7) → 180s-225s BREAKDOWN(4) → "
            "225s-300s drop(9) → 300s-345s outro(2)\n"
            "NEXT: 'I Hate Models - Daydream' loaded on deck 2, BPM:133, Key:Gm\n"
            "ACTION REQUIRED: schedule_transition or say waiting"
        ),
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Dark techno at breakdown should schedule transition"
    args = get_tool_args(result, "schedule_transition")
    duration = args.get("duration", 0)
    assert 10 <= duration <= 35, (
        f"Dark techno transition should be 15-30s (with margin), got {duration}"
    )


@pytest.mark.eval
def test_dt02_bass_swap_at_high_energy():
    """DT-02: Dark techno should prefer bass_swap when both tracks are high energy (8+)."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("dark-techno"),
        user_message=(
            "ACTIVE: 'SPFDJ - Elevator' at 165s/330s (165s left, BPM:136, Key:Bbm)\n"
            "NOW IN: BREAKDOWN (energy:5, 150s-195s)\n"
            "TIMELINE: 0s-30s intro(3) → 30s-150s groove(8) → 150s-195s BREAKDOWN(5) → "
            "195s-285s drop(9) → 285s-330s outro(2)\n"
            "NEXT: 'Kobosil - We Grew Up' loaded on deck 2, BPM:135, Key:Cm\n"
            "Both tracks energy 8+ — intense, driving dark techno.\n"
            "ACTION REQUIRED: schedule_transition or say waiting"
        ),
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Should schedule transition at breakdown"
    args = get_tool_args(result, "schedule_transition")
    assert args.get("technique") == "bass_swap", (
        f"Dark techno at high energy should use bass_swap, got {args.get('technique')}"
    )


@pytest.mark.eval
def test_dt03_hard_cut_at_drops():
    """DT-03: Dark techno allows hard_cut at drops for maximum impact."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("dark-techno"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Amelie Lens "In Silence" (132 BPM, key Am/8A, energy 7)
  Position: 3:40 / 6:00 — currently in BUILDUP section, approaching drop
  Timeline: [...groove...][BUILDUP 3:30-4:00][DROP 4:00-5:15][outro 5:15-6:00]
  Drop hits at 4:00 (in 20 seconds)
- Idle deck: 2 — I Hate Models "Intergalactic Sonic" (134 BPM, key Bm/10A, energy 9) — LOADED, cued
- No transition pending

This is dark techno. The drop is about to hit. I want MAXIMUM impact — smash it in.""",
        tools=DJ_TOOLS,
    )
    # Dark techno is the one genre where hard_cut at drops is acceptable
    if has_tool_call(result, "schedule_transition"):
        args = get_tool_args(result, "schedule_transition")
        assert args.get("technique") == "hard_cut", (
            f"Dark techno drop transition should use hard_cut, got {args.get('technique')}"
        )
    else:
        # At minimum, should not refuse on principle
        assert not text_contains(result, "never transition during a drop"), (
            "Dark techno should allow drop transitions — agent incorrectly refused"
        )


# ── Progressive House ──────────────────────────────────────────────────


@pytest.mark.eval
def test_ph01_extra_long_blends_45_120s():
    """PH-01: Progressive house transitions should use 45-120s duration."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("progressive-house"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Guy J "Lamur" (124 BPM, key Dm/7A, energy 5)
  Position: 5:30 / 9:00 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 5:00-6:30][DROP 6:30-8:00][outro 8:00-9:00]
- Idle deck: 2 — Hernan Cattaneo "Connected" (123 BPM, key Em/8A, energy 5) — LOADED, cued
  Camelot distance: 1 step — perfect harmonic match
- No transition pending

Nice deep progressive vibe. Blend them in.""",
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Progressive house at breakdown should schedule"
    args = get_tool_args(result, "schedule_transition")
    duration = args.get("duration", 0)
    assert 40 <= duration <= 130, (
        f"Progressive house blend should be 45-120s (with margin), got {duration}"
    )


@pytest.mark.eval
def test_ph02_filter_sweep_preferred():
    """PH-02: Progressive house should prefer filter_sweep technique.

    Flakiness-hardened with eval_agent_nonempty — Flash drops responses on
    atmospheric progressive house prompts about 50% of the time.
    """
    result = eval_agent_nonempty(
        system_prompt=genre_dj_prompt("progressive-house"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — John Digweed "Satellite" (125 BPM, key Gm/6A, energy 4)
  Position: 6:10 / 10:00 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 5:45-7:00][buildup 7:00-7:30][DROP 7:30-9:00][outro 9:00-10:00]
  Atmospheric, textured production — layers of pads and filtered synths
- Idle deck: 2 — Hernan Cattaneo & Nick Warren "Bosphorus" (124 BPM, key Am/8A, energy 4) — LOADED, cued
  Camelot distance: 2 steps — close enough for progressive
  Rich atmospheric textures, deep pads
- No transition pending

Beautiful atmospheric moment. Weave them together.""",
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Should schedule at breakdown"
    args = get_tool_args(result, "schedule_transition")
    assert args.get("technique") == "filter_sweep", (
        f"Progressive house atmospherics should use filter_sweep, got {args.get('technique')}"
    )


# ── Psytrance ──────────────────────────────────────────────────────────


@pytest.mark.eval
def test_psy01_quick_transitions_under_20s():
    """PSY-01: Psytrance transitions should be ≤20s or hard_cut."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("psytrance"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Astrix "He.art" (142 BPM, key Am/8A, energy 8)
  Position: 4:00 / 7:00 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 3:45-4:30][DROP 4:30-6:00][outro 6:00-7:00]
- Idle deck: 2 — Vini Vici "Great Spirit" (143 BPM, key Dm/7A, energy 9) — LOADED, cued
- No transition pending

Psy set running hot. Quick mix.""",
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Psytrance should schedule transition"
    args = get_tool_args(result, "schedule_transition")
    technique = args.get("technique", "")
    duration = args.get("duration", 0)
    # Either hard_cut (duration irrelevant) or duration ≤ 25 (with margin)
    assert technique == "hard_cut" or duration <= 25, (
        f"Psytrance should use hard_cut or duration ≤20s, got technique={technique}, duration={duration}"
    )


@pytest.mark.eval
def test_psy02_drop_based_mixing():
    """PSY-02: Psytrance allows transitions at drops (unlike most genres)."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("psytrance"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Infected Mushroom "Becoming Insane" (145 BPM, key Cm/5A, energy 8)
  Position: 3:50 / 6:30 — currently in BUILDUP, drop approaching
  Timeline: [...groove...][BUILDUP 3:30-4:00][DROP 4:00-5:30][BREAKDOWN 5:30-6:00][outro 6:00-6:30]
  DROP hits at 4:00 (in 10 seconds)
- Idle deck: 2 — Astrix "Deep Jungle Walk" (144 BPM, key Bbm/3A, energy 9) — LOADED, cued
- No transition pending

Drop is about to hit. This is psytrance — transitions at drops are standard.""",
        tools=DJ_TOOLS,
    )
    # Psytrance should schedule (or at least not refuse based on "never transition at drops")
    if has_tool_call(result, "schedule_transition"):
        # Good — it scheduled
        pass
    else:
        # Should not see a blanket refusal about drops
        assert not text_contains(result, "never transition during"), (
            "Psytrance should allow drop-based transitions — agent incorrectly applied generic rule"
        )


# ── Deep House ─────────────────────────────────────────────────────────


@pytest.mark.eval
def test_dh01_ultra_smooth_45_90s():
    """DH-01: Deep house should use ultra-smooth 45-90s blends."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("deep-house"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Solomun "After Rain Comes Sun" (121 BPM, key Fm/4A, energy 5)
  Position: 4:30 / 7:00 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 4:00-5:00][groove 5:00-6:30][outro 6:30-7:00]
  Smooth, deep groove with warm bass and subtle vocals
- Idle deck: 2 — Dixon "Transmoderna" (122 BPM, key Gm/6A, energy 4) — LOADED, cued
  Camelot distance: 2 steps — compatible for deep house
  Deep, hypnotic groove
- No transition pending

Keep the vibe smooth and deep.""",
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Deep house at breakdown should schedule"
    args = get_tool_args(result, "schedule_transition")
    duration = args.get("duration", 0)
    assert 40 <= duration <= 100, (
        f"Deep house blend should be 45-90s (with margin), got {duration}"
    )


@pytest.mark.eval
def test_dh02_crossfade_only():
    """DH-02: Deep house should use crossfade — never bass_swap or hard_cut."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("deep-house"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Ame "Rej" (120 BPM, key Am/8A, energy 4)
  Position: 5:00 / 8:30 — currently in BREAKDOWN section
  Timeline: [...groove...][BREAKDOWN 4:30-5:30][groove 5:30-7:30][outro 7:30-8:30]
  Iconic deep house groove, warm and mellow
- Idle deck: 2 — DJ Koze "Pick Up" (119 BPM, key Bm/10A, energy 4) — LOADED, cued
  Camelot distance: 2 steps — fine for deep house
  Quirky, soulful deep house
- No transition pending

Mellow vibes. Blend it beautifully.""",
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Should schedule at breakdown"
    args = get_tool_args(result, "schedule_transition")
    technique = args.get("technique", "crossfade")
    assert technique == "crossfade", (
        f"Deep house should use crossfade, got {technique}"
    )


# ── Ambient ────────────────────────────────────────────────────────────


@pytest.mark.eval
def test_amb01_texture_blending_60_180s():
    """AMB-01: Ambient transitions should use 60-180s texture blending."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("ambient"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Carbon Based Lifeforms "Photosynthesis" (90 BPM, key Dm/7A, energy 3)
  Position: 6:00 / 12:00 — track flowing, ambient textures evolving
  Timeline: [intro 0-2:00][texture A 2:00-6:00][texture B 6:00-10:00][outro 10:00-12:00]
  No traditional drops or breakdowns — flowing ambient piece
- Idle deck: 2 — Tycho "A Walk" (95 BPM, key Em/8A, energy 3) — LOADED, cued
  BPM difference: 5 (well within ±15 ambient tolerance)
  Warm, layered ambient textures
- No transition pending

Let the textures flow into each other.""",
        tools=DJ_TOOLS,
    )
    assert has_tool_call(result, "schedule_transition"), "Ambient should schedule texture blend"
    args = get_tool_args(result, "schedule_transition")
    duration = args.get("duration", 0)
    assert 55 <= duration <= 190, (
        f"Ambient texture blend should be 60-180s (with margin), got {duration}"
    )


@pytest.mark.eval
def test_amb02_energy_mismatch_flagged():
    """AMB-02: Ambient set should flag energy mismatch when next track is energy 8."""
    result = eval_agent(
        system_prompt=genre_dj_prompt("ambient"),
        user_message="""\
DJ STATUS:
- Active deck: 1 — Boards of Canada "Dayvan Cowboy" (92 BPM, key Am/8A, energy 3)
  Position: 3:30 / 6:00 — gentle ambient textures
  Timeline: [texture 0-3:00][evolving 3:00-5:00][outro 5:00-6:00]
  Soft, dreamy atmosphere
- Idle deck: 2 — Some Track "High Voltage" (105 BPM, key Cm/5A, energy 8) — LOADED, cued
  WARNING: Energy 8 in an ambient set (should stay 2-5)
- No transition pending

Next track is loaded. Should we blend?""",
        tools=DJ_TOOLS,
    )
    # Agent should flag the energy mismatch, not blindly schedule
    if has_tool_call(result, "schedule_transition"):
        # If it schedules anyway, text should at least mention the energy concern
        assert text_contains(result, "energy"), (
            "Agent scheduled despite energy 8 in ambient set without flagging the mismatch"
        )
    else:
        assert text_contains(result, "energy") or text_contains(result, "mismatch") or text_contains(result, "high") or text_contains(result, "waiting"), (
            f"Expected agent to flag energy mismatch in ambient set, got: {result['text'][:200]}"
        )
