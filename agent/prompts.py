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
    playlist: dict | None = None,
    mood_profile: dict | None = None,
    external_decks: list[int] | None = None,
) -> str:
    """Build the user message for DJ agent heartbeat decisions.

    v8 Phase 4: DJ receives the planner's ranked playlist as advisory input
    and a resolved mood profile. DJ has final authority — may load any of
    the ranked candidates on the idle deck (if it's empty / almost done)
    or override with a fresh pick, then schedule a transition at the right
    musical moment.

    Signals (idle_needs_load / user_skip / set_ending) are NOT passed to
    DJ — Python executes those directly. Keeping the prompt lean restores
    Flash's reliability on creative transition-timing decisions, which was
    lost when Phase A2 loaded the prompt with conditional signal branches.
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

    profile_line = ""
    if mood_profile:
        slug = mood_profile.get("canonical_slug") or ""
        vibe = ", ".join(mood_profile.get("vibe_keywords", [])[:4])
        profile_line = f"Mood profile: {slug} | vibe: {vibe}\n"

    # Phase 7 co-being mode: external Beings may have claimed one or more decks
    # via MCP. DJ Treta must not schedule transitions onto those decks.
    ownership_line = ""
    if external_decks:
        ownership_line = (
            f"EXTERNAL DECK OWNERSHIP: deck(s) {external_decks} claimed by "
            f"co-being(s); do NOT schedule transitions targeting these decks "
            f"and do NOT load tracks onto them.\n"
        )

    # Compact playlist render — top 5 ranks, one-liner each.  Path is
    # included explicitly so DJ can pass it verbatim to load_track.
    playlist_block = ""
    if playlist and playlist.get("tracks"):
        lines = ["Planner's ranked suggestions (advisory; you have final say):"]
        for t in sorted(playlist["tracks"], key=lambda x: x.get("rank", 999))[:5]:
            rank = t.get("rank")
            title = (t.get("title") or "")[:50]
            bpm = t.get("bpm") or ""
            key = t.get("key_camelot") or ""
            energy = t.get("energy") or ""
            reason = (t.get("reason") or "")[:80]
            path = t.get("path") or ""
            lines.append(
                f"  #{rank}: {title} | BPM {bpm} {key} e{energy} — {reason}"
            )
            if path:
                lines.append(f"       path: {path}")
        playlist_block = "\n".join(lines) + "\n\n"

    return (
        f"{directive_info}"
        f"{profile_line}"
        f"{ownership_line}"
        f"ACTIVE: '{active_track[:40]}' at {position:.0f}s/{duration:.0f}s "
        f"({remaining:.0f}s left, BPM:{active_bpm:.0f} file:{active_file_bpm:.0f}, Key:{active_key})\n"
        f"  NOW IN: {active_section}\n"
        f"  TIMELINE: {active_timeline}\n\n"
        f"NEXT: '{idle_track[:40]}' on deck {idle_deck} "
        f"(BPM:{idle_bpm:.0f} file:{idle_file_bpm:.0f}, Key:{idle_key})\n"
        f"  TIMELINE: {idle_timeline}\n\n"
        f"{playlist_block}"
        f"{pending_info}\n"
        f"Decide now. Your options:\n"
        f"  - If ACTIVE is in a breakdown or outro (and NEXT is ready on "
        f"deck {idle_deck}), invoke the schedule_transition tool. Pick a "
        f"technique that fits the BPM/key/energy gap.\n"
        f"  - If the idle deck is empty or loaded with the wrong track, "
        f"invoke the load_track tool with the best path from the playlist "
        f"above.\n"
        f"  - Otherwise (active track is mid-drop, mid-buildup, or too "
        f"early), call defer_decision(seconds=30) so the heartbeat asks "
        f"you again in 30s.\n\n"
        f"Respond with EXACTLY ONE tool call. Never respond in plain text. "
        f"If unsure, defer_decision(60) is always safe."
    )


# ── Planner Agent ──────────────────────────────────────────────────────


def build_planner_v8_message(
    *,
    current_info: str,
    played_list: list,
    library: list,
    mood_profile: dict | None,
    mood: str = "",
    planner_directive: str = "",
    user_intent: str = "",
    feedback_line: str = "",
    external_decks: list[int] | None = None,
) -> str:
    """Build the v8 planner prompt — asks for STRICT JSON output.

    The planner LLM receives the full analyzed library, current state, and
    mood profile, and must return a PlaylistV1 JSON:

        {"planned_at": <ts>, "mood_snapshot": "...", "reasoning_summary": "...",
         "tracks": [ {rank, path, title, bpm, key_camelot, energy, reason,
                      transition_hint: {technique, duration, at_section}}, ... ]}

    No SQL filter, no pre-selected candidates — the LLM picks from the
    provided library based on mood/BPM/energy/vibe.
    """
    import time as _time
    import json as _json

    mood_slug = (mood_profile or {}).get("canonical_slug") or mood or "melodic-techno"

    profile_line = ""
    if mood_profile:
        bpm = mood_profile.get("bpm_range") or []
        energy = mood_profile.get("energy_range") or []
        vibe = mood_profile.get("vibe_keywords") or []
        conf = mood_profile.get("confidence", 0.0)
        bits = [f"canonical={mood_slug}"]
        if bpm:
            bits.append(f"BPM {bpm[0]}-{bpm[1]}")
        if energy:
            bits.append(f"energy {energy[0]}-{energy[1]}/10")
        if vibe:
            bits.append("vibe: " + ", ".join(vibe[:5]))
        profile_line = (
            f"\nResolved mood profile: {' | '.join(bits)} "
            f"(confidence {conf:.2f})."
        )

    directive_line = ""
    if planner_directive:
        directive_line = (
            f"\nDIRECTIVE FROM TRETA: {planner_directive}\n"
            f"Prioritize this above BPM/key matching."
        )

    intent_line = ""
    if user_intent:
        intent_line = (
            f'\nLISTENER REQUEST: "{user_intent}"\n'
            f"Prioritize this above BPM/key matching."
        )

    # Phase 7 co-being mode: one or more decks claimed by external Beings.
    # Planner stays advisory for treta-owned decks only.
    ownership_line = ""
    if external_decks:
        ownership_line = (
            f"\nCO-BEING MODE: deck(s) {external_decks} currently claimed by "
            f"external being(s). Plan only for treta-owned decks; treta will "
            f"only transition into her own decks."
        )

    # Compact library snapshot — JSON array of key fields, one line each.
    # LLM picks tracks BY PATH from this list; do not invent paths.
    library_json = _json.dumps(
        [
            {
                "path": t.get("path"),
                "title": t.get("title") or (
                    f"{t.get('canonical_artist', '')} - {t.get('canonical_song', '')}"
                ).strip(" -"),
                "bpm": t.get("bpm"),
                "key_camelot": t.get("key_camelot"),
                "energy": t.get("energy_peak"),
                "genre": t.get("genre"),
                "mood": t.get("mood"),
            }
            for t in (library or [])
        ],
        separators=(",", ":"),
    )

    schema = (
        '{"planned_at":<float>, "mood_snapshot":"<canonical_slug>", '
        '"reasoning_summary":"<one paragraph>", '
        '"tracks":[{"rank":<int>, "path":"<from library>", "title":"...", '
        '"bpm":<float>, "key_camelot":"<e.g. 8A>", "energy":<1-10>, '
        '"reason":"<why this fits>", '
        '"transition_hint":{"technique":"crossfade|bass_swap|filter_sweep|echo_out|hard_cut", '
        '"duration":<10-90>, "at_section":"breakdown|outro|build|drop|intro"}}]}'
    )

    return (
        "You are DJ Treta's planning brain. Given the state below, return "
        "a ranked playlist of the next 5 candidate tracks as STRICT JSON.\n\n"
        f"Currently playing: {current_info}\n"
        f"Already played (DO NOT repeat): {played_list}\n"
        f"Current mood: {mood_slug}."
        + profile_line
        + directive_line
        + intent_line
        + ownership_line
        + feedback_line
        + "\n\nAvailable library (pick paths ONLY from this list):\n"
        + library_json
        + "\n\nReturn JSON matching this schema (no markdown fences, no prose):\n"
        + schema
        + "\n\nRules:\n"
        "- Return exactly 5 candidates ranked 1 (best) to 5.\n"
        "- Use `path` values EXACTLY as they appear in the library list.\n"
        "- Rank 1 should fit the current track BPM / key / energy best.\n"
        "- Lower ranks offer valid alternates if rank 1 is unavailable.\n"
        "- Never repeat a title from the played list.\n"
        "- reasoning_summary: one paragraph on your overall arc strategy.\n"
        "- Each track's `reason` should explain mood/BPM/energy fit in one sentence.\n"
        "- If library is thin and you can return <5 candidates, do — mention in reasoning_summary.\n"
        "Return JSON ONLY."
    )


def build_planner_v9_message(
    *,
    current_info: str,
    current_timeline: str = "",
    played_list: list,
    merged_candidates: list,
    mood_profile: dict | None,
    mood: str = "",
    planner_directive: str = "",
    user_intent: str = "",
    feedback_line: str = "",
    external_decks: list[int] | None = None,
) -> str:
    """v9 planner prompt: dataset-driven candidates, current track full timeline.

    `merged_candidates` is a list of MergedCandidate-shaped dicts:
        {rank, mbid, video_id, title, artist, bpm, key_camelot, energy,
         year, genre, subgenre, downloaded, path, similarity_score, reason}

    Python has already surfaced the relevant universe (mood filter + similarity
    seed + local merge). LLM picks the top 5 ranked by DJ-arc fit.
    """
    import json as _json
    import time as _time

    mood_slug = (mood_profile or {}).get("canonical_slug") or mood or "melodic-techno"

    profile_line = ""
    if mood_profile:
        bpm = mood_profile.get("bpm_range") or []
        energy = mood_profile.get("energy_range") or []
        vibe = mood_profile.get("vibe_keywords") or []
        conf = mood_profile.get("confidence", 0.0)
        bits = [f"canonical={mood_slug}"]
        if bpm:
            bits.append(f"BPM {bpm[0]}-{bpm[1]}")
        if energy:
            bits.append(f"energy {energy[0]}-{energy[1]}/10")
        if vibe:
            bits.append("vibe: " + ", ".join(vibe[:5]))
        profile_line = (
            f"\nResolved mood profile: {' | '.join(bits)} "
            f"(confidence {conf:.2f})."
        )

    directive_line = ""
    if planner_directive:
        directive_line = (
            f"\nDIRECTIVE FROM TRETA: {planner_directive}\n"
            f"Prioritize this above BPM/key matching."
        )

    intent_line = ""
    if user_intent:
        intent_line = (
            f'\nLISTENER REQUEST: "{user_intent}"\n'
            f"Prioritize this above BPM/key matching."
        )

    timeline_line = ""
    if current_timeline:
        timeline_line = f"\nCurrent track timeline: {current_timeline}"

    # Phase 7 co-being mode: one or more decks claimed by external Beings.
    ownership_line = ""
    if external_decks:
        ownership_line = (
            f"\nCO-BEING MODE: deck(s) {external_decks} currently claimed by "
            f"external being(s). Plan only for treta-owned decks."
        )

    # Compact candidate render — include `downloaded` flag + mbid/video_id
    # so the LLM can point at dataset tracks the library will fetch on demand.
    def _strip_artist_prefix(artist: str, title: str) -> str:
        # Local titles often include the artist ("Emilie Nana - Wild Sensations")
        # while dataset titles are clean ("Wild Sensations"). Normalize so the
        # render doesn't double the artist.
        if not artist or not title:
            return title
        prefix = f"{artist.lower().strip()} - "
        if title.lower().lstrip().startswith(prefix):
            return title[len(prefix):].lstrip()
        return title

    candidate_lines = []
    for c in merged_candidates:
        rank = c.get("rank", "?")
        raw_title = (c.get("title") or "")
        artist = (c.get("artist") or c.get("artist_name") or "")[:30]
        title = _strip_artist_prefix(artist, raw_title)[:60]
        bpm = c.get("bpm") or c.get("bpm_hint") or ""
        key = c.get("key_camelot") or ""
        energy = c.get("energy") or ""
        year = c.get("year") or ""
        sub = c.get("subgenre") or ""
        dl = "LOCAL" if c.get("downloaded") else "DATASET"
        sim = c.get("similarity_score")
        sim_str = f" sim={sim:.2f}" if sim is not None else ""
        ident = c.get("path") or f"mbid:{c.get('mbid','')[:8]}|vid:{c.get('video_id','')}"
        candidate_lines.append(
            f"#{rank} [{dl}]{sim_str} {artist} - {title} "
            f"| {bpm}bpm {key} e{energy} {year} {sub}"
        )
        candidate_lines.append(f"    ref: {ident}")

    candidates_block = "\n".join(candidate_lines) if candidate_lines else "  (empty)"

    schema = (
        '{"planned_at":<float>, "mood_snapshot":"<canonical_slug>", '
        '"reasoning_summary":"<paragraph>", '
        '"tracks":[{"rank":<int>, "downloaded":<bool>, '
        '"path":"<local path if downloaded>", '
        '"mbid":"<from candidates>", "video_id":"<from candidates>", '
        '"title":"...", "bpm":<float>, "key_camelot":"<e.g. 8A>", '
        '"energy":<1-10>, "reason":"<why this fits>", '
        '"transition_hint":{"technique":"crossfade|bass_swap|filter_sweep|echo_out|hard_cut", '
        '"duration":<10-90>, "at_section":"breakdown|outro|build|drop|intro"}}]}'
    )

    return (
        "You are DJ Treta's planning brain. The library below is a merged "
        "universe of LOCAL (downloaded, analyzed) and DATASET (known but "
        "not yet on disk) tracks. Return a ranked playlist of the next 5 "
        "candidates as STRICT JSON.\n\n"
        f"Currently playing: {current_info}"
        + timeline_line
        + f"\nAlready played (DO NOT repeat): {played_list}\n"
        f"Current mood: {mood_slug}."
        + profile_line
        + directive_line
        + intent_line
        + ownership_line
        + feedback_line
        + "\n\nCandidate universe (pick by #rank; use path for LOCAL, "
        "mbid+video_id for DATASET):\n"
        + candidates_block
        + "\n\nReturn JSON matching this schema (no markdown fences):\n"
        + schema
        + "\n\nRules:\n"
        "- Exactly 5 candidates, ranks 1..5.\n"
        "- `downloaded` must match the candidate's LOCAL/DATASET flag.\n"
        "- For LOCAL: copy `path` from the ref line verbatim.\n"
        "- For DATASET: copy `mbid` and `video_id` from the ref line; "
        "leave `path` as empty string.\n"
        "- Prefer LOCAL for rank 1 unless a DATASET track is clearly a "
        "better musical fit (similarity, BPM, key, energy arc).\n"
        "- Never repeat a title from the played list.\n"
        "- transition_hint.technique should respect BPM gap rules: "
        "≤3 BPM → crossfade, 4-6 → echo_out, ≥8 or genre change → hard_cut.\n"
        "- reasoning_summary: one paragraph on arc strategy.\n"
        "Return JSON ONLY."
    )


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
    mood_profile: dict = None,
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

    # Effective mood slug: if resolver produced a profile, prefer its
    # canonical_slug; else fall back to the raw mood (or default).
    mood_slug = (mood_profile or {}).get("canonical_slug") or mood or "melodic-techno"

    profile_line = ""
    if mood_profile:
        bpm = mood_profile.get("bpm_range") or []
        energy = mood_profile.get("energy_range") or []
        vibe = mood_profile.get("vibe_keywords") or []
        conf = mood_profile.get("confidence", 0.0)
        bits = [f"canonical={mood_slug}"]
        if bpm:
            bits.append(f"BPM {bpm[0]}-{bpm[1]}")
        if energy:
            bits.append(f"energy {energy[0]}-{energy[1]}/10")
        if vibe:
            bits.append("vibe: " + ", ".join(vibe[:5]))
        profile_line = (
            f"Resolved mood profile: {' | '.join(bits)} "
            f"(confidence {conf:.2f}).\n"
        )

    return (
        f"Currently playing: {current_info}\n"
        f"Already played (DO NOT repeat): {played_list}\n\n"
        f"Tracks already in library:\n{candidate_text or '  (none)'}\n\n"
        f"Current mood/genre: {mood_slug}.\n"
        + profile_line
        + f"IMPORTANT: The mood '{mood_slug}' is the listener's EXPLICIT instruction. "
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
