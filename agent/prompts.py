"""Pure prompt-building functions for all DJ Treta agents.

Every prompt used in production is built here. Eval tests import these same
functions to test the ACTUAL prompts, not approximations.

Each function takes plain data (dicts, strings, lists) — no Mixxx API, no DB,
no file I/O. The callers (heartbeat.py, planner_loop.py, etc.) gather the data
and pass it in.
"""

import json


# ── Notebook slice (v11 Phase 1) ────────────────────────────────────────
# The Notebook (agent/notebook.py) is the durable append-only event log. This
# renders a *derived*, ≤3-line digest of the live workspace for prompt
# injection — NOT a raw event dump. Pure: takes plain dicts/lists (a now_view
# and a tail), no Notebook/Session/Mixxx access. Returns "" when nothing
# salient, so the caller can thread it like room_sense_line/feedback_line and
# the slot collapses to nothing on a quiet workspace.


def render_notebook_slice(
    now_view: dict, tail: list, max_lines: int = 3
) -> str:
    """Render ≤`max_lines` DERIVED workspace lines from a notebook now_view + tail.

    Args:
        now_view: shape from Notebook.now_view() —
            {now_playing, up_next, room_sense, mood, recent}.
        tail: recent events (e.g. Notebook.tail(8, kinds=(...))) — each a
            dict with at least {kind, author, payload}.
        max_lines: hard ceiling on rendered lines (default 3).

    Returns a string like
        "WORKSPACE: planner held energy; DJ cued X; crowd steady"
    or "" when nothing salient — never raises (defensive on every access).
    """
    try:
        nv = now_view if isinstance(now_view, dict) else {}
        events = tail if isinstance(tail, list) else []

        bits: list[str] = []

        # 1) Most-recent DECISION (planner/dj intent) — the "why".
        decision = None
        for e in reversed(events):
            if isinstance(e, dict) and e.get("kind") == "decision":
                decision = e
                break
        if decision:
            p = decision.get("payload")
            author = decision.get("author") or "agent"
            summary = ""
            if isinstance(p, dict):
                summary = (
                    p.get("summary")
                    or p.get("action")
                    or p.get("reason")
                    or ""
                )
            elif isinstance(p, str):
                summary = p
            summary = str(summary).strip()
            if summary:
                bits.append(f"{author} {summary[:60]}")

        # 2) Most-recent TRANSITION — the "what just happened on the decks".
        transition = None
        for e in reversed(events):
            if isinstance(e, dict) and e.get("kind") == "transition":
                transition = e
                break
        if transition:
            p = transition.get("payload") or {}
            if isinstance(p, dict):
                tech = p.get("technique") or "transition"
                to_deck = p.get("to_deck")
                deck_str = f"→ deck {to_deck}" if to_deck is not None else ""
                bits.append(f"last mix: {tech} {deck_str}".strip())

        # 3) Crowd/room state — prefer a percept, else now_view.room_sense.
        room_bit = ""
        room_sense = nv.get("room_sense")
        if isinstance(room_sense, dict):
            energy = room_sense.get("energy")
            direction = room_sense.get("energyDirection") or "steady"
            if energy is not None:
                try:
                    room_bit = f"crowd {float(energy):.0f}/10 {direction}"
                except (TypeError, ValueError):
                    room_bit = f"crowd {direction}"
        if not room_bit:
            for e in reversed(events):
                if isinstance(e, dict) and e.get("kind") == "percept":
                    pp = e.get("payload")
                    if isinstance(pp, dict):
                        d = pp.get("energyDirection") or pp.get("direction")
                        if d:
                            room_bit = f"crowd {d}"
                    break
        if room_bit:
            bits.append(room_bit)

        if not bits:
            return ""
        return "WORKSPACE: " + "; ".join(bits[:max_lines])
    except Exception:
        # A notebook-slice fault must never break prompt building.
        return ""


# ── Mood-class → technique-whitelist mapping (Patch A, fix/mood-aware-technique)
# Keyed by canonical_slug emitted by mood_resolver. Slugs are matched
# case-insensitively. Anything not listed falls into the "flowing" default.

