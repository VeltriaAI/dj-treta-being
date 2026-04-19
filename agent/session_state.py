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
    "producer_need": None,

    # Deck-ownership signals (Phase A1 of deck-ownership sub-plan).
    # DJ agent consumes these via heartbeat P4; Python watchdog falls back
    # only on stuck signals. See APPENDIX A of the plan for consumers.
    "idle_needs_load": False,   # set when idle deck should get a fresh track
    "user_skip": None,          # {style: "fast"|"smooth", ts: float, directive: str|None}
    "set_ending": False,        # set when elapsed > target_minutes - 5

    # Planner output (Phase 3)
    "playlist": None,
    "playlist_updated_at": 0.0,
    "last_planner_error": "",

    # Subsystem health (Phase 3.5)
    "knowledge_health": None,

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
