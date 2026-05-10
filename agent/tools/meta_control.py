"""Meta-control over subagents — Treta takes the wheel when she needs to.

Sets pause flags on session that subagent loops check at top of each
cycle. force_replan clears the planner playlist and bumps the replan
signal. restart_subagent is best-effort (exact mechanics depend on
which subagent runs in a thread vs main process).
"""

import logging

log = logging.getLogger("dj-treta")

VALID_AGENTS = {"planner", "dj", "library"}


def _session():
    from ..session_state import get_session
    return get_session()


def pause_subagent(name: str) -> str:
    """Pause a subagent. It will skip its next cycle until resumed.

    Args: name in {'planner', 'dj', 'library'}
    """
    sess = _session()
    if sess is None:
        return "session not registered"
    if name not in VALID_AGENTS:
        return f"invalid agent: {name}. Must be one of {sorted(VALID_AGENTS)}"
    try:
        setattr(sess, f"{name}_paused", True)
    except Exception as e:
        log.warning(f"[meta-control] pause failed: {e}")
        return f"pause failed: {e}"
    log.info(f"[meta-control] paused: {name}")
    return f"{name} paused"


def resume_subagent(name: str) -> str:
    """Resume a paused subagent."""
    sess = _session()
    if sess is None:
        return "session not registered"
    if name not in VALID_AGENTS:
        return f"invalid agent: {name}. Must be one of {sorted(VALID_AGENTS)}"
    try:
        setattr(sess, f"{name}_paused", False)
    except Exception as e:
        log.warning(f"[meta-control] resume failed: {e}")
        return f"resume failed: {e}"
    log.info(f"[meta-control] resumed: {name}")
    return f"{name} resumed"


def force_replan(directive: str = "") -> str:
    """Clear planner's playlist and request a fresh planning cycle.

    Args:
      directive: optional shape directive injected for the next cycle.
    """
    sess = _session()
    if sess is None:
        return "session not registered"
    try:
        sess.playlist = None
        sess.replan_requested = True
        if directive:
            try:
                sess.add_directive(
                    kind="shape",
                    target="planner",
                    payload={"text": directive},
                    ttl_seconds=90.0,
                    supersede_kinds=["shape"],
                )
            except Exception as e:
                log.warning(f"[meta-control] add_directive failed: {e}")
    except Exception as e:
        log.warning(f"[meta-control] force_replan failed: {e}")
        return f"force_replan failed: {e}"
    log.info(f"[meta-control] force_replan ({directive[:80] if directive else 'no directive'})")
    return f"Replan triggered" + (f" with directive: {directive[:80]}" if directive else "")


def restart_subagent(name: str) -> str:
    """Best-effort restart of a subagent.

    Implementation depends on subagent. For 'library' (separate thread),
    set a flag the library_loop checks to break + spawn anew. For
    'planner' and 'dj' (main-process loops), reset their per-cycle state
    by toggling pause + clearing internal state markers. The actual
    restart wiring lives in the loop modules; this function just sets
    the signal and pauses-then-resumes.
    """
    sess = _session()
    if sess is None:
        return "session not registered"
    if name not in VALID_AGENTS:
        return f"invalid agent: {name}"
    try:
        setattr(sess, f"{name}_paused", True)
        setattr(sess, f"{name}_restart_requested", True)
        setattr(sess, f"{name}_paused", False)
    except Exception as e:
        log.warning(f"[meta-control] restart failed: {e}")
        return f"restart failed: {e}"
    log.info(f"[meta-control] restart requested: {name}")
    return f"{name} restart requested (effective on next cycle)"


def get_subagent_pause_state() -> dict:
    """Snapshot of pause flags. Useful for Treta to verify state before acting."""
    sess = _session()
    if sess is None:
        return {}
    try:
        return {
            "planner": getattr(sess, "planner_paused", False),
            "dj":      getattr(sess, "dj_paused", False),
            "library": getattr(sess, "library_paused", False),
        }
    except Exception as e:
        log.warning(f"[meta-control] get_pause_state failed: {e}")
        return {}
