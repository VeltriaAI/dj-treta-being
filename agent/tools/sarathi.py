"""Sarathi Mode tools — Treta suggests transitions; Manish executes.

In Sarathi Mode (copilot), Manish drives transitions on the DDJ-FLX4 while
Treta does everything else — loads tracks, plans, manages library — and
SUGGESTS transition moves rather than executing them.

The DJ agent calls `suggest_transition()` instead of `schedule_transition()`.
That writes a typed `transition_suggestion` directive into the session queue
(reusing the same directive machinery as the reflection-loop self_suggestions)
and broadcasts it to the WS dialog channel for the TUI / Mixxx panel.

Resolution:
  - confirm_suggestion(id)  — Manish said "do it". Replays the suggestion's
      params through schedule_transition() (which writes scheduled-transition
      .json → heartbeat P3 executes it). Marks the directive satisfied.
  - reject_suggestion(id)   — Manish said "no / something else". Marks
      satisfied (rejected) + flags a replan.
  - (no-op / TTL expiry)    — Manish executed it himself on the FLX4, or
      ignored it. The directive auto-expires; the manual-transition detector
      in heartbeat marks it 'manish_executed_manually' for revealed-preference
      learning.

The actual transition executors (do_transition / do_bass_swap / do_echo_out)
and heartbeat P3 are untouched — Sarathi only redirects *who triggers the
write* to scheduled-transition.json.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("dj-treta")

# How long a transition suggestion stays live before auto-expiring. Matched to
# a typical transition window — if Manish hasn't acted by then, it's stale.
SUGGESTION_TTL_S = 120.0


def _session():
    from ..session_state import get_session
    return get_session()


def _log_thought(agent_id: str, decision_text: str, context: dict) -> None:
    """Best-effort embed of suggestion decisions into LanceDB.treta_thoughts."""
    try:
        from ..memory import store_thought
        store_thought(
            ts=time.time(),
            agent_id=agent_id,
            decision_text=decision_text,
            context=context,
        )
    except Exception as exc:
        log.debug(f"[sarathi] thought-embed failed (non-fatal): {exc}")


def _broadcast(event: str, data: dict) -> None:
    """Best-effort push to the WS dialog channel (TUI / Mixxx panel)."""
    try:
        from ..session_state import get_session
        sess = get_session()
        being = getattr(sess, "_being_ref", None) if sess else None
        if being is not None and hasattr(being, "_ws_broadcast"):
            being._ws_broadcast(event, data)
    except Exception as exc:
        log.debug(f"[sarathi] ws broadcast failed (non-fatal): {exc}")


def suggest_transition(
    to_deck: int,
    technique: str = "crossfade",
    at_position: int = 0,
    duration: int = 45,
    reason: str = "",
    track_title: str = "",
    at_section_marker: str = "",
    bpm_after: str = "anchor",
) -> dict:
    """Suggest a transition to Manish (Sarathi Mode). Does NOT execute it.

    Call this — instead of schedule_transition — when a transition window
    opens and you (Treta) have a recommendation. Manish either executes it
    himself on the FLX4, or says "do it" and you fire it via confirm.

    State your reasoning plainly in `reason`: key bridge, BPM gap, energy
    intent, why this technique. That text surfaces in his panel.

    Args:
        to_deck: deck to transition TO (1 or 2)
        technique: crossfade / bass_swap / echo_out / filter_sweep / hard_cut / riser / dissolve
        at_position: track position (s) to start; or use at_section_marker
        duration: transition length (s)
        reason: plain-language justification shown to Manish
        track_title: human-readable name of the incoming track (for the panel)
        at_section_marker: "mix_out" / "next_breakdown" etc (overrides at_position)
        bpm_after: "anchor" (default — ride to mood BPM center, prevents drift) / "keep" / "reset" / a number

    Returns:
        {ok, suggestion_id, message}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "session not available"}

    payload = {
        "to_deck": int(to_deck),
        "technique": technique,
        "at_position": int(at_position),
        "duration": int(duration),
        "reason": reason,
        "track_title": track_title,
        "at_section_marker": at_section_marker,
        "bpm_after": str(bpm_after),
    }
    sug_id = sess.add_directive(
        kind="transition_suggestion",
        target="manish",
        payload=payload,
        ttl_seconds=SUGGESTION_TTL_S,
        supersede_kinds=["transition_suggestion"],  # one live suggestion at a time
    )
    _broadcast("transition_suggestion", {"id": sug_id, **payload})
    log.info(
        f"[sarathi] SUGGEST {sug_id}: {technique} → deck {to_deck} "
        f"({track_title or 'track'}) — {reason[:80]}"
    )
    return {
        "ok": True,
        "suggestion_id": sug_id,
        "message": (
            f"Suggested {technique} → deck {to_deck}. Manish will execute on the "
            f"FLX4, or say 'do it' and you fire it via confirm_suggestion."
        ),
    }


