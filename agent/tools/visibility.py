"""Visibility tools — let Treta see what her subagents are doing.

Treta is the root Being. The DJ agent, planner, and library manager are
her organs. Without these tools, every `[THINK:dj_treta]` /
`[THINK:planner]` / `[THINK:library_manager]` line in the thinking log
is invisible to her — she can't tell what her own organs are deciding.

This module is read-only: it inspects the thinking log, session state,
the scheduled-transition file, and the on-disk music library, and
returns a structured snapshot. It does NOT mutate anything.

Two tools:

  get_subagent_activity(window_minutes=5) -> dict
      One-shot snapshot across DJ / planner / library / session.

  tail_thinking_log(n=30, agent_filter="") -> list[str]
      Raw tail of the thinking log, optionally filtered to a single
      agent's [THINK:...] / [CALL:...] lines.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("dj-treta")


def _session():
    """Import-time-safe accessor for the Session singleton.

    Mirrors the helper in tools/directives.py — kept local on purpose so
    a cycle between visibility.py ↔ session_state.py can never form at
    import time.
    """
    try:
        from ..session_state import get_session
        return get_session()
    except Exception as exc:
        log.debug(f"visibility: get_session failed: {exc}")
        return None


def _runtime_path(name: str) -> Path | None:
    try:
        from ..runtime_paths import runtime_path
        return runtime_path(name)
    except Exception as exc:
        log.debug(f"visibility: runtime_path failed: {exc}")
        return None


# Approx avg cadence of a thinking-log line. Heartbeat ~5–15s, planner
# ~30s, but most lines are sub-second bursts inside a single tick. We
# only use this for "best-effort age" when seeking backwards.
_AVG_LINE_RATE_S = 0.5

# Regex to identify [THINK:agent] / [CALL:agent] markers.
_AGENT_TAG_RE = re.compile(r"\[(?:THINK|CALL):([^\]]+)\]")

# Planner cycle parsers.
_PLANNER_UNIQUE_RE = re.compile(r"union\+dedup\s*→\s*(\d+)\s*unique")
_PLANNER_MERGE_RE = re.compile(
    r"merge_against_local\s*→\s*(\d+)\s*downloaded[^\d]*(\d+)\s*need-download"
)
_PLANNED_AT_RE = re.compile(r'^\s*\{\s*"planned_at"')


def _read_tail_lines(path: Path, max_lines: int = 200, max_bytes: int = 1_000_000) -> list[str]:
    """Read up to `max_lines` lines from the tail of `path`.

    Bounded reader: never loads more than `max_bytes` from the end of
    the file, regardless of how many lines that yields. Returns lines
    in original order (most recent last), with trailing newlines stripped.
    """
    try:
        if not path or not path.exists():
            return []
        size = path.stat().st_size
        if size == 0:
            return []
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                # Drop the (likely partial) first line.
                f.readline()
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines
    except Exception as exc:
        log.debug(f"visibility: tail read failed for {path}: {exc}")
        return []


def _coarse_age_s(log_path: Path) -> int:
    """Coarse 'log was last touched N seconds ago' from mtime.

    Returns -1 if the file doesn't exist or stat fails.
    """
    try:
        if not log_path or not log_path.exists():
            return -1
        return max(0, int(time.time() - os.path.getmtime(log_path)))
    except Exception:
        return -1


def _find_log_path() -> Path | None:
    """Resolve the thinking-log path. Falls back gracefully."""
    # runtime_path adds the dj-treta- prefix automatically, so passing
    # "thinking.log" is enough.
    p = _runtime_path("thinking.log")
    if p is not None:
        return p
    return None


def _parse_planned_at(line: str) -> dict | None:
    """Parse a 'planned_at' JSON line; return dict or None on failure."""
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and "planned_at" in obj:
            return obj
    except Exception:
        return None
    return None


def _scan_dj_activity(lines: list[str], log_path: Path) -> dict:
    """Walk the tail of the thinking log and pull out DJ-relevant info."""
    last_call = ""
    last_call_idx = -1
    think_tail: list[str] = []

    for i, line in enumerate(lines):
        if "[CALL:dj_treta]" in line:
            last_call = line
            last_call_idx = i
        if "[THINK:dj_treta]" in line:
            think_tail.append(line)

    think_tail = think_tail[-5:]

    # Coarse age: if the [CALL:dj_treta] line is in the recent tail,
    # use mtime as the upper bound. If we never saw one in the tail,
    # we don't really know — return -1.
    if last_call_idx >= 0:
        # Lines from end: how far back the call sits.
        from_end = len(lines) - 1 - last_call_idx
        if from_end <= 10:
            age = _coarse_age_s(log_path)
        else:
            # Approximate by line position; capped to mtime so we don't
            # claim it's fresher than the file's last write.
            mtime_age = _coarse_age_s(log_path)
            approx = int(from_end * _AVG_LINE_RATE_S)
            age = max(mtime_age, approx) if mtime_age >= 0 else approx
    else:
        age = -1

    sess = _session()
    deferred_until_s: float | None = None
    if sess is not None:
        try:
            dj_def = float(getattr(sess, "dj_deferred_until", 0.0) or 0.0)
            delta = dj_def - time.time()
            if delta > 0:
                deferred_until_s = float(delta)
        except Exception:
            deferred_until_s = None

    return {
        "last_decision": last_call,
        "last_decision_age_s": int(age),
        "deferred_until_s": deferred_until_s,
        "thinking_tail": think_tail,
    }


def _scan_planner_activity(lines: list[str], log_path: Path) -> dict:
    """Walk the tail of the thinking log for planner-side info."""
    last_cycle_idx = -1
    candidates_total = 0
    downloaded = 0
    need_download = 0
    rank_1_path = ""
    reasoning_summary = ""

    # Walk forward, latest values overwrite earlier ones.
    for i, line in enumerate(lines):
        if "[THINK:planner]" in line and "cycle start" in line:
            last_cycle_idx = i
        m = _PLANNER_UNIQUE_RE.search(line)
        if m:
            try:
                candidates_total = int(m.group(1))
            except Exception:
                pass
        m = _PLANNER_MERGE_RE.search(line)
        if m:
            try:
                downloaded = int(m.group(1))
                need_download = int(m.group(2))
            except Exception:
                pass
        if _PLANNED_AT_RE.match(line):
            obj = _parse_planned_at(line.strip())
            if obj:
                # rank-1 path — try common shapes.
                playlist = obj.get("playlist") or obj.get("ranked") or []
                if isinstance(playlist, list) and playlist:
                    head = playlist[0]
                    if isinstance(head, dict):
                        rank_1_path = (
                            head.get("path")
                            or head.get("file_path")
                            or head.get("local_path")
                            or ""
                        )
                rs = obj.get("reasoning_summary") or obj.get("reasoning") or ""
                if isinstance(rs, str):
                    reasoning_summary = rs[:200]

    if last_cycle_idx >= 0:
        from_end = len(lines) - 1 - last_cycle_idx
        mtime_age = _coarse_age_s(log_path)
        approx = int(from_end * _AVG_LINE_RATE_S)
        last_cycle_age_s = max(mtime_age, approx) if mtime_age >= 0 else approx
    else:
        last_cycle_age_s = -1

    sess = _session()
    mood = ""
    last_error = ""
    if sess is not None:
        try:
            mood = getattr(sess, "mood", "") or ""
            last_error = getattr(sess, "last_planner_error", "") or ""
        except Exception:
            pass

    return {
        "last_cycle_age_s": int(last_cycle_age_s),
        "mood": mood,
        "candidates_total": int(candidates_total),
        "downloaded": int(downloaded),
        "need_download": int(need_download),
        "rank_1_path": rank_1_path,
        "reasoning_summary": reasoning_summary,
        "last_error": last_error,
    }


def _scan_library_activity(lines: list[str], window_minutes: int) -> dict:
    """Inspect the on-disk library + recent failure log lines."""
    in_flight = 0
    completed = 0
    failed = 0
    cutoff = time.time() - max(60, int(window_minutes) * 60)

    music_root = Path("~/Music/DJTreta").expanduser()
    if music_root.exists():
        try:
            in_flight = sum(1 for _ in music_root.glob("*/*.part"))
        except Exception as exc:
            log.debug(f"visibility: glob *.part failed: {exc}")
        try:
            for mp3 in music_root.glob("*/*.mp3"):
                try:
                    if mp3.stat().st_mtime >= cutoff:
                        completed += 1
                except Exception:
                    continue
        except Exception as exc:
            log.debug(f"visibility: glob *.mp3 failed: {exc}")

    for line in lines:
        if "Download failed" in line:
            failed += 1

    return {
        "downloads_in_flight": int(in_flight),
        "completed_last_5min": int(completed),
        "failed_last_5min": int(failed),
    }


def _read_scheduled_transition() -> dict | None:
    p = _runtime_path("scheduled-transition.json")
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.debug(f"visibility: scheduled-transition read failed: {exc}")
        return None


def _session_snapshot() -> dict:
    sess = _session()
    if sess is None:
        return {
            "emergency_count": 0,
            "agent_busy": False,
            "dj_deferred_until": 0.0,
            "mood": "",
            "mood_profile_slug": "",
            "tracks_played_count": 0,
        }

    try:
        emergency = int(getattr(sess, "emergency_count", 0) or 0)
    except Exception:
        emergency = 0
    try:
        agent_busy = bool(getattr(sess, "agent_busy", False))
    except Exception:
        agent_busy = False
    try:
        dj_def = float(getattr(sess, "dj_deferred_until", 0.0) or 0.0)
    except Exception:
        dj_def = 0.0
    try:
        mood = getattr(sess, "mood", "") or ""
    except Exception:
        mood = ""
    try:
        profile = getattr(sess, "mood_profile", None) or {}
        slug = profile.get("canonical_slug", "") if isinstance(profile, dict) else ""
    except Exception:
        slug = ""
    try:
        tracks = getattr(sess, "tracks_played", None)
        tracks_count = len(tracks) if tracks else 0
    except Exception:
        tracks_count = 0

    return {
        "emergency_count": emergency,
        "agent_busy": agent_busy,
        "dj_deferred_until": dj_def,
        "mood": mood,
        "mood_profile_slug": slug,
        "tracks_played_count": tracks_count,
    }


def _active_directives() -> list[dict]:
    sess = _session()
    if sess is None:
        return []
    try:
        return [
            dict(d) for d in getattr(sess, "directives", []) or []
            if isinstance(d, dict) and d.get("status") == "active"
        ]
    except Exception as exc:
        log.debug(f"visibility: directive read failed: {exc}")
        return []


def get_subagent_activity(window_minutes: int = 5) -> dict:
    """Structured snapshot of what every subagent is doing right now.

    Read-only. Inspects the thinking log, session state, scheduled-
    transition file, and the music library. Never mutates anything.

    Args:
        window_minutes: rolling window for library completion/failure
            counts. Default 5 minutes; clamped to >= 1.

    Returns:
        dict with keys: dj, planner, library, scheduled_transition,
        session, directives. See module docs for shape detail.
    """
    try:
        log_path = _find_log_path()
        # Pull a generous tail so the planner JSON line (which can be
        # long) is more likely to land inside it.
        lines = _read_tail_lines(log_path, max_lines=200) if log_path else []

        return {
            "dj": _scan_dj_activity(lines, log_path) if log_path else {
                "last_decision": "",
                "last_decision_age_s": -1,
                "deferred_until_s": None,
                "thinking_tail": [],
            },
            "planner": _scan_planner_activity(lines, log_path) if log_path else {
                "last_cycle_age_s": -1,
                "mood": "",
                "candidates_total": 0,
                "downloaded": 0,
                "need_download": 0,
                "rank_1_path": "",
                "reasoning_summary": "",
                "last_error": "",
            },
            "library": _scan_library_activity(lines, window_minutes),
            "scheduled_transition": _read_scheduled_transition(),
            "session": _session_snapshot(),
            "directives": _active_directives(),
        }
    except Exception as exc:
        log.warning(f"get_subagent_activity failed: {exc}")
        return {
            "dj": {
                "last_decision": "",
                "last_decision_age_s": -1,
                "deferred_until_s": None,
                "thinking_tail": [],
            },
            "planner": {
                "last_cycle_age_s": -1,
                "mood": "",
                "candidates_total": 0,
                "downloaded": 0,
                "need_download": 0,
                "rank_1_path": "",
                "reasoning_summary": "",
                "last_error": "",
            },
            "library": {
                "downloads_in_flight": 0,
                "completed_last_5min": 0,
                "failed_last_5min": 0,
            },
            "scheduled_transition": None,
            "session": _session_snapshot(),
            "directives": [],
        }


def tail_thinking_log(n: int = 30, agent_filter: str = "") -> list[str]:
    """Read the last N lines of the thinking log, optionally filtered.

    Args:
        n: how many lines to return. Clamped to [1, 200].
        agent_filter: when set, only return lines whose agent tag
            ([THINK:<filter>] or [CALL:<filter>]) matches this name.
            Examples: 'dj_treta', 'planner', 'library_manager',
            'treta'. Empty string returns all lines unfiltered.

    Returns:
        List of stripped log lines, oldest first. Most recent line is
        last. Empty list when the log is missing or unreadable.
    """
    try:
        n = max(1, min(int(n), 200))
    except Exception:
        n = 30

    log_path = _find_log_path()
    if log_path is None:
        return []

    # Read more than n if filtering, so the post-filter tail still has
    # ~n lines for that agent. Cap at 200 per the contract.
    raw_n = 200 if agent_filter else n
    lines = _read_tail_lines(log_path, max_lines=raw_n)

    if not agent_filter:
        return lines[-n:]

    needles = (f"[THINK:{agent_filter}]", f"[CALL:{agent_filter}]")
    filtered = [ln for ln in lines if any(s in ln for s in needles)]
    return filtered[-n:]
