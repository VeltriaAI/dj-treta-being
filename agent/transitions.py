"""Transitions mixin — scheduled transition execution."""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("dj-treta")


class TransitionMixin:

    def _execute_scheduled_transition(self, sched: dict):
        """Python-side transition executor. Waits for track position, then executes.
        Runs in its own thread. Agent is FREE during this entire time."""
        from .main import _get_status
        from .tools import do_transition, do_bass_swap, do_filter_sweep, do_hard_cut, do_echo_out

        to_deck = sched["toDeck"]
        at_position = sched["atPosition"]
        technique = sched.get("technique", "crossfade")
        duration = sched.get("duration", 45)
        active_deck = sched.get("activeDeck", 1 if to_deck == 2 else 2)

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
                    # Time to execute — right on the mark
                    log.info(f"Executing {technique} to deck {to_deck} at {current_pos:.1f}s (target: {at_position}s)")
                    if technique == "bass_swap":
                        result = do_bass_swap(to_deck, duration)
                    elif technique == "filter_sweep":
                        result = do_filter_sweep(to_deck, duration)
                    elif technique == "hard_cut":
                        result = do_hard_cut(to_deck)
                    elif technique == "echo_out":
                        result = do_echo_out(to_deck, duration)
                    else:
                        result = do_transition(to_deck, duration)
                    log.info(f"Transition result: {str(result)[:200]}")
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
            Path("/tmp/dj-treta-scheduled-transition.json").unlink(missing_ok=True)