def list_pending_suggestions() -> list[dict]:
    """All active transition_suggestion directives, oldest-first."""
    sess = _session()
    if sess is None:
        return []
    now = time.time()
    out = []
    for d in sess.directives:
        if d.get("status") != "active" or d.get("kind") != "transition_suggestion":
            continue
        p = d.get("payload") or {}
        out.append({
            "id": d.get("id"),
            "age_s": int(now - (d.get("created_at") or now)),
            "expires_in_s": int((d.get("expires_at") or now) - now),
            "to_deck": p.get("to_deck"),
            "technique": p.get("technique"),
            "at_position": p.get("at_position"),
            "duration": p.get("duration"),
            "reason": p.get("reason", ""),
            "track_title": p.get("track_title", ""),
        })
    return out


def _latest_pending(sess):
    """Return the newest active transition_suggestion directive, or None."""
    latest = None
    for d in sess.directives:
        if d.get("status") == "active" and d.get("kind") == "transition_suggestion":
            if latest is None or (d.get("created_at") or 0) > (latest.get("created_at") or 0):
                latest = d
    return latest


def confirm_suggestion(suggestion_id: str = "") -> dict:
    """Manish said 'do it' — execute the suggested transition.

    Replays the suggestion's stored params through schedule_transition(),
    which writes scheduled-transition.json so heartbeat P3 executes it with
    all its normal clamping/section-resolution. Marks the directive satisfied.

    Args:
        suggestion_id: which suggestion. Empty = the most recent live one
            (almost always what 'do it' means).

    Returns:
        {ok, message}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "session not available"}

    target = None
    if suggestion_id:
        for d in sess.directives:
            if d.get("id") == suggestion_id and d.get("kind") == "transition_suggestion":
                target = d
                break
    else:
        target = _latest_pending(sess)

    if target is None:
        return {"ok": False, "message": "no pending transition suggestion to confirm"}
    if target.get("status") != "active":
        return {"ok": False, "message": f"suggestion already {target.get('status')}"}

    p = target.get("payload") or {}
    sid = target.get("id")
    sess.mark_satisfied(sid)

    # Replay through schedule_transition — reuses ALL its validation, position
    # clamping, section-marker resolution, mood-guard, echo-out floor.
    try:
        from .transitions import schedule_transition
        result = schedule_transition(
            to_deck=p.get("to_deck", 0),
            at_position=p.get("at_position", 0),
            technique=p.get("technique", "crossfade"),
            duration=p.get("duration", 45),
            bpm_after=p.get("bpm_after", "anchor"),
            at_section_marker=p.get("at_section_marker", ""),
        )
    except Exception as exc:
        log.error(f"[sarathi] confirm execute failed: {exc}")
        return {"ok": False, "message": f"execution failed: {exc}"}

    _log_thought(
        agent_id="treta:sarathi",
        decision_text=f"Manish confirmed transition: {p.get('technique')} → deck {p.get('to_deck')}. {p.get('reason','')[:120]}",
        context={"suggestion_id": sid, "decision": "confirmed", "payload": p},
    )
    _broadcast("suggestion_resolved", {"id": sid, "resolution": "confirmed"})
    log.info(f"[sarathi] CONFIRMED {sid} → executing: {str(result)[:120]}")
    return {
        "ok": True,
        "message": f"Executing {p.get('technique')} → deck {p.get('to_deck')}. {result}",
    }


def reject_suggestion(suggestion_id: str = "", reason: str = "") -> dict:
    """Manish said 'no / something else' — drop the suggestion + replan.

    Args:
        suggestion_id: which one. Empty = most recent live.
        reason: optional ("too dark", "give me vocal", ...). Stored for learning.

    Returns:
        {ok, message}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "session not available"}

    target = None
    if suggestion_id:
        for d in sess.directives:
            if d.get("id") == suggestion_id and d.get("kind") == "transition_suggestion":
                target = d
                break
    else:
        target = _latest_pending(sess)

    if target is None:
        return {"ok": False, "message": "no pending transition suggestion to reject"}
    if target.get("status") != "active":
        return {"ok": False, "message": f"suggestion already {target.get('status')}"}

    p = target.get("payload") or {}
    sid = target.get("id")
    sess.mark_satisfied(sid)
    # Signal the planner to come up with an alternative.
    try:
        sess.replan_requested = True
    except Exception:
        pass
    _log_thought(
        agent_id="treta:sarathi",
        decision_text=f"Manish rejected transition: {p.get('technique')} → deck {p.get('to_deck')}. Reason: {reason[:120] or '(none)'}.",
        context={"suggestion_id": sid, "decision": "rejected", "reason": reason, "payload": p},
    )
    _broadcast("suggestion_resolved", {"id": sid, "resolution": "rejected", "reason": reason})
    log.info(f"[sarathi] REJECTED {sid}: {reason[:80]}")
    return {"ok": True, "message": f"Dropped. {('Reason: ' + reason) if reason else 'Re-planning alternative.'}"}
