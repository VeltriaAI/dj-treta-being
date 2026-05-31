"""Wake/veto policy for DJ Treta's autonomous Being-wake (v11 Phase 2).

Pure functions. No imports of session / notebook / mixxx / heartbeat — every
input is passed in by the caller (the integrator reads the state, this module
only decides). This keeps the policy unit-testable in isolation and diff-able
against the inline ladder it replaces.

═══════════════════════════════════════════════════════════════════════════
CRITICAL SAFETY CONTRACT — music-never-stops sits ABOVE this module.
═══════════════════════════════════════════════════════════════════════════
P1 silence recovery and P2 emergency-load are the audio safety nets. They are
HIGHER priority than any autonomous wake and are NEVER routed through this
module. The suppressor ONLY ever vetoes the OPTIONAL autonomous Being-wake
(the off-cadence self-reflection / re-think triggered by a high-salience
notebook event). It can NEVER veto, delay, or gate a safety action.

Concretely:
  - `wake_veto()` decides whether to ALLOW an autonomous wake. A veto here
    means "don't wake the Being right now" — it does NOT touch playback.
  - `p4_decision()` is a behaviour-preserving re-expression of the heartbeat's
    P4 creative-DJ-invoke ladder. P4 is the *creative* transition decision; it
    already sits BELOW P1 (silence recovery) and P2 (end-of-track rescue),
    which run earlier in `_heartbeat()` and are unaffected by this module.

If this module is removed or returns garbage, the WORST case is a missed or
spurious autonomous wake / a creative-transition invoke that the caller still
guards. Audio continuity does not depend on it.

SHIP FLAG-FALSE: when `config.evolution.enabled == false`, the integrator does
NOT register `_on_event`, does NOT start the wake thread, and does NOT add a
wake prompt line — so `wake_veto()` is simply never called and behaviour is
byte-identical to the current P0/P1 build. (`p4_decision()` is a pure
extraction the integrator may wire in regardless of the flag, since it only
re-expresses already-shipped P4 logic — see its docstring.)
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "WAKE_COOLDOWN_S",
    "is_human_directive",
    "wake_veto",
    "p4_decision",
]

# Default minimum gap between two autonomous wakes (seconds). Mirrors the 60s
# cooldown called out in V11_BUILD_PLAN.md Phase 2 ("60s cooldown").
WAKE_COOLDOWN_S: float = 60.0

# Author tag stamped on the Being's own wake output. An event carrying this
# author must NEVER be allowed to re-trigger a wake (anti-echo) — otherwise a
# wake's notebook appends would loop the Being awake forever.
_WAKE_AUTHOR = "being:wake"

# Authors that are agents/Beings inside the process (never a human). Used only
# to recognise a *human* directive vs an agent-emitted one. Conservative: any
# author prefixed "being:" is also treated as non-human (covers being:wake,
# being:reflection, etc.).
_AGENT_AUTHORS = frozenset({
    "dj", "planner", "being", "library", "producer",
    "reflection", "journal", "intention", "heartbeat",
})


def is_human_directive(event: dict) -> bool:
    """True iff `event` is a directive that came from a human (Manish).

    A human directive is the one wake source that PIERCES the Sarathi veto: in
    Sarathi mode the Being stays quiet for *self*-generated wakes, but a human
    explicitly telling her something must always get through.

    Encoding (caller-agnostic):
      - kind == "directive", AND
      - the author is NOT an in-process agent/Being.

    The author is considered human when it is neither in `_AGENT_AUTHORS` nor
    prefixed "being:". An empty/None author is treated as NON-human-directive
    (fail-safe: we do not let an unattributed event pierce a veto). The caller
    may also set `event["human"] = True` to mark it explicitly.
    """
    if not isinstance(event, dict):
        return False
    if event.get("human") is True and event.get("kind") == "directive":
        return True
    if event.get("kind") != "directive":
        return False
    author = event.get("author")
    if not author or not isinstance(author, str):
        return False
    if author.startswith("being:"):
        return False
    return author not in _AGENT_AUTHORS


def wake_veto(
    *,
    event: dict,
    sarathi_mode: bool,
    manish_in_motion: bool,
    last_wake_age_s: float,
    wake_in_flight: bool,
    cooldown_s: float = WAKE_COOLDOWN_S,
) -> tuple[bool, str]:
    """Decide whether to VETO an autonomous Being-wake for `event`.

    Returns (vetoed, reason). `(False, "ok")` means the wake is allowed.

    SAFETY: this gates ONLY the optional autonomous wake. P1 silence / P2
    emergency load are never routed here (see module docstring).

    Veto rules (checked in order; first match wins):
      1. wake_in_flight        — a wake is already running; serialize, don't
                                 stack a second one.
      2. cooldown              — last_wake_age_s < cooldown_s (default 60s):
                                 too soon since the last wake.
      3. anti-echo             — event author == "being:wake": a wake's own
                                 output must never re-trigger a wake.
      4. Sarathi (self-wake)   — in sarathi_mode, a SELF-generated autonomous
                                 wake is vetoed (Manish drives). EXCEPTION: a
                                 HUMAN DIRECTIVE pierces this veto.
      5. manish_in_motion      — Manish is mid-move on the FLX4; an autonomous
                                 wake would talk over him. (A human directive
                                 still pierces — he asked for it.)

    A human directive (`is_human_directive(event)`) pierces BOTH the Sarathi
    veto (rule 4) and the manish_in_motion veto (rule 5). It is still subject
    to in-flight / cooldown / anti-echo (1-3): those are about not stacking or
    echoing wakes, not about whose turn it is.
    """
    human_directive = is_human_directive(event)

    # 1. A wake is already running — never stack a second concurrent wake.
    if wake_in_flight:
        return (True, "wake_in_flight")

    # 2. Cooldown: too soon since the last wake fired.
    if last_wake_age_s < cooldown_s:
        return (True, f"cooldown ({last_wake_age_s:.0f}s < {cooldown_s:.0f}s)")

    # 3. Anti-echo: a wake's own output must never re-trigger a wake.
    if isinstance(event, dict) and event.get("author") == _WAKE_AUTHOR:
        return (True, "anti-echo (author=being:wake)")

    # 4. Sarathi mode: Manish drives → veto SELF-generated autonomous wakes.
    #    A human directive pierces this veto.
    if sarathi_mode and not human_directive:
        return (True, "sarathi (autonomous wake suppressed — Manish drives)")

    # 5. Manish is mid-move on the FLX4 → veto autonomous wakes that would
    #    talk over him. A human directive pierces (he asked).
    if manish_in_motion and not human_directive:
        return (True, "manish_in_motion")

    return (False, "ok")


def p4_decision(*, ladder_inputs: dict) -> dict:
    """Behaviour-preserving re-expression of the heartbeat P4 priority ladder.

    P4 is the creative DJ-invoke: when the active track is winding down and the
    idle deck is ready, the heartbeat decides whether to invoke the DJ agent to
    choose the next transition. The *current* implementation is a long
    `if/elif … elif <action>` chain in `heartbeat.py` (~lines 563-595). Each
    skip branch only logs a debug line and falls through (no invoke); the final
    branch is the invoke.

    This function mirrors that chain EXACTLY, returning a plain decision dict so
    the integrator can replace the inline ladder with one call and the verifier
    can diff-test it. It does NO I/O and reads NO globals — every value the
    inline ladder reads is passed in via `ladder_inputs`.

    Decision shape (faithful to the inline ladder's observable behaviour):
        {
          "invoke": bool,   # True iff the ladder reaches the action branch
          "skip":   bool,   # not invoke (convenience inverse)
          "reason": str,    # the skip reason (matches the inline debug text),
                            # or "transition_window" when invoking, or
                            # "no_window" when the action guard itself fails.
        }

    `ladder_inputs` keys (all caller-supplied; defaults match the attribute
    fall-backs used inline via getattr(..., default)):
        now                    float  — time.time() snapshot used by the ladder
        dj_paused              bool   — session.dj_paused
        sarathi                bool   — session.sarathi_mode
        live_sugg              bool   — a live sarathi suggestion is pending
        manish_in_motion       bool   — session.manish_in_motion
        manish_motion_until    float  — session.manish_motion_until (0.0)
        deferred_until         float  — session.dj_deferred_until (0.0)
        idle_deck_external     bool   — self._deck_owned_by_external(idle_deck)
        last_transition_at     float  — session.last_transition_at (0.0)
        min_play_time_seconds  float  — config.planner.min_play_time_seconds
        transition_window      bool   — idle_ready and remaining > 0
        agent_busy             bool   — self._agent_busy
        transition_pending     bool   — self._transition_pending
        sched_file_exists      bool   — scheduled-transition.json exists
    """
    g = ladder_inputs.get  # local alias

    now = float(g("now", 0.0))

    dj_paused = bool(g("dj_paused", False))
    sarathi = bool(g("sarathi", False))
    live_sugg = bool(g("live_sugg", False))
    manish_in_motion = bool(g("manish_in_motion", False))
    manish_motion_until = float(g("manish_motion_until", 0.0) or 0.0)
    deferred_until = float(g("deferred_until", 0.0) or 0.0)
    idle_deck_external = bool(g("idle_deck_external", False))
    last_transition_at = float(g("last_transition_at", 0.0) or 0.0)
    min_play_time_seconds = float(g("min_play_time_seconds", 0.0) or 0.0)
    transition_window = bool(g("transition_window", False))
    agent_busy = bool(g("agent_busy", False))
    transition_pending = bool(g("transition_pending", False))
    sched_file_exists = bool(g("sched_file_exists", False))

    # Mirrors: `if getattr(self.session, "dj_paused", False):`
    #   → "P4 DJ invoke skipped — dj_paused (Treta has the wheel)"
    if dj_paused:
        return _skip("dj_paused")

    # Mirrors: `elif _sarathi and _live_sugg:`
    #   → "P4 DJ invoke skipped — live suggestion already pending (sarathi …)"
    if sarathi and live_sugg:
        return _skip("sarathi_live_suggestion_pending")

    # Mirrors: `elif manish_in_motion and now < manish_motion_until:`
    #   → "P4 DJ invoke skipped — manish_in_motion (…FLX4)"
    if manish_in_motion and now < manish_motion_until:
        return _skip("manish_in_motion")

    # Mirrors: `elif time.time() < deferred_until:`
    #   → "P4 DJ invoke skipped — deferred until …"
    if now < deferred_until:
        return _skip("deferred")

    # Mirrors: `elif self._deck_owned_by_external(idle_deck):`
    #   → "P4 DJ invoke skipped — idle deck … owned by …"
    if idle_deck_external:
        return _skip("idle_deck_external")

    # Mirrors: `elif (time.time() - last_transition_at) < min_play_time_seconds:`
    #   → "P4 DJ invoke skipped — post-transition cooldown (…letting the groove ride)"
    if (now - last_transition_at) < min_play_time_seconds:
        return _skip("post_transition_cooldown")

    # Mirrors the final action guard:
    #   `elif (transition_window and not self._agent_busy
    #          and not self._transition_pending and not sched_file_exists):`
    # → reach the invoke body (build prompt + call DJ agent).
    if transition_window and not agent_busy and not transition_pending \
            and not sched_file_exists:
        return {"invoke": True, "skip": False, "reason": "transition_window"}

    # No `else:` exists inline — if none of the above matched, the ladder simply
    # falls through and does NOT invoke. Preserve that: skip with no reason.
    return _skip("no_window")


def _skip(reason: str) -> dict:
    """A skip (no DJ invoke) outcome for `p4_decision`."""
    return {"invoke": False, "skip": True, "reason": reason}
