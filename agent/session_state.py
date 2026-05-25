"""Single source of truth for DJ Treta live state.

Replaces scattered `self.mood`, `self.tracks_played`, `self.current_set`, etc.
on DJTretaBeing, plus file-based IPC (/tmp/dj-treta-state.json,
/tmp/dj-treta-playlist.json, /tmp/dj-treta-mood-change.json,
/tmp/dj-treta-directives.json, /tmp/dj-treta-scheduled-transition.json).

All live state reads/writes go through Session. On mutation, state is
persisted to .beings/session.json — critical fields synchronously,
transients debounced 500ms.

Usage:
    session = Session.load(Path(".beings/session.json"))
    session.mood = "BollyAfro"                # triggers flush
    session.tracks_played.append(track_dict)  # observed list, triggers flush
    session.register_callback("mood", on_mood_change)
    session.flush()                           # force immediate write

Thread safety: all reads/writes hold an internal RLock. List/dict observers
fire a dirty-flag bump which the background flush thread coalesces.

Callbacks fire synchronously inside the write lock — keep them fast; for
expensive work (e.g. LLM mood resolver) spawn a thread inside the callback.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("dj-treta")

# Fields that bypass the 500ms debounce and write to disk immediately.
# Losing these to a crash window would corrupt set history, mood intent, or
# listener-visible state.
CRITICAL_FIELDS = frozenset({
    "mood", "mood_profile", "current_set", "tracks_played",
    "planner_directive", "dj_directive", "user_intent",
    # Typed directive queue — surgical actions (load_track, transition_now)
    # must be durable across crash windows or the named track is lost.
    "directives",
    # Self-scheduling — Treta wakes herself for reasons. Must persist or
    # she forgets her own intent across daemon restarts.
    "self_schedule",
    # Set-arc plan — pre-committed energy curve for the set. Persisted so
    # progress checks survive restarts.
    "set_arc",
    # Meta-control flags — pause/resume of subagents. Read at top of
    # each loop; durable so an in-progress pause survives a restart.
    "planner_paused", "dj_paused", "library_paused",
    # Sarathi Mode flag — durable so the daemon boots back into the
    # same mode after a restart mid-set.
    "sarathi_mode",
    # Deck-ownership signals (Phase A1) — consumers may depend on these
    # being durable before the next heartbeat tick, so sync-flush on write.
    "idle_needs_load", "user_skip", "set_ending",
})

DEBOUNCE_SECONDS = 0.5


class ObservedList(list):
    """list subclass that calls `on_mutate()` after every mutation.

    Used for tracks_played and chat_history so `session.tracks_played.append(x)`
    triggers a flush without the caller having to call session.flush() manually.
    """

    def __init__(self, iterable=(), on_mutate: Optional[Callable] = None):
        super().__init__(iterable)
        # Use object.__setattr__ to avoid hitting any class __setattr__ we add later
        object.__setattr__(self, "_on_mutate", on_mutate)

    def _fire(self):
        cb = getattr(self, "_on_mutate", None)
        if cb is not None:
            cb()

    def append(self, item):
        super().append(item)
        self._fire()

    def extend(self, iterable):
        super().extend(iterable)
        self._fire()

    def insert(self, index, item):
        super().insert(index, item)
        self._fire()

    def pop(self, *a, **kw):
        result = super().pop(*a, **kw)
        self._fire()
        return result

    def remove(self, value):
        super().remove(value)
        self._fire()

    def clear(self):
        super().clear()
        self._fire()

    def __setitem__(self, index, value):
        super().__setitem__(index, value)
        self._fire()

    def __delitem__(self, index):
        super().__delitem__(index)
        self._fire()

    def __iadd__(self, other):
        result = super().__iadd__(other)
        self._fire()
        return result


# Default values for every Session field.
# Callable values are factories invoked once per Session (e.g. list for a fresh
# empty list); non-callable values are used as-is.
_FIELD_DEFAULTS: dict[str, Any] = {
    # Intent
    "mood": "",
    "mood_profile": None,          # populated by Phase 2 LLM mood resolver
    "user_intent": "",
    "planner_directive": "",
    "dj_directive": "",

    # Typed directive queue — replaces fire-once free-text directives for
    # surgical actions. Each entry:
    #   {id, kind, target, payload, status, created_at, expires_at}
    # kind ∈ {"load_track", "transition_now", "shape"}
    # status ∈ {"active", "satisfied", "expired", "superseded"}
    # The `planner_directive` and `dj_directive` strings above are kept as
    # legacy mirrors of the latest active "shape" directive for prompt
    # rendering and TUI/WS observability. Surgical kinds (load_track,
    # transition_now) are consumed programmatically, never via prompt.
    "directives": list,

    # Self-scheduling queue — Treta wakes herself at specific times for
    # specific reasons. Each entry:
    #   {at_ts, reason, callback_directive, fired, created_at}
    # Heartbeat reads at top of every tick and fires due entries. A fired
    # entry stays in the list until pruned (so we can audit what Treta
    # asked herself to do).
    "self_schedule": list,

    # Pre-committed set arc — energy curve + ending style for the set
    # Treta is currently running. Heartbeat reads progress against arc
    # and auto-emits shape directives when drift > 20%. Schema:
    #   {target_minutes, energy_curve, ending_style, started_at,
    #    checkpoints: [{at_pct, expected_energy, hit_at, observed_energy}]}
    # None when no arc is in flight.
    "set_arc": None,

    # Reflection log — synthesized output from the 15-min reflection
    # loop. Capped at 20 entries (FIFO). Each entry:
    #   {ts, went_well: [], to_improve: [], next_intent: str,
    #    mood_drift: str, listener_engagement_delta: int}
    "reflections": list,

    # Meta-control flags — Treta pauses subagents when she wants to
    # take direct control. Each subagent loop checks its flag at the
    # top of its cycle and skips work when paused. Defaults False.
    "planner_paused": False,
    "dj_paused": False,
    "library_paused": False,

    # Sarathi Mode — copilot. Manish drives transitions on the FLX4;
    # Treta does everything else (load, plan, library) and SUGGESTS
    # transitions rather than executing them. Default ON — autonomous
    # is the opt-in (djtreta mode autonomous). See docs/sarathi-mode.md.
    # When True:
    #   - DJ agent emits suggest_transition (transition_suggestion
    #     directive) instead of schedule_transition
    #   - heartbeat P2 emergency safety net tightens to remaining<12s
    #   - heartbeat P4 skips while manish_in_motion
    "sarathi_mode": True,
    # True while Manish is physically working a transition on the FLX4
    # (detected via crossfader/deck deltas). Suppresses P4 + new
    # suggestions so Treta stays quiet during his mix. Auto-clears at
    # manish_motion_until.
    "manish_in_motion": False,
    "manish_motion_until": 0.0,

    # Playback state
    "tracks_played": list,
    "current_deck": 0,
    "current_track_path": "",
    "current_position_s": 0.0,

    # Set / session identity
    "current_set": None,

    # Signals (event bus for Phase 3-6)
    "replan_requested": False,
    "library_need": None,
    "library_ready": None,          # K5: library agent emits on successful download
    "library_need_failed": None,    # K5: library agent emits after 3 download failures
    "producer_need": None,

    # Deck-ownership signals (Phase A1 of deck-ownership sub-plan).
    # DJ agent consumes these via heartbeat P4; Python watchdog falls back
    # only on stuck signals. See APPENDIX A of the plan for consumers.
    "idle_needs_load": False,   # set when idle deck should get a fresh track
    "user_skip": None,          # {style: "fast"|"smooth", ts: float, directive: str|None}
    "set_ending": False,        # set when elapsed > target_minutes - 5

    # Co-being deck ownership (Phase 7).
    # Map deck number (int) → being_id (str). "treta" or absent means DJ Treta owns.
    # Anything else means that being_id has claimed the deck via MCP and DJ Treta
    # must not auto-load or auto-transition it. Synced from
    # /tmp/dj-treta-deck-ownership.json at the top of every heartbeat tick.
    "deck_ownership": dict,

    # Planner output (Phase 3)
    "playlist": None,
    "playlist_updated_at": 0.0,
    "last_planner_error": "",

    # --- E3/E5 ---  Rolling arrangement plan (the leapfrog). A short sequence
    # of musical intents toward a goal, re-derived every planner cycle.
    # Transient (not persisted) — it's regenerated within ~15s of any restart.
    # Plain dict (ArrangementPlan.to_dict()); Agent C maps intents onto its
    # mixer-State model at integration. See agent/arrangement.py.
    "arrangement_plan": None,
    "arrangement_plan_updated_at": 0.0,

    # Subsystem health (Phase 3.5)
    "knowledge_health": None,

    # Issue #76: DJ defer-decision gate. P4 skips DJ invoke when
    # now < this. Set by the defer_decision tool; naturally ages out.
    "dj_deferred_until": 0.0,

    # Housekeeping
    "chat_history": list,
    "emergency_count": 0,
    "last_reflect_count": 0,
    "saved_at": 0.0,
}


class Session:
    """Single source of truth for DJ Treta live state.

    Instantiate via `Session.load(path)`. Read/write attributes normally;
    mutations are persisted to `path` automatically.
    """

    def __init__(self, path: Path):
        # Everything assigned via object.__setattr__ bypasses our __setattr__
        # hook, which is what we want for internal bookkeeping fields.
        object.__setattr__(self, "_path", Path(path))
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_dirty", False)
        object.__setattr__(self, "_callbacks", {})  # {field_name: [callback]}
        object.__setattr__(self, "_stop_event", threading.Event())
        object.__setattr__(self, "_closed", False)

        # Initialize every declared field to its default.
        for name, default in _FIELD_DEFAULTS.items():
            value = default() if callable(default) else default
            if isinstance(value, list):
                value = ObservedList(value, on_mutate=self._mark_dirty)
            object.__setattr__(self, name, value)

        # Start background flush thread.
        t = threading.Thread(
            target=self._flush_loop, daemon=True, name="session-flush",
        )
        object.__setattr__(self, "_flush_thread", t)
        t.start()

        # Ensure a final flush runs at process exit so no dirty state is lost
        # in the 500ms debounce window.
        atexit.register(self._flush_on_exit)

    # ── Mutation path ─────────────────────────────────────────────────

    def __setattr__(self, name: str, value: Any) -> None:
        # Internal bookkeeping fields (prefixed with _) bypass the observer.
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return

        # Unknown field → allow but warn. Helps catch typos without preventing
        # ad-hoc extension.
        if name not in _FIELD_DEFAULTS:
            log.warning(f"Session: setting undeclared field {name!r}")

        with self._lock:
            old = getattr(self, name, None)
            if _deep_equal(old, value):
                return  # no-op — skip callbacks and flush

            # Wrap lists so nested mutations (append, etc.) bubble up.
            if isinstance(value, list) and not isinstance(value, ObservedList):
                value = ObservedList(value, on_mutate=self._mark_dirty)

            object.__setattr__(self, name, value)

            # Fire callbacks registered for this field.
            for cb in self._callbacks.get(name, []):
                try:
                    cb(name, old, value)
                except Exception as exc:
                    log.warning(f"Session callback for {name!r} raised: {exc}")

            self._dirty = True

            # Critical fields flush synchronously so a crash doesn't lose them.
            if name in CRITICAL_FIELDS:
                self._flush_locked()

    def _mark_dirty(self) -> None:
        """Called by ObservedList on mutation — set dirty flag."""
        with self._lock:
            self._dirty = True

    # ── Explicit API ──────────────────────────────────────────────────

    def flush(self) -> None:
        """Force an immediate write to disk."""
        with self._lock:
            self._flush_locked()

    def register_callback(self, field_name: str, callback: Callable) -> None:
        """Register `callback(name, old, new)` to fire on writes to `field_name`.

        Callbacks fire synchronously inside the write lock. For expensive work,
        spawn a thread inside the callback.
        """
        with self._lock:
            self._callbacks.setdefault(field_name, []).append(callback)

    def to_dict(self) -> dict:
        """Snapshot of every declared field as a plain dict."""
        with self._lock:
            return {
                name: _serialize(getattr(self, name))
                for name in _FIELD_DEFAULTS
            }

    def close(self) -> None:
        """Stop the flush thread. Call at shutdown."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            if self._dirty:
                self._flush_locked()

    # ── Typed directive queue ─────────────────────────────────────────
    #
    # Why this exists: free-text directives (planner_directive,
    # dj_directive) are interpreted by an LLM at the action layer.
    # Models acknowledge the directive in reasoning_summary then pick a
    # different action. Surgical actions (load this specific track now,
    # transition into deck N) must be Python-enforced, not LLM-enforced.
    # Free text is reserved for shaping intent ("less vocals", "keep
    # energy high").
    #
    # Lifecycle: pending → active → satisfied | expired | superseded.
    # `add_directive` writes status="active". Consumers call
    # `mark_satisfied(id)` once the action completes (Mixxx confirms
    # load, transition fires, etc.). `expire_stale` runs at the top of
    # every planner tick + heartbeat to retire past-TTL entries.

    _DIRECTIVE_QUEUE_CAP = 16   # FIFO eviction of satisfied/expired beyond this

    def add_directive(
        self,
        kind: str,
        payload: dict,
        target: str = "both",
        ttl_seconds: Optional[float] = None,
        supersede_kinds: Optional[list[str]] = None,
    ) -> str:
        """Append a typed directive to the queue, return its id.

        Args:
            kind: "load_track", "transition_now", or "shape".
            payload: kind-specific dict (see plan for shapes).
            target: "planner", "dj", or "both" — who consumes this.
            ttl_seconds: auto-expire after N seconds. None = no expiry.
            supersede_kinds: when set, mark prior `active` directives of
                these kinds as `superseded`. Use ["load_track"] when a
                new load request should cancel the previous one.

        Returns:
            The new directive's id (string).
        """
        with self._lock:
            now = time.time()
            new_id = f"d{int(now * 1000)}_{len(self.directives)}"
            entry = {
                "id": new_id,
                "kind": kind,
                "target": target,
                "payload": dict(payload or {}),
                "status": "active",
                "created_at": now,
                "expires_at": (now + ttl_seconds) if ttl_seconds else None,
            }

            if supersede_kinds:
                for d in self.directives:
                    if d.get("status") == "active" and d.get("kind") in supersede_kinds:
                        d["status"] = "superseded"

            # Mirror "shape" directives into the legacy string fields so
            # existing prompt-render and TUI code keeps working unchanged.
            if kind == "shape":
                text = (payload or {}).get("text", "")
                if target in ("planner", "both"):
                    object.__setattr__(self, "planner_directive", text)
                if target in ("dj", "both"):
                    object.__setattr__(self, "dj_directive", text)

            new_list = list(self.directives) + [entry]
            # FIFO-evict completed entries when over cap.
            if len(new_list) > self._DIRECTIVE_QUEUE_CAP:
                # Keep all active first; evict satisfied/expired/superseded
                # in insertion order until we're back under the cap.
                actives = [d for d in new_list if d.get("status") == "active"]
                completed = [d for d in new_list if d.get("status") != "active"]
                room = max(0, self._DIRECTIVE_QUEUE_CAP - len(actives))
                completed = completed[-room:] if room else []
                new_list = completed + actives

            object.__setattr__(self, "directives", ObservedList(new_list, on_mutate=self._mark_dirty))
            self._dirty = True
            self._flush_locked()  # critical field — durable immediately
            return new_id

    def find_active_directive(
        self,
        kind: str,
        target: Optional[str] = None,
        deck: Optional[int] = None,
    ) -> Optional[dict]:
        """Return the oldest active directive matching the filters, or None.

        target=None matches any target. deck=None means don't filter by
        deck. When deck is given, also matches directives whose
        payload.deck is None (= "any idle deck").
        """
        with self._lock:
            for d in self.directives:
                if d.get("status") != "active":
                    continue
                if d.get("kind") != kind:
                    continue
                if target is not None:
                    t = d.get("target") or "both"
                    if t not in (target, "both"):
                        continue
                if deck is not None:
                    payload_deck = (d.get("payload") or {}).get("deck")
                    if payload_deck not in (None, deck):
                        continue
                return dict(d)
        return None

    def mark_satisfied(self, directive_id: str) -> bool:
        """Mark a directive `satisfied`. Returns True if found and was active."""
        with self._lock:
            new_list = []
            changed = False
            for d in self.directives:
                if d.get("id") == directive_id and d.get("status") == "active":
                    d = dict(d)
                    d["status"] = "satisfied"
                    d["satisfied_at"] = time.time()
                    changed = True
                new_list.append(d)
            if changed:
                object.__setattr__(self, "directives", ObservedList(new_list, on_mutate=self._mark_dirty))
                self._dirty = True
                self._flush_locked()
            return changed

    def expire_stale(self) -> int:
        """Move past-TTL active directives to `expired`. Return count expired."""
        now = time.time()
        with self._lock:
            new_list = []
            count = 0
            for d in self.directives:
                if d.get("status") == "active" and d.get("expires_at") is not None:
                    if d["expires_at"] <= now:
                        d = dict(d)
                        d["status"] = "expired"
                        count += 1
                new_list.append(d)
            if count:
                object.__setattr__(self, "directives", ObservedList(new_list, on_mutate=self._mark_dirty))
                # Re-resolve legacy mirrors so prompt rendering doesn't keep
                # showing the text of an expired shape directive.
                self._resolve_shape_mirror_locked()
                self._dirty = True
                self._flush_locked()
            return count

    def _resolve_shape_mirror_locked(self) -> None:
        """Sync legacy planner_directive / dj_directive strings to the
        latest active 'shape' directive per target. Caller holds _lock.

        When no active shape directive exists for a target, the mirror
        is cleared. This keeps prompt-render code (which still reads the
        legacy strings) honest about whether a shape directive is live.
        """
        latest_planner = ""
        latest_dj = ""
        for d in self.directives:
            if d.get("status") != "active" or d.get("kind") != "shape":
                continue
            text = (d.get("payload") or {}).get("text", "")
            target = d.get("target") or "both"
            if target in ("planner", "both"):
                latest_planner = text
            if target in ("dj", "both"):
                latest_dj = text
        object.__setattr__(self, "planner_directive", latest_planner)
        object.__setattr__(self, "dj_directive", latest_dj)

    def clear_directive_queue(self) -> int:
        """Remove all directives. Return count removed."""
        with self._lock:
            n = len(self.directives)
            object.__setattr__(self, "directives", ObservedList([], on_mutate=self._mark_dirty))
            object.__setattr__(self, "planner_directive", "")
            object.__setattr__(self, "dj_directive", "")
            self._dirty = True
            self._flush_locked()
            return n

    # ── Persistence ───────────────────────────────────────────────────

    def _flush_locked(self) -> None:
        """Write state to disk. Caller must hold `_lock`."""
        data = self.to_dict()
        data["saved_at"] = time.time()

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as exc:
            log.warning(f"Session flush failed: {exc}")

    def _flush_loop(self) -> None:
        """Background thread: flush if dirty, every DEBOUNCE_SECONDS."""
        while not self._stop_event.is_set():
            self._stop_event.wait(DEBOUNCE_SECONDS)
            if self._stop_event.is_set():
                break
            with self._lock:
                if self._dirty:
                    self._flush_locked()

    def _flush_on_exit(self) -> None:
        """Final flush at process exit."""
        try:
            self.close()
        except Exception:
            pass

    # ── Factory ───────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Session":
        """Load session from disk, or create fresh if file missing/corrupt.

        Fields missing from the JSON keep their defaults — allows additive
        schema evolution without migrations.
        """
        path = Path(path)
        session = cls(path)

        if not path.exists():
            log.info(f"Session: starting fresh at {path}")
            return session

        try:
            raw = json.loads(path.read_text())
        except Exception as exc:
            log.warning(f"Session: failed to load {path} ({exc}) — starting fresh")
            return session

        for name in _FIELD_DEFAULTS:
            if name not in raw:
                continue
            value = raw[name]
            if isinstance(value, list):
                value = ObservedList(value, on_mutate=session._mark_dirty)
            object.__setattr__(session, name, value)

        object.__setattr__(session, "_dirty", False)
        log.info(f"Session: loaded {path}")
        return session


