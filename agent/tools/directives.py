"""Directive tools — Being sets directives for DJ and Planner agents.

The Being is the brain. These tools let her direct her autonomous agents
without micromanaging them. Each agent reads its directive on its next cycle.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("dj-treta")

# Shared state file — Being writes, agents read
_DIRECTIVE_FILE = Path("/tmp/dj-treta-directives.json")


def _read_directives() -> dict:
    try:
        if _DIRECTIVE_FILE.exists():
            return json.loads(_DIRECTIVE_FILE.read_text())
    except Exception:
        pass
    return {}


def _write_directives(data: dict):
    _DIRECTIVE_FILE.write_text(json.dumps(data, indent=2))


def set_dj_directive(instruction: str) -> str:
    """Set a directive for the DJ agent. The DJ reads this on its next heartbeat cycle.

    Examples:
    - "When bhojpuri track loads on idle deck, use hard_cut transition"
    - "Keep energy high for next 3 transitions, use bass_swap"
    - "Let current track play fully, no early transition"

    Args:
        instruction: What the DJ agent should do on its next cycle(s).
    """
    data = _read_directives()
    data["dj"] = {"instruction": instruction, "set_at": time.time()}
    _write_directives(data)
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
    data = _read_directives()
    data["planner"] = {"instruction": instruction, "set_at": time.time()}
    _write_directives(data)
    log.info(f"Planner directive set: {instruction[:100]}")
    return f"Planner directive set: {instruction}"


def set_mood(mood: str) -> str:
    """Change the current mood/genre for the entire set.

    This updates the Being's mood, the current set's mood/genre,
    and triggers a planner replan on the next cycle.

    Args:
        mood: The new mood/genre (e.g. "bhojpuri", "dark-techno", "ambient", "psytrance")
    """
    # Write mood to a temp file — Being's main loop picks it up
    Path("/tmp/dj-treta-mood-change.json").write_text(json.dumps({
        "mood": mood, "set_at": time.time()
    }))
    log.info(f"Mood change requested: {mood}")
    return f"Mood changed to: {mood}"


def get_directives() -> str:
    """Read current directives (for debugging/status).

    Returns the current DJ and Planner directives as JSON.
    """
    data = _read_directives()
    return json.dumps(data, indent=2) if data else "No active directives"


def clear_directives() -> str:
    """Clear all directives after they've been consumed."""
    _DIRECTIVE_FILE.unlink(missing_ok=True)
    return "Directives cleared"
