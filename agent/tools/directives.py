"""Directive tools — Being sets directives for DJ and Planner agents.

The Being is the brain. These tools let her direct her autonomous agents
without micromanaging them.

Two directive shapes:

  Surgical (typed, Python-enforced):
    play_specific_track(path)  → load_track + transition_now directives
    replace_deck(deck, path=…) → load_track directive on a specific deck

  Shape (free-text, LLM-interpreted):
    set_dj_directive(text)       → guides the DJ agent's next decisions
    set_planner_directive(text)  → guides the Planner's next picks

Surgical directives are consumed by Python code in planner_loop and
heartbeat — they do not depend on the LLM honoring them. Shape directives
are appended to the next subagent's prompt and survive 3 cycles by
default before auto-expiring.

Under v8/v9, all directives live in the typed `Session.directives` queue
(plus legacy `dj_directive`/`planner_directive` strings as a back-compat
mirror of the latest shape directive).
"""

import logging
import os
import time

log = logging.getLogger("dj-treta")


# Default TTLs (seconds). Shape directives auto-expire so a one-off ask
# ("keep energy high for next 3 transitions") doesn't bleed into the
# rest of the set. Surgical directives don't auto-expire — they expire
# only via supersession or by being satisfied.
SHAPE_DIRECTIVE_TTL_S = 90.0


def _session():
    """Import-time-safe accessor for the Session singleton."""
    from ..session_state import get_session
    return get_session()


# ── Shape directives (free-text, prompt-injected) ─────────────────────


def set_dj_directive(instruction: str) -> str:
    """Shape the DJ agent's next decisions with a free-text directive.

    Use for *guidance* that the DJ should weigh, not surgical commands.
    For "play this specific track now", call `play_specific_track`.

    Examples:
    - "Keep energy high for next 3 transitions, prefer bass_swap"
    - "Let the current track breathe, no early transition"
    - "Listener wants more vocals — bias toward vocal-heavy picks"

    Lifecycle: stays alive for ~90s, then auto-expires. Calling again
    replaces the active shape directive (only one active at a time).

    Args:
        instruction: What the DJ agent should weigh on its next cycles.
    """
    sess = _session()
    if sess is None:
        return "Session not available — directive not set"
    sess.add_directive(
        kind="shape",
        target="dj",
        payload={"text": instruction},
        ttl_seconds=SHAPE_DIRECTIVE_TTL_S,
        supersede_kinds=["shape"],   # one shape directive at a time per target
    )
    log.info(f"DJ shape directive set: {instruction[:100]}")
    return f"DJ directive set: {instruction}"


def set_planner_directive(instruction: str) -> str:
    """Shape the Planner's next picks with a free-text directive.

    Use for *guidance* that the planner should weigh. For "load this
    specific track on idle deck now", call `play_specific_track` or
    `replace_deck(deck, path=…)`.

    Examples:
    - "Download 3 bhojpuri tracks immediately, skip dataset suggestions"
    - "Focus on ambient/chill tracks — winding down"
    - "Avoid Argy for the next 5 picks, listener wants variety"

    Lifecycle: stays alive ~90s, then auto-expires. Calling again
    replaces the active shape directive.

    Args:
        instruction: What the Planner should prioritize on its next cycles.
    """
    sess = _session()
    if sess is None:
        return "Session not available — directive not set"
    sess.add_directive(
        kind="shape",
        target="planner",
        payload={"text": instruction},
        ttl_seconds=SHAPE_DIRECTIVE_TTL_S,
        supersede_kinds=["shape"],
    )
    log.info(f"Planner shape directive set: {instruction[:100]}")
    return f"Planner directive set: {instruction}"


# ── Surgical directives (typed, Python-enforced) ──────────────────────