CONTINUOUS_ENERGY_SLUGS = {
    "psy-trance", "psytrance", "psy_trance",
    "peak-time", "peak-time-techno", "peak_time_techno",
    "hard-techno", "hard_techno",
    "drum-n-bass", "drum-and-bass", "dnb", "drum_n_bass",
    "hardstyle",
    "big-room", "big_room",
}

MOOD_SHIFT_SLUGS: set[str] = set()  # reserved — currently caller-driven, not slug-driven


def _mood_class_and_allowed(mood_slug: str) -> tuple[str, list[str]]:
    """Return (class_name, allowed_techniques) for a canonical mood slug.

    Three classes:
      - continuous-energy: bass_swap (default), crossfade. echo_out BANNED.
      - flowing/atmospheric: crossfade (default), bass_swap, echo_out.
      - mood-shift: echo_out, hard_cut. (Caller-driven; slug rarely lands here.)
    """
    s = (mood_slug or "").strip().lower()
    if s in CONTINUOUS_ENERGY_SLUGS:
        return ("continuous-energy", ["bass_swap", "crossfade"])
    if s in MOOD_SHIFT_SLUGS:
        return ("mood-shift", ["echo_out", "hard_cut"])
    return ("flowing/atmospheric", ["crossfade", "bass_swap", "echo_out"])


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
    # v9 Tier-2 — pre-computed transition points so DJ doesn't have to
    # invent at_position values from raw timeline strings. All optional;
    # fall back to a generic prompt if the analyzer hasn't run yet.
    active_camelot: str = "",
    active_energy: int | None = None,
    active_mix_in: float | None = None,
    active_mix_out: float | None = None,
    idle_duration: float | None = None,
    idle_camelot: str = "",
    idle_energy: int | None = None,
    idle_mix_in: float | None = None,
    idle_mix_out: float | None = None,
    # v8.2 — caller pre-computes whether idle deck holds a track already in
    # tracks_played. Surfacing this explicitly fixes the observed bug where
    # DJ scheduled crossfades into already-played tracks (replay) because the
    # info was buried in implicit playlist filtering.
    idle_already_played: bool = False,
    # Typed-directive surfacing (atomic-cuddling-manatee plan).
    # When Treta has issued a play_specific_track / replace_deck(path=…)
    # directive, the heartbeat passes the resolved title + path here so the
    # DJ prompt renders an unambiguous "IDLE DECK PINNED TO" block. The
    # planner has usually already loaded the pinned track by the time DJ
    # runs, but this is the belt-and-suspenders rule that catches the case
    # where the loaded title doesn't match (deck contention, stale state).
    pinned_idle_title: str = "",
    pinned_idle_path: str = "",
    pinned_idle_loaded: bool = False,
    # transition_now directive present and consumable. When True, DJ
    # should schedule a transition into idle this cycle (no "let it
    # breathe" deferral) — Treta has explicitly asked for the swap.
    transition_now_pending: bool = False,
    # v11 Phase 0 — room-sense: Treta's own mixer output, sampled ~3Hz by
    # _room_sense_loop. Rendered as ONE staleness-gated advisory line
    # (>15s old → dropped). Schema: {energy, energyDirection, ...,
    # masterLoudness, sampled_at}. None = no read yet (drop the line).
    room_sense: dict | None = None,
    # v11 Phase 1 — Notebook slice: ≤3 DERIVED lines from the durable event
    # log (rendered by render_notebook_slice). Threaded exactly like
    # room_sense_line/ownership_line — "" collapses the slot on a quiet
    # workspace. Caller (heartbeat) builds it from get_notebook().
    workspace_line: str = "",
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
    # Default mood class + allowed technique list (used even when no profile)
    _mood_class, _allowed = _mood_class_and_allowed(
        (mood_profile or {}).get("canonical_slug") or ""
    )
    if mood_profile:
        slug = mood_profile.get("canonical_slug") or ""
        vibe = ", ".join(mood_profile.get("vibe_keywords", [])[:4])
        profile_line = (
            f"Mood profile: {slug} | vibe: {vibe}\n"
            f"Mood class: {_mood_class} | allowed techniques: "
            f"{', '.join(_allowed)}"
        )
        if _mood_class == "continuous-energy":
            profile_line += " (echo_out BANNED — kick wall, no holes)"
        profile_line += "\n"

    # Phase 7 co-being mode: external Beings may have claimed one or more decks
    # via MCP. DJ Treta must not schedule transitions onto those decks.
    ownership_line = ""
    if external_decks:
        ownership_line = (
            f"EXTERNAL DECK OWNERSHIP: deck(s) {external_decks} claimed by "
            f"co-being(s); do NOT schedule transitions targeting these decks "
            f"and do NOT load tracks onto them.\n"
        )

    # ── ROOM-SENSE (v11 Phase 0) ────────────────────────────────────
    # Treta hears her OWN mixer output. ONE terse advisory line, kept to
    # a single line for Flash prompt-budget discipline. STALENESS-GATED:
    # if the snapshot is older than 15s (room-sense loop stalled), drop it
    # entirely rather than feed the DJ a frozen reading. The single
    # most-actionable section tag is picked in priority order
    # drop → breakdown → buildup (else none).
    room_sense_line = ""
    if room_sense and isinstance(room_sense, dict):
        import time as _t
        sampled_at = room_sense.get("sampled_at") or 0
        if _t.time() - sampled_at <= 15:
            energy = room_sense.get("energy", 0)
            direction = room_sense.get("energyDirection", "steady")
            if room_sense.get("dropDetected"):
                tag = " [DROP]"
            elif room_sense.get("breakdownDetected"):
                tag = " [BREAKDOWN]"
            elif room_sense.get("buildupDetected"):
                tag = " [BUILDUP]"
            else:
                tag = ""
            room_sense_line = (
                f"ROOM (my output): energy {energy:.0f}/10 {direction}{tag}\n"
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

    # ── Pre-computed transition points (v9 Tier-2) ──────────────────
    # These are what a real DJ uses: where active's outro begins, where
    # idle's intro ends. Pre-computed by the audio analyzer (Essentia
    # mix_in / mix_out heuristic). DJ no longer has to count seconds
    # from raw timeline strings — pick from these landmarks.
    active_camelot_str = active_camelot or active_key or "?"
    idle_camelot_str = idle_camelot or idle_key or "?"
    active_energy_str = f"e{active_energy}" if active_energy is not None else "e?"
    idle_energy_str = f"e{idle_energy}" if idle_energy is not None else "e?"

    # Active track transition points
    active_points = []
    if active_mix_out is not None:
        delta_to_outro = active_mix_out - position
        if delta_to_outro >= 0:
            active_points.append(
                f"  OUTRO STARTS AT: {active_mix_out:.0f}s "
                f"({delta_to_outro:.0f}s away — IDEAL transition-out point)"
            )
        else:
            active_points.append(
                f"  OUTRO STARTED AT: {active_mix_out:.0f}s "
                f"({-delta_to_outro:.0f}s ago — already in outro, schedule NOW)"
            )
    if active_mix_in is not None:
        active_points.append(f"  INTRO ENDED AT: {active_mix_in:.0f}s")
    active_points_block = "\n".join(active_points)
    if active_points_block:
        active_points_block += "\n"

    # ── IDLE DECK STATUS (v8.2 fix for replay bug) ──────────────────
    # Explicitly tells the DJ whether the idle-deck track has already been
    # played. Without this, DJ has been scheduling crossfades into played
    # tracks ("the next track is loaded on deck 1, schedule transition") —
    # observed in the live set on 2026-05-03.
    if idle_already_played:
        idle_status_line = (
            f"IDLE DECK STATUS: ALREADY PLAYED — DO NOT TRANSITION INTO "
            f"deck {idle_deck}. Call defer_decision(seconds=15) and wait for "
            f"a fresh load.\n"
        )
    else:
        idle_status_line = f"IDLE DECK STATUS: FRESH (deck {idle_deck} OK to mix into)\n"

    # ── Pinned-idle directive (atomic-cuddling-manatee fix) ─────────
    # When Treta has called play_specific_track or replace_deck(path=…),
    # the planner has loaded that exact track on the idle deck. The DJ
    # MUST honour this: schedule a transition into the pinned track,
    # don't pick another candidate from the playlist.
    pinned_block = ""
    if pinned_idle_path:
        if pinned_idle_loaded:
            pinned_block = (
                f"IDLE DECK PINNED TO: '{pinned_idle_title[:60]}' (loaded ✓)\n"
                f"  → Treta directive: this exact track must play next. "
                f"Schedule the transition into deck {idle_deck} now; do NOT "
                f"defer or pick another candidate.\n"
            )
        else:
            pinned_block = (
                f"IDLE DECK PINNED TO: '{pinned_idle_title[:60]}' "
                f"(NOT YET LOADED on deck {idle_deck})\n"
                f"  → Treta directive: load this exact path on deck "
                f"{idle_deck} BEFORE scheduling any transition. Call "
                f"load_track(deck={idle_deck}, "
                f"file_path={pinned_idle_path!r}). Path is non-negotiable.\n"
            )

    # When Treta has emitted a transition_now directive (typically as part
    # of play_specific_track), surface it so the DJ doesn't sit on
    # "wait for outro" when the listener has explicitly asked for the
    # swap right now.
    transition_now_block = ""
    if transition_now_pending:
        transition_now_block = (
            f"TRANSITION_NOW DIRECTIVE: schedule a transition into deck "
            f"{idle_deck} this cycle. Pick a sensible technique + duration "
            f"based on the music, but do NOT call defer_decision.\n"
        )

    # ── BAR-COUNT REFERENCE (v8.2 — pro DJs think in bars, not seconds) ──
    # Use active_bpm if > 0, fall back to idle_bpm or a 120 BPM placeholder.
    _ref_bpm = active_bpm if active_bpm and active_bpm > 0 else (idle_bpm or 120)
    bar_ref_block = (
        f"BAR-COUNT REFERENCE (active BPM {_ref_bpm:.0f}):\n"
        f"  8 bars  = {8*4*60/_ref_bpm:.0f}s\n"
        f"  16 bars = {16*4*60/_ref_bpm:.0f}s\n"
        f"  32 bars = {32*4*60/_ref_bpm:.0f}s   ← default crossfade for melodic techno\n"
        f"  64 bars = {64*4*60/_ref_bpm:.0f}s   ← big-moment crossfade\n"
        f"Pick duration in BARS first, convert to seconds with this table. "
        f"NEVER pick < 16 bars unless using hard_cut.\n"
    )

    # Idle track transition points
    idle_points = []
    idle_dur_str = f"/{idle_duration:.0f}s" if idle_duration else ""
    if idle_mix_in is not None:
        idle_points.append(
            f"  INTRO ENDS AT: {idle_mix_in:.0f}s "
            f"(seed-in window — drop active over idle's first {idle_mix_in:.0f}s)"
        )
        idle_points.append(
            f"  ECHO_OUT GUARD: if technique=echo_out, "
            f"duration MUST be ≥ 32s AND at_position MUST aim so that "
            f"transition START + duration ≈ idle's INTRO ENDS AT "
            f"({idle_mix_in:.0f}s) — i.e. echo tail rings into the drop, "
            f"not the buildup. Otherwise pick crossfade."
        )
    if idle_mix_out is not None:
        idle_points.append(f"  OUTRO STARTS AT: {idle_mix_out:.0f}s")
    idle_points_block = "\n".join(idle_points)
    if idle_points_block:
        idle_points_block += "\n"

    # Ideal-transition hint — pre-compute the obvious answer when both
    # mix points are known. DJ may override with a directive or based on
    # section context, but having the canonical answer in the prompt
    # massively reduces hallucinated at_position values.
    ideal_hint = ""
    if (active_mix_out is not None and idle_mix_in is not None
            and active_mix_out >= position):
        # Duration: target 32 bars (pro melodic-techno default), but never
        # exceed idle's intro length. Cap at 64 bars for sanity.
        _bars32_s = int(round(32 * 4 * 60 / max(_ref_bpm, 60)))
        _bars64_s = int(round(64 * 4 * 60 / max(_ref_bpm, 60)))
        ideal_dur = min(_bars32_s, int(idle_mix_in), _bars64_s)
        if ideal_dur < 16:
            # too-short intros — clamp to a 16-bar floor (avoid sub-16-bar mixes)
            ideal_dur = min(int(round(16 * 4 * 60 / max(_ref_bpm, 60))), int(idle_mix_in) or _bars32_s)
        ideal_hint = (
            f"\nIDEAL TRANSITION (pre-computed from mix points):\n"
            f"  schedule_transition(to_deck={idle_deck}, "
            f"at_position={int(active_mix_out)}, technique=<your_pick>, "
            f"duration={ideal_dur})\n"
            f"  → start when active's outro begins, blend over idle's intro.\n"
            f"  Override only if directive says otherwise OR section "
            f"context demands earlier (e.g. breakdown alignment).\n"
        )

    # ── WORKSPACE (v11 Phase 1) ─────────────────────────────────────
    # ≤3 derived lines from the durable Notebook (caller-built). One
    # newline-terminated block, dropped entirely when empty.
    workspace_block = f"{workspace_line}\n" if workspace_line else ""

    return (
        f"{directive_info}"
        f"{profile_line}"
        f"{ownership_line}"
        f"{room_sense_line}"
        f"{workspace_block}"
        f"ACTIVE: '{active_track[:40]}' at {position:.0f}s/{duration:.0f}s "
        f"({remaining:.0f}s left, BPM:{active_bpm:.0f} file:{active_file_bpm:.0f}, "
        f"Camelot:{active_camelot_str} {active_energy_str})\n"
        f"  NOW IN: {active_section}\n"
        f"{active_points_block}"
        f"  TIMELINE: {active_timeline}\n\n"
        f"NEXT: '{idle_track[:40]}' on deck {idle_deck}{idle_dur_str} "
        f"(BPM:{idle_bpm:.0f} file:{idle_file_bpm:.0f}, "
        f"Camelot:{idle_camelot_str} {idle_energy_str})\n"
        f"{pinned_block}"
        f"{idle_status_line}"
        f"{transition_now_block}"
        f"{idle_points_block}"
        f"  TIMELINE: {idle_timeline}\n"
        f"{ideal_hint}\n"
        f"{bar_ref_block}\n"
        f"{playlist_block}"
        f"{pending_info}\n"
        f"Decide now. Your options:\n"
        f"  - HARD RULE: if IDLE DECK PINNED TO names a path that does NOT "
        f"match the currently loaded idle, you MUST call load_track FIRST "
        f"on deck {idle_deck} with that exact path before any "
        f"schedule_transition. Pinned-idle outranks all other candidates.\n"
        f"  - HARD RULE: if IDLE DECK STATUS = ALREADY PLAYED, do NOT call "
        f"schedule_transition to deck {idle_deck}. Call "
        f"defer_decision(seconds=15) and wait for a fresh load.\n"
        f"  - If ACTIVE is in a breakdown / outro OR within ~30s of OUTRO "
        f"STARTS AT AND idle is FRESH, invoke schedule_transition. Use the "
        f"IDEAL TRANSITION values above unless a directive or section cue "
        f"tells you otherwise. Pick duration in BARS (default 32 bars for "
        f"melodic techno crossfade), convert to seconds with the bar-count "
        f"table above.\n"
        f"  - If the idle deck is empty or loaded with the wrong track, "
        f"invoke load_track with the best path from the playlist above.\n"
        f"  - Otherwise (active track is mid-drop, mid-buildup, or too "
        f"early — i.e. position is well before OUTRO STARTS AT), call "
        f"defer_decision(seconds=30).\n\n"
        f"NEVER pick at_position > duration. NEVER pick at_position < "
        f"current position+5. NEVER use idle's mix_in as active's "
        f"at_position — those are different decks.\n"
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
    workspace_line: str = "",
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

    # Worked example anchors the output format (Gemini best practice: a
    # concrete example cuts hallucinated/invalid JSON far more than rules
    # alone). Paths here are illustrative — the model must use real library
    # paths from <library>.
    example = (
        '{"planned_at":1736500000.0,"mood_snapshot":"melodic-techno",'
        '"reasoning_summary":"Holding 122-124 BPM and building energy gradually; '
        'rank 1 shares the current key for a seamless harmonic blend, lower ranks '
        'keep the floor moving if it is unavailable.",'
        '"tracks":[{"rank":1,"path":"melodic-techno/Artist - Song (Original Mix).mp3",'
        '"title":"Artist - Song","bpm":123.0,"key_camelot":"8A","energy":6,'
        '"reason":"Same key (8A), +1 BPM — seamless harmonic blend.",'
        '"transition_hint":{"technique":"bass_swap","duration":32,"at_section":"breakdown"}}]}'
    )

    # Structured with XML-style tags + instructions AFTER the large <library>
    # context, per Google's Gemini prompting guidance (helps Flash separate
    # data from task and reduces invalid output).
    return (
        "<role>\n"
        "You are DJ Treta's planning brain. Pick the next tracks for a live "
        "DJ set and return them as STRICT JSON. No markdown fences, no prose.\n"
        "</role>\n\n"
        "<now_playing>" + current_info + "</now_playing>\n"
        f"<mood>{mood_slug}{profile_line}</mood>\n"
        f"<already_played>DO NOT repeat any of these: {played_list}</already_played>"
        + directive_line
        + intent_line
        + ownership_line
        + feedback_line
        + (("\n" + workspace_line) if workspace_line else "")
        + "\n\n<library>\n"
        "Pick `path` values ONLY from this list — never invent a path.\n"
        + library_json
        + "\n</library>\n\n"
        "<output_schema>\n" + schema + "\n</output_schema>\n\n"
        "<example>\n" + example + "\n</example>\n\n"
        "<rules>\n"
        "- Return exactly 5 candidates ranked 1 (best) to 5.\n"
        "- Use `path` values EXACTLY as they appear in <library>.\n"
        "- Rank 1 should fit the current track BPM / key / energy best.\n"
        "- Lower ranks offer valid alternates if rank 1 is unavailable.\n"
        "- Never repeat a title from <already_played>.\n"
        "- reasoning_summary: one paragraph on your overall arc strategy.\n"
        "- Each track's `reason`: one sentence on mood/BPM/energy fit.\n"
        "- If the library is thin and you can return <5, do — say so in reasoning_summary.\n"
        "</rules>\n\n"
        "Return JSON ONLY, matching <output_schema>."
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
    workspace_line: str = "",
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
        + (("\n" + workspace_line) if workspace_line else "")
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
        "- transition_hint.technique: prefer echo_out for key/genre/tempo "
        "gaps; crossfade only for compatible Camelot + ≤3 BPM gap; "
        "filter_sweep for ±2-step Camelot bridges over a breakdown; "
        "bass_swap for compatible high-energy techno momentum. NEVER "
        "suggest hard_cut as default — DJ reserves it for breakdown→cold-drop "
        "with phrase-boundary alignment.\n"
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
    self_suggestions: list[dict] | None = None,
    sarathi_block: str = "",
) -> str:
    """Build the user message for Being agent conversation.

    `sarathi_block`: optional pre-rendered MODE: SARATHI header (+ any live
    transition suggestion in front of Manish). When present, Treta reads
    "do it" as confirm_suggestion, "no/darker/something else" as
    reject_suggestion, and "i've got this" as leave-it-alone.

    `self_suggestions`: optional list of active self_suggestion directives
    surfaced from the reflection loop. Rendered as an INNER NUDGE block
    above the listener message. Treta is expected to either
    honor_self_suggestion(id, reason) or discard_self_suggestion(id, reason)
    before responding — pure silence also leaves them to TTL-expire.
    Skipped entirely in readonly mode (web listener).
    """
    readonly_tag = ""
    if readonly:
        readonly_tag = (
            "\n\nMODE: READONLY — this is a live web listener. "
            "You can ONLY respond conversationally. Do NOT call set_dj_directive, "
            "set_planner_directive, set_mood, or any control tools. "
            "Just chat, share your thoughts on the music, describe the vibe.\n"
        )

    nudge_block = ""
    if self_suggestions and not readonly:
        lines = [
            "── INNER NUDGE FROM YOUR REFLECTION LOOP ──",
            "These are suggestions from your prior self, not from the listener.",
            "Listener's live message ALWAYS takes priority. Honor a nudge only "
            "if it still fits the moment; discard with a reason if it doesn't. "
            "Use honor_self_suggestion(id, reasoning) or "
            "discard_self_suggestion(id, reasoning) to gate each one before "
            "you act. Silence is also fine — they auto-expire.",
        ]
        for s in self_suggestions[:3]:  # cap at 3 to keep prompt tight
            ni = (s.get("next_intent") or "").strip()
            ti = s.get("to_improve") or []
            md = (s.get("mood_drift") or "").strip()
            ed = s.get("engagement_delta")
            lines.append(f"• id={s.get('id')} — intent: {ni or '(none)'}")
            if ti:
                lines.append(f"    to_improve: {ti}")
            if md:
                lines.append(f"    mood_drift: {md}")
            if ed is not None:
                lines.append(f"    listener_engagement_delta: {ed}")
        lines.append("── END INNER NUDGE ──\n")
        nudge_block = "\n".join(lines) + "\n"

    return (
        f"{context}\n\n{history}\n{readonly_tag}\n"
        f"{sarathi_block}"
        f"{nudge_block}"
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


# ── Autonomous wake (v11 Phase 2) ─────────────────────────────────────


def build_wake_user_message(event: dict, now_view: dict = None) -> str:
    """Build the user message for an OFF-CADENCE autonomous wake (v11 Phase 2).

    Unlike the periodic HEARTBEAT TICK, this fires because a single
    high-salience Notebook event pulled the Being awake (crowd-collapse,
    drop-landed, skip-burst, human directive, contradiction — see
    agent/salience.py). The message tells her WHAT woke her (the event kind),
    a one-line WHY (a terse digest of the event), and the current now_view
    summary if one is supplied, then asks for a brief think-or-act.

    Pure: takes plain dicts, never raises, returns "" inputs degrade gracefully.
    The caller (main.py _on_event) is responsible for tagging the resulting
    invocation author='being:wake' — this builder tags nothing.
    """
    ev = event if isinstance(event, dict) else {}

    kind = str(ev.get("kind", "event")).strip() or "event"
    author = str(ev.get("author", "")).strip()

    # One-line WHY: prefer a human-readable summary off the payload, else a
    # compact rendering of the payload, else just the kind.
    why = ""
    payload = ev.get("payload")
    if isinstance(payload, dict):
        why = str(
            payload.get("summary")
            or payload.get("text")
            or payload.get("action")
            or payload.get("reason")
            or ""
        ).strip()
        if not why:
            # Compact key:val of the most useful scalar fields, capped.
            scalars = [
                f"{k}={v}"
                for k, v in payload.items()
                if isinstance(v, (str, int, float, bool))
            ]
            why = ", ".join(scalars[:4])
    elif isinstance(payload, str):
        why = payload.strip()
    why = (why or kind)[:160]

    src = f" from {author}" if author else ""

    # now_view summary — same shape as Notebook.now_view():
    # {now_playing, up_next, room_sense, mood, recent}. Compact one-liner.
    now_line = ""
    if isinstance(now_view, dict):
        nv_bits: list[str] = []
        np = now_view.get("now_playing")
        if isinstance(np, dict):
            title = str(np.get("title") or np.get("path") or "").strip()
            if title:
                nv_bits.append(f"playing {title[:50]}")
        elif isinstance(np, str) and np.strip():
            nv_bits.append(f"playing {np.strip()[:50]}")
        mood = now_view.get("mood")
        if mood:
            nv_bits.append(f"mood: {str(mood).strip()[:30]}")
        rs = now_view.get("room_sense")
        if isinstance(rs, dict):
            energy = rs.get("energy")
            direction = rs.get("direction")
            rs_str = " ".join(
                str(x) for x in (
                    f"energy {energy}" if energy is not None else "",
                    str(direction) if direction else "",
                ) if x
            ).strip()
            if rs_str:
                nv_bits.append(f"room: {rs_str[:40]}")
        if nv_bits:
            now_line = "Now: " + " | ".join(nv_bits) + "\n"

    return (
        f"WAKE — a high-salience {kind} event{src} pulled you off-cadence.\n"
        f"Why: {why}\n"
        f"{now_line}"
        f"\nThis is not the periodic tick — something changed. "
        f"Think briefly, act if it warrants it, or say HEARTBEAT_OK."
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
