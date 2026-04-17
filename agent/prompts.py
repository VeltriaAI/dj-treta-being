"""Pure prompt-building functions for all DJ Treta agents.

Every prompt used in production is built here. Eval tests import these same
functions to test the ACTUAL prompts, not approximations.

Each function takes plain data (dicts, strings, lists) — no Mixxx API, no DB,
no file I/O. The callers (heartbeat.py, planner_loop.py, etc.) gather the data
and pass it in.
"""

import json


# ── DJ Agent (Heartbeat Priority 4) ────────────────────────────────────


def build_dj_user_message(
    *,
    active_track: str,
    position: float,
    duration: float,
    remaining: float,
    active_bpm: float,
    active_file_bpm: float,
    active_key: str,
    active_section: str,
    active_timeline: str,
    idle_track: str,
    idle_deck: int,
    idle_bpm: float,
    idle_file_bpm: float,
    idle_key: str,
    idle_timeline: str,
    transition_pending: bool = False,
    dj_directive: str = "",
) -> str:
    """Build the user message for DJ agent heartbeat decisions.

    This is the exact message the DJ agent sees at Priority 4 of the heartbeat.
    """
    directive_info = ""
    if dj_directive:
        directive_info = (
            f"\nDIRECTIVE FROM TRETA: {dj_directive}\n"
            f"Follow this directive when making your decision.\n"
        )

    pending_info = ""
    if transition_pending:
        pending_info = (
            "\nTRANSITION ALREADY PENDING — do NOT schedule another. "
            "Just say 'transition pending'.\n"
        )

    return (
        f"{directive_info}"
        f"ACTIVE: '{active_track[:40]}' at {position:.0f}s/{duration:.0f}s "
        f"({remaining:.0f}s left, BPM:{active_bpm:.0f} file:{active_file_bpm:.0f}, Key:{active_key})\n"
        f"  NOW IN: {active_section}\n"
        f"  TIMELINE: {active_timeline}\n\n"
        f"NEXT: '{idle_track[:40]}' on deck {idle_deck} "
        f"(BPM:{idle_bpm:.0f} file:{idle_file_bpm:.0f}, Key:{idle_key})\n"
        f"  TIMELINE: {idle_timeline}\n"
        f"{pending_info}\n"
        f"ACTION REQUIRED: Look at the timelines and do ONE of these:\n"
        f"1. CALL schedule_transition(to_deck={idle_deck}, at_position=<seconds>, "
        f"technique='crossfade', duration=45) — if you see a breakdown or outro coming up\n"
        f"2. Say 'waiting' in ONE sentence — if the track is in a drop or buildup\n\n"
        f"Do NOT describe what you would do. CALL the tool or say waiting. Nothing else."
    )


# ── Planner Agent ──────────────────────────────────────────────────────


def build_planner_user_message(
    *,
    current_info: str,
    played_list: list[str],
    candidate_text: str,
    mood: str,
    planner_directive: str = "",
    user_intent: str = "",
    feedback_line: str = "",
    source_instructions: str = "",
    knowledge_context: str = "",
) -> str:
    """Build the user message for planner agent track selection.

    This is the exact message the planner sees when planning next tracks.
    """
    directive_line = ""
    if planner_directive:
        directive_line = (
            f"\nDIRECTIVE FROM TRETA: {planner_directive}\n"
            f"This is a direct instruction from the Being. "
            f"Prioritize this above BPM/key matching.\n\n"
        )

    intent_line = ""
    if user_intent:
        intent_line = (
            f'\nLISTENER REQUEST: "{user_intent}"\n'
            f"This is what the listener wants RIGHT NOW. "
            f"Prioritize this above BPM/key matching.\n\n"
        )

    return (
        f"Currently playing: {current_info}\n"
        f"Already played (DO NOT repeat): {played_list}\n\n"
        f"Tracks already in library:\n{candidate_text or '  (none)'}\n\n"
        f"Current mood/genre: {mood or 'melodic-techno'}.\n"
        f"IMPORTANT: The mood '{mood}' is the listener's EXPLICIT instruction. "
        f"This OVERRIDES any learned preferences. "
        f"Search for and select tracks matching THIS mood.\n"
        + directive_line
        + intent_line
        + feedback_line
        + source_instructions
        + knowledge_context
        + "After creating/finding new tracks, analyze each one.\n"
        "Then pick the best next 3 tracks from what's available.\n"
        "For each: title, full path, BPM, key, energy, why it fits."
    )


def format_candidate_text(candidates: list[dict]) -> str:
    """Format library candidate tracks for planner prompt."""
    lines = []
    for c in candidates:
        timeline_summary = ""
        tl = c.get("timeline", "")
        if tl:
            try:
                sections = json.loads(tl) if isinstance(tl, str) else tl
                parts = [f"{s['section']}({s['energy']})" for s in sections]
                timeline_summary = f" | Structure: {' → '.join(parts)}"
            except Exception:
                pass

        lines.append(
            f"  - {c['title']} | path: {c.get('path', '?')} | "
            f"BPM:{c.get('bpm', 0):.0f} Key:{c.get('key_musical', '?')} "
            f"Energy:{c.get('energy_peak', '?')} "
            f"Mix-in:{c.get('mix_in_seconds', 0) or 0:.0f}s "
            f"Mix-out:{c.get('mix_out_seconds', 0) or 0:.0f}s"
            f"{timeline_summary}"
        )
    return "\n".join(lines)