def play_specific_track(
    path: str,
    deck: int = 0,
    transition: bool = True,
) -> dict:
    """Force the named track to play next. Surgical, not advisory.

    The right tool when Manish (or you) names a specific track to play.
    Treta's typical flow:
      1. search_music(artist=…, title=…)        → URL
      2. download_track(url, genre=…)            → file on disk
      3. play_specific_track(path=<file_path>)   → loads on idle deck
                                                    + transitions to it

    The path is honored *programmatically* — the planner overrides its
    LLM-generated playlist for the idle slot, and the DJ agent's prompt
    will show "IDLE DECK PINNED TO: <track>" so it loads + transitions
    even if it would otherwise skip.

    Args:
        path: Absolute path to the audio file. Must exist on disk.
        deck: Which deck to load on. 0 (default) = next idle deck.
            Pass 1 or 2 to force a specific deck.
        transition: If True (default), also schedule a transition into
            this track once it loads. If False, just load it on idle
            and let the existing transition logic decide when to fire.

    Returns:
        {ok: bool, directive_id: str, message: str}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "Session not available"}
    if not path or not os.path.exists(path):
        return {"ok": False, "message": f"file not found: {path}"}
    if deck not in (0, 1, 2):
        return {"ok": False, "message": f"invalid deck {deck} (must be 0, 1, 2)"}

    # Derive a display title from the filename basename for prompt
    # rendering. The planner/DJ never need to parse this — the path is
    # authoritative. Title is purely for human-readable prompt output.
    title = os.path.splitext(os.path.basename(path))[0]

    load_id = sess.add_directive(
        kind="load_track",
        target="planner",
        payload={
            "deck": deck if deck in (1, 2) else None,  # None = next idle
            "path": path,
            "title": title,
        },
        # No TTL — surgical directives expire via satisfaction or
        # supersession. A new play_specific_track call cancels prior
        # load_track directives.
        supersede_kinds=["load_track", "transition_now"],
    )

    if transition:
        sess.add_directive(
            kind="transition_now",
            target="dj",
            payload={
                "deck": deck if deck in (1, 2) else None,
                "bound_to": load_id,
                "title": title,
            },
        )

    log.info(f"play_specific_track: {title} → directive {load_id}")
    return {
        "ok": True,
        "directive_id": load_id,
        "message": f"Will load + play: {title}",
    }


def replace_deck(deck: int, instruction: str = "", path: str = "") -> str:
    """Force-replace the track on a specific deck.

    Two modes:

    - With `path`: emits a typed load_track directive pointing at the
      given file. The planner loads it on `deck` next cycle, bypassing
      the "idle has fresh cued track, skip load" gate. This is the
      surgical mode — use when you know exactly which file to load.

    - Without `path`: legacy behaviour. Writes a JSON intent into
      `session.user_intent`; the planner ejects `deck` and loads
      whatever's rank-1 from its next playlist. Use when you want
      "anything but this current track" (e.g. "the listener hated this,
      swap it for something better").

    Args:
        deck: Which deck to replace (1 or 2).
        instruction: Optional shape directive describing what to load
            instead (e.g. "something with more energy"). When set,
            also emits a planner shape directive.
        path: Optional absolute path. When set, the loaded track is
            this exact file (not the LLM's rank-1 guess).
    """
    import json
    sess = _session()
    if sess is None:
        return "Session not available — replace not signaled"
    if deck not in (1, 2):
        return f"Invalid deck {deck} — must be 1 or 2"

    if path:
        if not os.path.exists(path):
            return f"file not found: {path}"
        title = os.path.splitext(os.path.basename(path))[0]
        sess.add_directive(
            kind="load_track",
            target="planner",
            payload={"deck": deck, "path": path, "title": title},
            supersede_kinds=["load_track"],
        )
        if instruction:
            sess.add_directive(
                kind="shape",
                target="planner",
                payload={"text": instruction},
                ttl_seconds=SHAPE_DIRECTIVE_TTL_S,
                supersede_kinds=["shape"],
            )
        log.info(f"replace_deck (path mode): deck={deck} ← {title}")
        return f"Replace signal sent — deck {deck} ← {title}"

    # Legacy path: rank-1 from playlist.
    sess.user_intent = json.dumps({
        "action": "replace_deck",
        "deck": int(deck),
        "instruction": instruction,
        "ts": time.time(),
    })
    if instruction:
        sess.add_directive(
            kind="shape",
            target="planner",
            payload={"text": instruction},
            ttl_seconds=SHAPE_DIRECTIVE_TTL_S,
            supersede_kinds=["shape"],
        )
    log.info(f"replace_deck (legacy mode): deck={deck} instruction={instruction[:80]}")
    return f"Replace signal sent — deck {deck} will be ejected and reloaded on next planner tick"


# ── Mood + utility ────────────────────────────────────────────────────


def set_mood(mood: str) -> str:
    """Change the current mood/genre for the entire set.

    Updates the Being's mood on Session. Session fires the mood-change
    callback (registered in main.py at startup) which kicks off the LLM
    mood resolver and triggers a planner replan.

    Args:
        mood: The new mood/genre (e.g. "bhojpuri", "dark-techno", "ambient", "psytrance")
    """
    sess = _session()
    if sess is None:
        return "Session not available — mood not set"
    sess.mood = mood
    log.info(f"Mood changed to: {mood}")
    return f"Mood changed to: {mood}"


def get_directives() -> str:
    """Read current directives (for debugging/status)."""
    sess = _session()
    if sess is None:
        return "Session not available"
    parts = []
    # Active typed directives.
    actives = [d for d in sess.directives if d.get("status") == "active"]
    for d in actives:
        kind = d.get("kind")
        target = d.get("target")
        payload = d.get("payload") or {}
        if kind == "shape":
            parts.append(f"[{target} shape] {payload.get('text','')[:80]}")
        elif kind == "load_track":
            parts.append(f"[load_track deck={payload.get('deck')}] {payload.get('title','')[:80]}")
        elif kind == "transition_now":
            parts.append(f"[transition_now deck={payload.get('deck')}] bound_to={payload.get('bound_to')}")
    return "\n".join(parts) if parts else "No active directives"


def clear_directives() -> str:
    """Clear all directives — both typed queue and legacy mirrors."""
    sess = _session()
    if sess is None:
        return "Session not available"
    n = sess.clear_directive_queue()
    return f"Directives cleared ({n} entries removed)"


def defer_decision(seconds: int = 30) -> dict:
    """Defer the DJ's transition decision by N seconds.

    Use when the active track is mid-drop / mid-buildup / too early for a
    transition. The heartbeat will skip P4 invocation until this time
    elapses, then ask again. Honor the music — don't transition if it
    would feel forced.

    Args:
        seconds: how long to defer (default 30, clamped to 5-120).
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "session not registered"}
    seconds = max(5, min(int(seconds), 120))
    sess.dj_deferred_until = time.time() + seconds
    log.info(f"DJ deferred decision for {seconds}s")
    return {
        "ok": True,
        "deferred_seconds": seconds,
        "deferred_until": sess.dj_deferred_until,
    }


# --- E3/E5 ---  Arrangement plan visibility (the leapfrog).
def get_arrangement_plan() -> dict:
    """Read the planner's current rolling ARRANGEMENT PLAN.

    The arrangement plan is a short sequence of musical *intents* (track +
    transition technique + energy target + bar duration) toward a high-level
    goal (e.g. "build energy 16 bars → drop into a breakdown → loop roll →
    next track"). The planner re-derives it every cycle so it adapts live.

    Use this to see the shape Treta is currently authoring before deciding
    how to realize the next transition, or to narrate her intent. Each intent
    maps onto the mixer-State sequence at execution; the `technique` is a hint
    to schedule_transition / do_transition.

    Returns the plan dict ({goal, horizon_bars, intents:[...]}) or an empty
    plan when none has been built yet.
    """
    sess = _session()
    if sess is None:
        return {"goal": "", "intents": [], "horizon_bars": 0,
                "message": "session not registered"}
    plan = getattr(sess, "arrangement_plan", None)
    if not plan:
        return {"goal": "", "intents": [], "horizon_bars": 0,
                "message": "no arrangement plan yet"}
    return plan
