"""Self-suggestion tools — Treta's gate over her own reflection loop.

The reflection loop runs every 15 min and produces a structured synthesis
(went_well / to_improve / next_intent / mood_drift). Those reflections are
NOT auto-fired as directives — that would let a background loop quietly
mutate the set without Treta's judgment in the loop.

Instead, the reflection loop emits a typed `self_suggestion` directive
with target=`treta`. It surfaces in her next chat-turn prompt as an
"INNER NUDGE" block. She then decides:

  - honor_self_suggestion(directive_id, reasoning)
        marks satisfied, logs the decision to treta_thoughts, then she
        emits whatever concrete directives the suggestion implied
        (set_dj_directive, set_planner_directive, play_specific_track…)

  - discard_self_suggestion(directive_id, reasoning)
        marks satisfied with a "discarded" note, logs to treta_thoughts.
        Used when she disagrees with her prior reflection — listener
        cues, set arc, or live observation say otherwise.

  - defer (no-op)
        do nothing. The directive auto-expires after its TTL (~5 min).
        Pure silence is also a valid response.

The audit trail (every suggestion + every honor/discard) lives in
LanceDB.treta_thoughts so we can review later whether the reflection
loop is actually producing signal worth acting on.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("dj-treta")


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
        log.debug(f"[suggestion] thought-embed failed (non-fatal): {exc}")


def list_self_suggestions() -> list[dict]:
    """Read all active self_suggestion directives currently in queue.

    Returns oldest-first list of:
      {id, created_at_age_s, expires_at_age_s, next_intent, to_improve, mood_drift, engagement_delta}

    Most callers should rely on the INNER NUDGE block in the prompt — this
    tool is for cases where Treta wants to enumerate before deciding.
    """
    sess = _session()
    if sess is None:
        return []
    now = time.time()
    out = []
    for d in sess.directives:
        if d.get("status") != "active":
            continue
        if d.get("kind") != "self_suggestion":
            continue
        payload = d.get("payload") or {}
        created = d.get("created_at") or 0
        expires = d.get("expires_at")
        out.append({
            "id": d.get("id"),
            "created_at_age_s": int(now - created) if created else None,
            "expires_in_s": int(expires - now) if expires else None,
            "next_intent": payload.get("next_intent", ""),
            "to_improve": payload.get("to_improve", []) or [],
            "mood_drift": payload.get("mood_drift", ""),
            "engagement_delta": payload.get("engagement_delta"),
        })
    return out


def honor_self_suggestion(directive_id: str, reasoning: str = "") -> dict:
    """Accept a self-suggestion. Marks it satisfied + logs the decision.

    Call this BEFORE you emit the concrete directives the suggestion
    implied. The mark-satisfied step prevents the same suggestion from
    surfacing in your next prompt; the log step gives us an audit trail
    of which reflections you actually agreed with.

    Args:
        directive_id: id from the INNER NUDGE block (or from
            list_self_suggestions()).
        reasoning: one short sentence on WHY you're honoring it.
            Example: "listener engagement was flat last 15 min — I agree
            we should drop BPM 4 and add vocals".

    Returns:
        {ok: bool, message: str}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "session not available"}

    target = None
    for d in sess.directives:
        if d.get("id") == directive_id and d.get("kind") == "self_suggestion":
            target = d
            break
    if target is None:
        return {"ok": False, "message": f"no self_suggestion with id {directive_id}"}
    if target.get("status") != "active":
        return {
            "ok": False,
            "message": f"suggestion already {target.get('status')} — too late to honor",
        }

    sess.mark_satisfied(directive_id)
    payload = target.get("payload") or {}
    _log_thought(
        agent_id="treta:judgment",
        decision_text=(
            f"Honored self-suggestion: '{payload.get('next_intent', '')[:120]}'. "
            f"Reasoning: {reasoning[:200] or '(none)'}."
        ),
        context={
            "directive_id": directive_id,
            "decision": "honor",
            "reasoning": reasoning,
            "suggestion_payload": payload,
        },
    )
    log.info(f"[suggestion] HONORED {directive_id}: {reasoning[:80]}")
    return {
        "ok": True,
        "message": (
            f"Suggestion {directive_id} honored. Now emit the concrete "
            f"directives (set_dj_directive / set_planner_directive / "
            f"play_specific_track / set_mood) that act on it."
        ),
    }


def discard_self_suggestion(directive_id: str, reasoning: str = "") -> dict:
    """Reject a self-suggestion. Marks it satisfied + logs the decision.

    Use when your in-the-moment judgment disagrees with the reflection.
    Reasoning is REQUIRED-ish — without it the audit trail is useless and
    we can't tell whether the reflection loop is producing signal worth
    keeping. One short sentence is fine.

    Args:
        directive_id: id from the INNER NUDGE block.
        reasoning: WHY you're discarding. Example: "listener just asked
            for high-energy psytrance, the suggestion to wind down
            contradicts what they want right now".

    Returns:
        {ok: bool, message: str}
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "message": "session not available"}

    target = None
    for d in sess.directives:
        if d.get("id") == directive_id and d.get("kind") == "self_suggestion":
            target = d
            break
    if target is None:
        return {"ok": False, "message": f"no self_suggestion with id {directive_id}"}
    if target.get("status") != "active":
        return {
            "ok": False,
            "message": f"suggestion already {target.get('status')} — nothing to discard",
        }

    sess.mark_satisfied(directive_id)
    payload = target.get("payload") or {}
    _log_thought(
        agent_id="treta:judgment",
        decision_text=(
            f"Discarded self-suggestion: '{payload.get('next_intent', '')[:120]}'. "
            f"Reasoning: {reasoning[:200] or '(none)'}."
        ),
        context={
            "directive_id": directive_id,
            "decision": "discard",
            "reasoning": reasoning,
            "suggestion_payload": payload,
        },
    )
    log.info(f"[suggestion] DISCARDED {directive_id}: {reasoning[:80]}")
    return {"ok": True, "message": f"Suggestion {directive_id} discarded."}
