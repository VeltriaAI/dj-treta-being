"""Directive tools — Being sets directives for DJ and Planner agents.

The Being is the brain. These tools let her direct her autonomous agents
without micromanaging them. Each agent reads its directive from Session
on its next cycle.

Under v8, directives live in Session (single source of truth), not in
/tmp/dj-treta-directives.json. The old file-based IPC is gone.
"""

import logging

log = logging.getLogger("dj-treta")


def _session():
    """Import-time-safe accessor for the Session singleton.

    Imported inside functions so this tool module doesn't crash at import
    time if session_state hasn't yet registered (e.g. during tests).
    """
    from ..session_state import get_session
    return get_session()


def set_dj_directive(instruction: str) -> str:
    """Set a directive for the DJ agent. The DJ reads this on its next heartbeat cycle.

    Examples:
    - "When bhojpuri track loads on idle deck, use hard_cut transition"
    - "Keep energy high for next 3 transitions, use bass_swap"
    - "Let current track play fully, no early transition"

    Args:
        instruction: What the DJ agent should do on its next cycle(s).
    """
    sess = _session()
    if sess is None:
        return "Session not available — directive not set"
    sess.dj_directive = instruction
    log.info(f"DJ directive set: {instruction[:100]}")
    return f"DJ directive set: {instruction}"


def set_planner_directive(instruction: str) -> str:
    """Set a directive for the Planner agent. The Planner reads this on its next planning cycle.

    Examples:
    - "Download 3 bhojpuri tracks immediately"
    - "Generate 2 dark techno tracks at 130 BPM"
    - "Focus on ambient/chill tracks for wind-down"

    Args:
        instruction: What the Planner should prioritize on its next cycle.
    """
    sess = _session()
    if sess is None:
        return "Session not available — directive not set"
    sess.planner_directive = instruction
    log.info(f"Planner directive set: {instruction[:100]}")
    return f"Planner directive set: {instruction}"


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
    if sess.dj_directive:
        parts.append(f"DJ: {sess.dj_directive}")
    if sess.planner_directive:
        parts.append(f"Planner: {sess.planner_directive}")
    return "\n".join(parts) if parts else "No active directives"


def clear_directives() -> str:
    """Clear all directives after they've been consumed."""
    sess = _session()
    if sess is None:
        return "Session not available"
    sess.dj_directive = ""
    sess.planner_directive = ""
    return "Directives cleared"


def defer_decision(seconds: int = 30) -> dict:
    """Defer the DJ's transition decision by N seconds.

    Use when the active track is mid-drop / mid-buildup / too early for a
    transition. The heartbeat will skip P4 invocation until this time
    elapses, then ask again. Honor the music — don't transition if it
    would feel forced.

    Args:
        seconds: how long to defer (default 30, clamped to 5-120).
    """
    import time
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
