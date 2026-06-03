"""Transitions mixin — scheduled transition execution."""

import json
import logging
import time
from pathlib import Path
from .runtime_paths import runtime_path

log = logging.getLogger("dj-treta")


class TransitionMixin:

    def _execute_scheduled_transition(self, sched: dict):
        """Python-side transition executor. Waits for track position, then executes.
        Runs in its own thread. Agent is FREE during this entire time."""
        import httpx
        from .main import _get_status
        from .tools import do_transition, do_bass_swap, do_filter_sweep, do_hard_cut, do_echo_out, do_riser, do_dissolve

        to_deck = sched["toDeck"]
        at_position = sched["atPosition"]
        technique = sched.get("technique", "crossfade")
        duration = sched.get("duration", 45)
        active_deck = sched.get("activeDeck", 1 if to_deck == 2 else 2)
        scheduled_track_path = sched.get("activeTrackPath", "") or ""
        # FIX-B: forward the persisted tempo intent. Without this the executor
        # dropped bpm_after/glide_duration and every do_* defaulted to "anchor",
        # so the scheduled intent (e.g. "keep") was silently lost at fire-time.
        bpm_after = sched.get("bpmAfter", "anchor")
        glide_duration = sched.get("glideDuration", 60)

        log.info(f"Transition scheduled: {technique} to deck {to_deck} at {at_position}s (waiting...)")

        try:
            # Poll until position reached or track ends
            # Adaptive sleep: far away = 5s, close = 0.3s for precision
            while True:
                status = _get_status(self.config.mixxx.url)
                if not status:
                    log.warning("Scheduled transition: Mixxx not responding, aborting")
                    break

                d = status.get(f"deck{active_deck}", {})
                if not d.get("playing"):
                    log.warning(f"Scheduled transition: Deck {active_deck} stopped, aborting")
                    break

                current_pos = float(d.get("position_seconds", 0) or 0)
                gap = at_position - current_pos

                if gap <= 0:
                    # ── Patch B: stale-schedule detection ─────────────────
                    # If the active deck's track changed between scheduling
                    # and fire-time (planner force-load is the common cause),
                    # the at_position is meaningless against the new track.
                    # Abort and ask planner to re-evaluate.
                    if scheduled_track_path:
                        try:
                            tinfo = httpx.get(
                                f"{self.config.mixxx.url}/api/deck/{active_deck}/track_info",
                                timeout=2,
                            ).json() or {}
                            current_track_path = tinfo.get("file_path", "") or ""
                        except Exception:
                            current_track_path = ""
                        if current_track_path and current_track_path != scheduled_track_path:
                            log.warning(
                                f"[STALE-SCHEDULE] active track changed from "
                                f"{scheduled_track_path!r} to {current_track_path!r}, "
                                f"aborting scheduled {technique} — planner will re-evaluate"
                            )
                            if hasattr(self, "session"):
                                try:
                                    self.session.replan_requested = True
                                except Exception:
                                    pass
                            break

                    # ── Patch A: fire-time overshoot guard ────────────────
                    # Re-fetch fresh status; the duration we scheduled with
                    # might no longer fit the active track (cold-load 0,
                    # short track, late tail). Shorten if needed.
                    try:
                        d_now = status.get(f"deck{active_deck}", {})
                        rem_now = float(d_now.get("remaining_seconds", 0) or 0)
                    except Exception:
                        rem_now = 0.0
                    if rem_now > 0 and rem_now < duration + 5:
                        new_duration = max(10, int(rem_now - 5))
                        if new_duration < duration:
                            log.warning(
                                f"[OVERSHOOT-GUARD] track ends in {rem_now:.1f}s, "
                                f"transition needs {duration}s — shortening to "
                                f"{new_duration}s so the fade finishes before track end"
                            )
                            if hasattr(self, '_ws_broadcast'):
                                self._ws_broadcast("log", {"text": f"[OVERSHOOT-GUARD] shortening {technique} {duration}s → {new_duration}s (rem={rem_now:.1f}s)"})
                            duration = new_duration

                    # Time to execute — right on the mark
                    log.info(f"Executing {technique} to deck {to_deck} at {current_pos:.1f}s (target: {at_position}s)")
                    if hasattr(self, '_ws_broadcast'):
                        self._ws_broadcast("log", {"text": f"Executing {technique} to deck {to_deck} at {current_pos:.1f}s"})
                    if technique == "bass_swap":
                        result = do_bass_swap(to_deck, duration, bpm_after=bpm_after, glide_duration=glide_duration)
                    elif technique == "filter_sweep":
                        result = do_filter_sweep(to_deck, duration, bpm_after=bpm_after, glide_duration=glide_duration)
                    elif technique == "hard_cut":
                        result = do_hard_cut(to_deck, bpm_after=bpm_after, glide_duration=glide_duration)
                    elif technique == "echo_out":
                        result = do_echo_out(to_deck, duration, bpm_after=bpm_after, glide_duration=glide_duration)
                    elif technique == "riser":
                        result = do_riser(to_deck, duration, bpm_after=bpm_after, glide_duration=glide_duration)
                    elif technique == "dissolve":
                        result = do_dissolve(to_deck, duration, bpm_after=bpm_after, glide_duration=glide_duration)
                    else:
                        result = do_transition(to_deck, duration, bpm_after=bpm_after, glide_duration=glide_duration)
                    log.info(f"Transition result: {str(result)[:200]}")
                    if hasattr(self, '_ws_broadcast'):
                        self._ws_broadcast("log", {"text": f"Transition result: {str(result)[:200]}"})
                    # Mark transition event in energy arc
                    if self.current_set and isinstance(self.current_set.get("energy_arc"), list):
                        self.current_set["energy_arc"].append({
                            "t": round(time.time() - self.current_set["started_at"]),
                            "event": "transition",
                            "technique": technique,
                            "to_deck": to_deck,
                        })
                    self._record_playing_tracks()
                    self._check_set_duration()
                    break

                # Adaptive sleep: tight when close, relaxed when far
                if gap > 30:
                    time.sleep(5)
                elif gap > 10:
                    time.sleep(2)
                elif gap > 3:
                    time.sleep(0.5)
                else:
                    time.sleep(0.2)
        except Exception as e:
            log.error(f"Scheduled transition error: {e}")
        finally:
            self._transition_pending = False
            # BUG-5 fix (Phase A2 dry run 2026-04-19): emit idle_needs_load
            # immediately after the transition. The executor is the
            # authoritative moment of the deck-state change — waiting for
            # planner_loop's 15s polling tick introduced an observed
            # ~90s idle-empty window. This removes the race entirely.
            if hasattr(self, "session"):
                self.session.idle_needs_load = True
                # v10: stamp completion so P4 (proactive mix) holds off for
                # min_play_time_seconds — let the new track breathe instead of
                # being mixed out instantly (kills back-to-back churn).
                self.session.last_transition_at = time.time()
            runtime_path("scheduled-transition.json").unlink(missing_ok=True)
            runtime_path("transition-pending.lock").unlink(missing_ok=True)
