"""E4 — State Sequencing & Set Archive: agent-callable tool functions.

Registered in agent/agents.py under the `# --- E4 State/Set ---` marker.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("dj-treta")


def get_set_archive(n: int = 5) -> dict:
    """Retrieve the N most recently archived DJ sets (state sequences + metadata).

    Use this to review what sets Treta has played, inspect recorded state
    sequences, or find a set_id for replay.

    Args:
        n: How many sets to return (newest first). Max 50. 0 = return all.

    Returns a dict with "sets" (list of set records) and "archive_path".
    """
    from ..state_sequence import get_set_archive as _get, _archive_path
    n = min(int(n), 50)
    sets = _get(n=n)
    return {
        "count": len(sets),
        "archive_path": str(_archive_path()),
        "sets": [
            {
                "set_id": s.get("set_id", ""),
                "started_at": s.get("started_at", 0),
                "ended_at": s.get("ended_at", 0),
                "mood": s.get("mood", ""),
                "tracks_played": len(s.get("tracks_played") or []),
                "states_recorded": len(s.get("state_sequence") or []),
                "recording_path": s.get("recording_path", ""),
            }
            for s in sets
        ],
    }


def replay_set(set_id: str) -> dict:
    """Replay a previously archived set's mixer state sequence.

    This re-applies the recorded EQ / volume / filter / crossfader snapshots
    to Mixxx in order, at BPM-derived timing. It does NOT restart playback
    or change which tracks are loaded — it only re-applies the mixer moves.

    Replay runs in a background daemon thread so this tool returns immediately.
    The set must have been recorded during a previous session (check
    get_set_archive for available set IDs).

    Args:
        set_id: The set ID string (e.g. "set-20260525-201300").
    """
    if not set_id or not set_id.strip():
        return {"error": "set_id is required"}

    stop_event = threading.Event()

    def _run():
        from ..state_sequence import replay_set as _replay
        result = _replay(set_id=set_id.strip(), stop_event=stop_event)
        log.info("replay_set background thread finished: %s", result)

    t = threading.Thread(target=_run, daemon=True, name=f"replay-{set_id}")
    t.start()

    return {
        "status": "replay_started",
        "set_id": set_id,
        "note": (
            "Replay is running in the background. Mixer controls will be "
            "re-applied in sequence. Music playback is unaffected."
        ),
    }