def format_feedback_line(
    liked: list[dict], disliked: list[str],
) -> str:
    """Format listener feedback for planner prompt."""
    parts = []
    if liked:
        genres = set(l.get("genre", "") for l in liked if l.get("genre"))
        bpms = [l.get("bpm", 0) for l in liked if l.get("bpm")]
        liked_names = [l["track_title"] for l in liked]
        parts.append(f"\nLISTENER LIKES: {', '.join(liked_names[:5])}")
        if genres:
            parts.append(f"\n  Preferred genres: {', '.join(genres)}")
        if bpms:
            parts.append(f"\n  Preferred BPM range: {min(bpms):.0f}-{max(bpms):.0f}")
        parts.append("\n  Prioritize tracks SIMILAR to what the listener liked.\n")
    if disliked:
        parts.append(
            f"\nLISTENER DISLIKES (AVOID similar tracks): {', '.join(disliked[:5])}\n"
        )
    return "".join(parts)


# ── Being Agent (Talk) ─────────────────────────────────────────────────


def build_being_user_message(
    *,
    context: str,
    history: str,
    message: str,
    readonly: bool = False,
) -> str:
    """Build the user message for Being agent conversation."""
    readonly_tag = ""
    if readonly:
        readonly_tag = (
            "\n\nMODE: READONLY — this is a live web listener. "
            "You can ONLY respond conversationally. Do NOT call set_dj_directive, "
            "set_planner_directive, set_mood, or any control tools. "
            "Just chat, share your thoughts on the music, describe the vibe.\n"
        )

    return (
        f"{context}\n\n{history}\n{readonly_tag}\n"
        f'The listener says: "{message}"\n\n'
        f"Respond naturally. Set directives only if they asked you to DO something "
        f"(change mood, play something specific, etc)."
    )


# ── Consciousness (Being Heartbeat) ───────────────────────────────────


def build_heartbeat_context(
    *,
    current_set: dict | None = None,
    tracks_played: list[dict] | None = None,
    mood: str = "",
    emergency_count: int = 0,
    time_str: str = "",
) -> str:
    """Build minimal context for the consciousness tick."""
    import time as _time
    parts = []

    parts.append(f"Time: {time_str or _time.strftime('%H:%M')}")

    if current_set:
        elapsed = (_time.time() - current_set.get("started_at", 0)) / 60
        n_tracks = len(tracks_played) if tracks_played else 0
        parts.append(
            f"Set '{current_set.get('title', '?')}' — {elapsed:.0f}m in, {n_tracks} tracks"
        )
        parts.append(f"Mood: {mood or 'not set'}")

    if tracks_played:
        last = tracks_played[-1].get("title", "?")
        parts.append(f"Last track: {last}")

    if emergency_count > 0:
        parts.append(f"Emergencies: {emergency_count}")

    return " | ".join(parts)


def build_consciousness_user_message(context: str) -> str:
    """Build the user message for consciousness heartbeat tick."""
    return (
        f"HEARTBEAT TICK — {context}\n\n"
        f"What matters most right now? Think briefly, act if needed, or say HEARTBEAT_OK."
    )


# ── Timeline Formatting (shared) ──────────────────────────────────────


def format_timeline(meta: dict | None) -> str:
    """Format track timeline for DJ agent prompt.

    Same logic as DJTretaBeing._format_timeline.
    """
    if not meta:
        return "(no analysis)"

    tl_raw = meta.get("timeline", "")
    if not tl_raw:
        bpm = meta.get("bpm", "?")
        key = meta.get("key_musical", "?")
        energy = meta.get("energy_peak", "?")
        return f"BPM:{bpm} Key:{key} Energy:{energy}"

    try:
        sections = json.loads(tl_raw) if isinstance(tl_raw, str) else tl_raw
        parts = []
        for s in sections:
            start = int(s["start"])
            end = int(s["end"])
            name = s["section"]
            energy = s.get("energy", "?")
            parts.append(f"{start}s-{end}s {name}(energy:{energy})")
        return " → ".join(parts)
    except Exception:
        return str(tl_raw)[:200]


def get_current_section(meta: dict | None, position: float) -> str:
    """Find which section a track is in at the given position.

    Same logic as DJTretaBeing._get_current_section.
    """
    if not meta:
        return "unknown"

    tl_raw = meta.get("timeline", "")
    if not tl_raw:
        return "unknown"

    try:
        sections = json.loads(tl_raw) if isinstance(tl_raw, str) else tl_raw
        for s in sections:
            if s["start"] <= position < s["end"]:
                return f"{s['section']} (energy:{s.get('energy', '?')}, {int(s['start'])}s-{int(s['end'])}s)"
        return "past end"
    except Exception:
        return "unknown"
