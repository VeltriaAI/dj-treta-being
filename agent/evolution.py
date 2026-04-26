"""EvolutionMixin — enhanced self-learning with pattern detection.

Replaces basic _agent_reflect() with structured data collection,
pattern detection, and optional self-modification triggers.
"""

import json
import logging
import time
from pathlib import Path
from .runtime_paths import runtime_path

log = logging.getLogger("dj-treta")

THINKING_FILE = runtime_path("thinking.log")


class EvolutionMixin:

    def _evolution_reflect(self):
        """Enhanced reflection — collects structured data, detects patterns.
        Called from planner_loop every N tracks (replaces _agent_reflect)."""
        if self._agent_busy:
            return
        try:
            data = self._collect_evolution_data()

            # Ask Being to reflect on structured data
            prompt = (
                f"SELF-REFLECTION TIME. Here's your performance data:\n\n"
                f"Last 5 tracks: {data['recent_tracks']}\n"
                f"Transitions: {data['transition_quality']}\n"
                f"Listener feedback: {data['feedback_summary']}\n"
                f"Energy arc: {data['energy_summary']}\n\n"
                f"Use save_learning() to record what worked and what didn't.\n"
                f"Be specific — what patterns do you see?"
            )
            self._invoke_being(prompt, timeout=60)

            # Detect patterns from accumulated data
            patterns = self._detect_patterns(data)
            if patterns:
                self._store_patterns(patterns)

                # Check if we should trigger self-evolution
                if hasattr(self, 'config') and self.config.evolution.auto_evolve:
                    should, goal = self._should_trigger_evolution(patterns)
                    if should:
                        log.info(f"Auto-evolution triggered: {goal}")
                        from .tools.evolve import evolve

                        result = evolve(
                            goal,
                            scope="agent/",
                            max_budget_usd=self.config.evolution.max_budget_per_evolve_usd,
                        )
                        log.info(f"Auto-evolution result: {result[:200]}")

            log.info("Evolution reflection complete")
        except Exception as e:
            log.warning(f"Evolution reflection error: {e}")

    def _collect_evolution_data(self) -> dict:
        """Gather structured performance data."""
        data = {
            "recent_tracks": [],
            "transition_quality": {"agent": 0, "auto": 0, "emergency": 0},
            "feedback_summary": {"likes": 0, "dislikes": 0, "liked_genres": []},
            "energy_summary": "",
        }

        # Recent tracks
        data["recent_tracks"] = [t.get("title", "?") for t in self.tracks_played[-5:]]

        # Transition quality from thinking log
        try:
            if THINKING_FILE.exists():
                content = THINKING_FILE.read_text()
                data["transition_quality"]["agent"] = content.count(
                    "[CALL:dj_treta] schedule_transition"
                )
                data["transition_quality"]["auto"] = content.count("Auto-transition")
        except Exception:
            pass

        data["transition_quality"]["emergency"] = self._emergency_count

        # Feedback
        try:
            from .db import get_liked_tracks, get_disliked_tracks

            liked = get_liked_tracks(20)
            disliked = get_disliked_tracks(20)
            data["feedback_summary"]["likes"] = len(liked)
            data["feedback_summary"]["dislikes"] = len(disliked)
            # liked returns list[dict] with genre; disliked returns list[str]
            genres = set(l.get("genre", "") for l in liked if l.get("genre"))
            data["feedback_summary"]["liked_genres"] = list(genres)[:5]
        except Exception:
            pass

        # Energy arc
        if self.current_set and self.current_set.get("energy_arc"):
            arc = self.current_set["energy_arc"]
            if arc:
                energies = [a.get("energy", 0) for a in arc if isinstance(a, dict)]
                if energies:
                    avg = sum(energies) / len(energies)
                    peak = max(energies)
                    data["energy_summary"] = (
                        f"avg:{avg:.1f} peak:{peak:.0f} samples:{len(energies)}"
                    )

        return data

    def _detect_patterns(self, data: dict) -> list[dict]:
        """Find recurring patterns in performance data."""
        patterns = []

        tq = data["transition_quality"]
        total_transitions = tq["agent"] + tq["auto"]

        # Pattern: too many auto-transitions (agent not deciding fast enough)
        if total_transitions > 0 and tq["auto"] / max(total_transitions, 1) > 0.5:
            patterns.append({
                "type": "transition_timing",
                "description": (
                    f"Auto-transitions ({tq['auto']}) outnumber agent transitions "
                    f"({tq['agent']}). Agent may be deciding too late."
                ),
                "confidence": min(0.9, tq["auto"] / max(total_transitions, 1)),
                "occurrences": tq["auto"],
                "suggested_action": (
                    "Lower the heartbeat P4 threshold from 50% to 40% of track duration"
                ),
            })

        # Pattern: too many emergencies
        if tq["emergency"] > 3:
            patterns.append({
                "type": "emergency_frequency",
                "description": (
                    f"{tq['emergency']} emergencies this session. "
                    f"Idle deck loading is too slow."
                ),
                "confidence": 0.9,
                "occurrences": tq["emergency"],
                "suggested_action": (
                    "Reduce planner loop interval or add eager loading after each transition"
                ),
            })

        # Pattern: listener dislikes outnumber likes
        fb = data["feedback_summary"]
        if fb["dislikes"] > fb["likes"] and fb["dislikes"] >= 3:
            patterns.append({
                "type": "listener_dissatisfaction",
                "description": (
                    f"More dislikes ({fb['dislikes']}) than likes ({fb['likes']}). "
                    f"Track selection needs improvement."
                ),
                "confidence": 0.7,
                "occurrences": fb["dislikes"],
                "suggested_action": (
                    "Adjust track selection to prefer genres/BPM ranges from liked tracks"
                ),
            })

        return patterns

    def _store_patterns(self, patterns: list[dict]):
        """Store or update patterns in evolution_patterns table."""
        try:
            from .db import get_db

            db = get_db()
            for p in patterns:
                existing = db.execute(
                    "SELECT id, occurrences FROM evolution_patterns "
                    "WHERE pattern_type=? AND resolved=0",
                    (p["type"],),
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE evolution_patterns SET occurrences=?, confidence=?, "
                        "description=?, last_seen=? WHERE id=?",
                        (
                            existing["occurrences"] + p["occurrences"],
                            p["confidence"],
                            p["description"],
                            time.time(),
                            existing["id"],
                        ),
                    )
                else:
                    db.execute(
                        "INSERT INTO evolution_patterns "
                        "(pattern_type, description, confidence, occurrences, suggested_action) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            p["type"],
                            p["description"],
                            p["confidence"],
                            p["occurrences"],
                            p["suggested_action"],
                        ),
                    )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"Pattern storage error: {e}")

    def _should_trigger_evolution(self, patterns: list[dict]) -> tuple[bool, str]:
        """Decide if patterns warrant auto-evolution."""
        if not hasattr(self, 'config') or not self.config.evolution.auto_evolve:
            return False, ""

        # Rate limit: check last evolution time and daily count
        try:
            from .db import get_db

            db = get_db()
            try:
                last = db.execute(
                    "SELECT created_at FROM evolution_log "
                    "WHERE status='success' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if last and time.time() - last["created_at"] < 86400:  # 24h cooldown
                    return False, ""

                # Check daily limit
                today_start = time.time() - (time.time() % 86400)
                count = db.execute(
                    "SELECT COUNT(*) as c FROM evolution_log WHERE created_at > ?",
                    (today_start,),
                ).fetchone()
                if count and count["c"] >= self.config.evolution.max_evolve_per_day:
                    return False, ""
            finally:
                db.close()
        except Exception:
            pass

        # Find strongest pattern
        for p in sorted(patterns, key=lambda x: x["confidence"], reverse=True):
            if p["confidence"] >= 0.8 and p["occurrences"] >= 5:
                return True, p["suggested_action"]

        return False, ""