# ── Helpers ───────────────────────────────────────────────────────────

def _serialize(value: Any) -> Any:
    """Convert a Session field to JSON-friendly form."""
    if isinstance(value, ObservedList):
        return list(value)
    return value


def _deep_equal(a: Any, b: Any) -> bool:
    """Deep equality for basic JSON-friendly structures."""
    if type(a) != type(b):
        # list and ObservedList should compare equal when contents match
        if isinstance(a, list) and isinstance(b, list):
            return list(a) == list(b)
        return False
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_deep_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_deep_equal(a[k], b[k]) for k in a)
    return a == b


# ── Module-level singleton for tools ──────────────────────────────────
#
# Tools invoked by LLM agents (e.g. set_mood, set_dj_directive) need access to
# the Session without having a reference to the DJTretaBeing instance. Main
# registers the session at startup; tools call get_session() to read/write.

_session_instance: Optional[Session] = None
_singleton_lock = threading.Lock()


def register_session(session: Session) -> None:
    """Register the module-level Session singleton.

    Called exactly once by DJTretaBeing.__init__ after Session.load().
    Tools that need to mutate session state import get_session() from here.
    """
    global _session_instance
    with _singleton_lock:
        if _session_instance is not None and _session_instance is not session:
            log.warning("Session singleton re-registered — replacing previous instance")
        _session_instance = session


def get_session() -> Optional[Session]:
    """Get the registered Session singleton, or None if not yet registered.

    Tools should handle the None case defensively during daemon startup.
    """
    return _session_instance
