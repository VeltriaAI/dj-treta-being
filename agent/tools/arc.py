"""Set arc — Treta pre-commits to an energy/duration shape, then progress-checks.

Stored on session.set_arc as a dict. Heartbeat reads progress and emits
shape directives when drift > 20%.
"""

import time as _t
import logging

log = logging.getLogger("dj-treta")

# Allowed energy curves
ENERGY_CURVES = {"build", "peak-then-settle", "flat-warm", "rollercoaster"}
# Allowed ending styles
ENDING_STYLES = {"fade-out", "drop-and-stop", "ambient-tail"}


def _session():
    from ..session_state import get_session
    return get_session()


def plan_set_arc(target_minutes: int, energy_curve: str, ending_style: str) -> dict:
    """Pre-commit to a set shape.

    Args:
      target_minutes: planned set duration. Clamped to 15..480 (8 hr).
      energy_curve: one of 'build' (e3->e9), 'peak-then-settle' (e7->e9->e5),
                    'flat-warm' (e5..e6 throughout), 'rollercoaster' (e3<->e9).
      ending_style: one of 'fade-out', 'drop-and-stop', 'ambient-tail'.

    Returns: {ok: bool, arc: dict, message: str}

    Side effect: writes session.set_arc with checkpoints.
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "arc": None, "message": "session not registered"}
    try:
        target_minutes = max(15, min(480, int(target_minutes)))
    except (TypeError, ValueError):
        return {"ok": False, "arc": None, "message": "target_minutes must be an integer"}
    if energy_curve not in ENERGY_CURVES:
        return {"ok": False, "arc": None, "message": f"energy_curve must be one of {sorted(ENERGY_CURVES)}"}
    if ending_style not in ENDING_STYLES:
        return {"ok": False, "arc": None, "message": f"ending_style must be one of {sorted(ENDING_STYLES)}"}

    # Build checkpoints at 10/25/50/75/90% milestones, with expected energy
    # per curve. Energy is on a 1..10 scale matching the analyzer's energy_peak.
    curve_energies = {
        "build":            [3, 4, 6, 8, 9],
        "peak-then-settle": [7, 9, 9, 7, 5],
        "flat-warm":        [5, 5, 6, 6, 5],
        "rollercoaster":    [3, 8, 5, 9, 6],
    }
    pcts = [10, 25, 50, 75, 90]
    energies = curve_energies[energy_curve]
    started_at = _t.time()
    checkpoints = [
        {"at_pct": p, "expected_energy": e, "hit_at": None, "observed_energy": None}
        for p, e in zip(pcts, energies)
    ]

    arc = {
        "target_minutes": target_minutes,
        "energy_curve": energy_curve,
        "ending_style": ending_style,
        "started_at": started_at,
        "checkpoints": checkpoints,
    }
    try:
        sess.set_arc = arc
    except Exception as e:
        log.warning(f"[set-arc] write failed: {e}")
        return {"ok": False, "arc": None, "message": f"write failed: {e}"}
    log.info(
        f"[set-arc] planned: {target_minutes}min, curve={energy_curve}, ending={ending_style}"
    )
    return {"ok": True, "arc": arc, "message": f"Arc planned: {target_minutes}min {energy_curve}"}


def progress_set_arc() -> dict:
    """Where we are vs the planned arc.

    Returns: {
      ok: bool,
      elapsed_pct: float,
      planned_minutes: int,
      elapsed_minutes: float,
      next_checkpoint: dict | None,
      drift: str,
      suggestion: str,
    }
    """
    sess = _session()
    if sess is None:
        return {"ok": False, "drift": "no-arc", "suggestion": "session not registered"}
    arc = getattr(sess, "set_arc", None)
    if not arc:
        return {"ok": False, "drift": "no-arc", "suggestion": "no arc planned — call plan_set_arc first"}

    try:
        elapsed_s = _t.time() - arc.get("started_at", _t.time())
        target_s = max(1, int(arc.get("target_minutes", 60)) * 60)
        elapsed_pct = min(100.0, (elapsed_s / target_s) * 100)

        next_cp = None
        for cp in arc.get("checkpoints", []) or []:
            if cp.get("hit_at") is None:
                next_cp = cp
                break

        if next_cp is None:
            drift = "on-target"
            suggestion = "all checkpoints hit — moving toward set ending"
        elif elapsed_pct < next_cp["at_pct"] - 10:
            drift = "behind"
            suggestion = f"behind — next checkpoint {next_cp['at_pct']}% expects energy {next_cp['expected_energy']}"
        elif elapsed_pct > next_cp["at_pct"] + 10:
            drift = "ahead"
            suggestion = f"ahead of plan — slow down OR adjust expectations"
        else:
            drift = "on-target"
            suggestion = f"on-target — next checkpoint {next_cp['at_pct']}% expects energy {next_cp['expected_energy']}"

        return {
            "ok": True,
            "elapsed_pct": round(elapsed_pct, 1),
            "planned_minutes": arc.get("target_minutes", 0),
            "elapsed_minutes": round(elapsed_s / 60, 1),
            "next_checkpoint": next_cp,
            "drift": drift,
            "suggestion": suggestion,
        }
    except Exception as e:
        log.warning(f"[set-arc] progress failed: {e}")
        return {"ok": False, "drift": "no-arc", "suggestion": f"progress failed: {e}"}


def clear_set_arc() -> str:
    """Clear the planned arc."""
    sess = _session()
    if sess is None:
        return "session not registered"
    try:
        sess.set_arc = None
    except Exception as e:
        log.warning(f"[set-arc] clear failed: {e}")
        return f"clear failed: {e}"
    log.info("[set-arc] cleared")
    return "Arc cleared"
