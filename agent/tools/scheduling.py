"""Self-scheduling — Treta wakes herself for specific reasons.

Heartbeat reads session.self_schedule at top of every tick and fires
entries whose at_ts has passed. A fired entry stays in the list until
pruned (audit trail). The callback_directive, when non-empty, is
injected as a 'shape' directive Treta will see in her next chat tick.
"""

import time as _t
import logging

log = logging.getLogger("dj-treta")


def _session():
    from ..session_state import get_session
    return get_session()


def schedule_self(in_seconds: int, reason: str, callback_directive: str = "") -> dict:
    """Schedule Treta to wake herself in `in_seconds` with `reason`.

    Args:
      in_seconds: delay before firing. Clamped to 5..86400 (1 day).
      reason: human-readable reason ("check mood landed", "resume downloads").
      callback_directive: optional free-text directive injected when fired.

    Returns: {ok: bool, at_ts: float, message: str}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "at_ts": 0, "message": "session not registered"}
    try:
        in_seconds = max(5, min(86400, int(in_seconds)))
    except (TypeError, ValueError):
        return {"ok": False, "at_ts": 0, "message": "in_seconds must be an integer"}
    at_ts = _t.time() + in_seconds
    entry = {
        "at_ts": at_ts,
        "reason": (reason or "")[:200],
        "callback_directive": (callback_directive or "")[:500],
        "fired": False,
        "created_at": _t.time(),
    }
    try:
        sess.self_schedule.append(entry)
    except Exception as e:
        log.warning(f"[self-schedule] append failed: {e}")
        return {"ok": False, "at_ts": 0, "message": f"append failed: {e}"}
    log.info(f"[self-schedule] +{in_seconds}s: {entry['reason'][:80]}")
    return {"ok": True, "at_ts": at_ts, "message": f"Will fire in {in_seconds}s"}


def cancel_self_schedule(reason_match: str) -> int:
    """Cancel all unfired entries whose reason contains `reason_match`. Returns count cancelled."""
    sess = _session()
    if sess is None:
        return 0
    if not reason_match:
        return 0
    try:
        match = reason_match.lower()
        count = 0
        new_list = []
        for e in list(sess.self_schedule):
            if not e.get("fired") and match in (e.get("reason", "") or "").lower():
                count += 1
                continue  # drop
            new_list.append(e)
        if count:
            sess.self_schedule.clear()
            sess.self_schedule.extend(new_list)
            log.info(f"[self-schedule] cancelled {count} entries matching {reason_match!r}")
        return count
    except Exception as e:
        log.warning(f"[self-schedule] cancel failed: {e}")
        return 0


def list_self_schedule(include_fired: bool = False) -> list:
    """List scheduled entries. include_fired=False filters to pending only."""
    sess = _session()
    if sess is None:
        return []
    try:
        if include_fired:
            return list(sess.self_schedule)
        return [e for e in sess.self_schedule if not e.get("fired")]
    except Exception as e:
        log.warning(f"[self-schedule] list failed: {e}")
        return []
