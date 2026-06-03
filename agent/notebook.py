"""Durable global-workspace substrate for DJ Treta — the Notebook (v11 Phase 1).

A sibling to `Session` (`agent/session_state.py`). Where `Session` is the
*materialized now-state* (overwrite-only, whole-file JSON, ~45 typed fields),
the Notebook is the *append-only event log* — durable history that survives a
restart (unlike `thinking.log`, truncated every boot).

  - Substrate = append-only JSONL at `runtime_path("events.jsonl")`
    (decision-doc locked; SQLite-unification is v12, not a blocker).
  - In-memory `deque(maxlen=ring_size)` ring for cheap tail/find/now-view reads
    (no disk hit on the read path).
  - Monotonic, gap-free `seq` across restart (seeded from `replay()`).
  - A salience-callback bus, registered-but-DORMANT in Phase 1 (no callbacks
    registered yet — wired in Phase 2's autonomous-wake).

Schema of each event (one JSON line):
    {seq, ts, author, kind, payload, salience, confidence, event_id}
  kind ∈ {percept, decision, transition, claim, directive,
          generated_track, reflection}

Thread safety: the Notebook owns its OWN `threading.Lock` (NOT Session's
RLock). `append()` is one line + flush; compaction (the only whole-file
rewrite) is off the hot path and caller-gated (hourly). This separation
guarantees an append NEVER blocks behind a Session flush.

Audio-thread safety (CRITICAL): every disk write is wrapped in try/except —
a notebook fault NEVER raises to the caller, so it can never break audio or
billing. `replay()` is pure in-memory (zero Mixxx calls). Callbacks, when
registered (Phase 2), spawn threads and never block the appender.

Usage:
    nb = Notebook(runtime_path("events.jsonl"))
    nb.replay()                       # seed seq + refill ring after restart
    register_notebook(nb)             # module singleton for tool access
    nb.append(author="dj", kind="decision", payload={"action": "load_track"})
    nb.tail(10, kinds={"transition"})
    view = nb.now_view()              # cheap derived projection
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .session_state import get_session

log = logging.getLogger("dj-treta")

# Default salience per kind. A percept-storm (3 Hz room-sense) is cheap (0.2);
# a directive or a generated track is worth a closer look (0.6). Phase 2's
# salience module refines these per-event; these are the floor.
SALIENCE_DEFAULTS: dict[str, float] = {
    "percept": 0.2,
    "decision": 0.4,
    "transition": 0.5,
    "claim": 0.5,
    "directive": 0.6,
    "generated_track": 0.6,
    "reflection": 0.3,
}

# The closed set of event kinds. Unknown kinds are still appended (additive,
# never reject a write) but fall back to a neutral default salience.
KINDS = frozenset(SALIENCE_DEFAULTS.keys())

# Within this window, two consecutive ring events sharing the same dedup_key
# collapse to one — kills percept-storms (room-sense firing ~3 Hz).
_DEDUP_WINDOW_S = 2.0

# now_view() is a pure derived projection; cache it briefly so a tight read
# loop (e.g. multiple prompt builders in one tick) doesn't re-fold the ring.
_NOW_VIEW_TTL_S = 0.5

# Default ceiling for the on-disk JSONL before compact() rewrites it.
_DEFAULT_MAX_DISK_LINES = 50_000


class Notebook:
    """Append-only event log + in-memory ring + cheap now-view projection.

    Owns its own `threading.Lock` (separate from Session's RLock) so an
    append never blocks behind a Session flush. Instantiate, then call
    `replay()` to seed `seq` and refill the ring from disk before the first
    `append()`.
    """

    def __init__(
        self,
        path: Path,
        ring_size: int = 200,
        max_disk_lines: int = _DEFAULT_MAX_DISK_LINES,
    ):
        self._path = Path(path)
        self._ring_size = int(ring_size)
        self._max_disk_lines = int(max_disk_lines)

        # Notebook's OWN lock — NOT Session's RLock. A plain Lock is enough:
        # the hot path is non-reentrant (append never re-enters append).
        self._lock = threading.Lock()

        # In-memory ring of the most-recent events. Cheap reads land here;
        # disk is only touched on append (one line) and compaction.
        self._ring: collections.deque = collections.deque(maxlen=self._ring_size)

        # Monotonic, gap-free sequence number. Seeded by replay() to the max
        # seq seen on disk so it stays gap-free across restart.
        self._seq: int = 0

        # Append file handle, opened lazily on first append (and re-opened
        # after compaction). None until first write.
        self._fh = None

        # Salience-callback bus: list of (callback, threshold). Registered in
        # Phase 1 but DORMANT — fired from append() once Phase 2 wires wakes.
        self._salience_callbacks: list[tuple[Callable, float]] = []

        # now_view() TTL cache.
        self._now_view_cache: Optional[dict] = None
        self._now_view_cached_at: float = 0.0

        self._closed = False

    # ── Append path (hot) ─────────────────────────────────────────────

    def append(
        self,
        *,
        author: str,
        kind: str,
        payload: Any,
        salience: Optional[float] = None,
        confidence: float = 1.0,
        dedup_key: Optional[str] = None,
    ) -> int:
        """Append one event. Return its seq (or the last seq if deduped).

        Args:
            author: who posted it (e.g. "dj", "planner", "being:wake").
            kind: one of KINDS (unknown kinds allowed, neutral salience).
            payload: arbitrary JSON-friendly value (dict/list/str/num).
            salience: override; defaults from SALIENCE_DEFAULTS[kind].
            confidence: writer's confidence in the event (0..1).
            dedup_key: if the last ring event had the same key within
                _DEDUP_WINDOW_S, skip the write and return that seq —
                collapses percept-storms.

        Never raises: the disk write is try/except-wrapped so a notebook
        fault can NEVER break the caller (audio/billing).
        """
        with self._lock:
            # Dedup: collapse a same-key event that immediately follows
            # another within the window (the last ring event only — this is
            # a storm-suppressor, not a global de-dup index).
            if dedup_key is not None and self._ring:
                last = self._ring[-1]
                if (
                    last.get("dedup_key") == dedup_key
                    and (time.time() - last.get("ts", 0.0)) <= _DEDUP_WINDOW_S
                ):
                    return last.get("seq", self._seq)

            if salience is None:
                salience = SALIENCE_DEFAULTS.get(kind, 0.3)

            seq = self._seq + 1
            event = {
                "seq": seq,
                "ts": time.time(),
                "author": author,
                "kind": kind,
                "payload": payload,
                "salience": salience,
                "confidence": confidence,
                "event_id": uuid.uuid4().hex,
            }
            # dedup_key is kept on the ring copy (for the next dedup check)
            # but only written to disk if present — keeps the log clean.
            ring_event = dict(event)
            if dedup_key is not None:
                ring_event["dedup_key"] = dedup_key
                event["dedup_key"] = dedup_key

            # ONE json line + flush. Wrapped so a write fault never raises:
            # the ring + seq still advance so in-memory consumers stay correct.
            self._write_line_locked(event)

            self._seq = seq
            self._ring.append(ring_event)
            self._now_view_cache = None  # invalidate derived projection

            # Fire salience callbacks (>= threshold). Registered-but-dormant
            # in Phase 1 (no callbacks yet). Each callback is responsible for
            # offloading expensive work to a thread — see register_callback
            # in session_state.py for the pattern.
            if salience is not None and self._salience_callbacks:
                for cb, threshold in self._salience_callbacks:
                    if salience >= threshold:
                        try:
                            cb(ring_event)
                        except Exception as exc:
                            log.warning(f"Notebook salience callback raised: {exc}")

            return seq

    def _write_line_locked(self, event: dict) -> None:
        """Write one JSON line + flush. Caller holds `_lock`.

        Opens the append handle lazily. NEVER raises — a notebook write
        fault must not break audio/billing.
        """
        try:
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._path, "a", encoding="utf-8")
            self._fh.write(json.dumps(event, default=str) + "\n")
            self._fh.flush()
        except Exception as exc:
            log.warning(f"Notebook append write failed: {exc}")

    # ── Read path (ring only, cheap) ───────────────────────────────────

    def tail(self, n: int = 20, kinds: Optional[Any] = None) -> list[dict]:
        """Return up to the last `n` ring events, optionally filtered by kind.

        Reads the in-memory ring ONLY (never disk). `kinds` may be any
        iterable (set/list/frozenset) of kind strings.
        """
        with self._lock:
            events = list(self._ring)
        if kinds is not None:
            kindset = set(kinds)
            events = [e for e in events if e.get("kind") in kindset]
        if n is None or n < 0:
            return events
        return events[-n:]

    def find_last(self, kind: str, author: Optional[str] = None) -> Optional[dict]:
        """Return the newest ring event matching `kind` (and `author`), or None."""
        with self._lock:
            for e in reversed(self._ring):
                if e.get("kind") != kind:
                    continue
                if author is not None and e.get("author") != author:
                    continue
                return dict(e)
        return None

    def now_view(self) -> dict:
        """Cheap derived projection of the live workspace.

        Folds `get_session()` (now_playing / up_next / room_sense / mood)
        with the ring tail. Pure/derived — owns NO state. TTL-cached ~0.5s.
        Tolerates `get_session()` returning None (startup window).
        """
        now = time.time()
        with self._lock:
            if (
                self._now_view_cache is not None
                and (now - self._now_view_cached_at) <= _NOW_VIEW_TTL_S
            ):
                return self._now_view_cache
            recent = list(self._ring)[-8:]

        now_playing: Any = None
        up_next: Any = None
        room_sense: Any = None
        mood: Any = None
        try:
            session = get_session()
        except Exception:
            session = None
        if session is not None:
            # now_playing / up_next are NOT first-class Session fields; derive
            # them defensively. now_playing ← current track; up_next ← head of
            # the planner playlist if present.
            now_playing = getattr(session, "current_track_path", None) or None
            playlist = getattr(session, "playlist", None)
            if isinstance(playlist, dict):
                tracks = playlist.get("tracks") or playlist.get("up_next")
                if isinstance(tracks, list) and tracks:
                    up_next = tracks[0]
            elif isinstance(playlist, list) and playlist:
                up_next = playlist[0]
            room_sense = getattr(session, "room_sense", None)
            mood = getattr(session, "mood", None) or None

        view = {
            "now_playing": now_playing,
            "up_next": up_next,
            "room_sense": room_sense,
            "mood": mood,
            "recent": recent,
        }
        with self._lock:
            self._now_view_cache = view
            self._now_view_cached_at = now
        return view

    # ── Durability: replay + compaction ────────────────────────────────

    def replay(self) -> int:
        """Stream the JSONL file, seed `_seq`, refill the ring. Return count.

        Pure in-memory — ZERO Mixxx calls. Skips malformed lines (incl. a
        truncated final line from a crash mid-write, mirroring Session.load).
        `_seq` is seeded to the max seq seen so it stays gap-free across
        restart; the ring is refilled with the last `ring_size` events.
        """
        if not self._path.exists():
            return 0

        count = 0
        max_seq = 0
        # Refill the ring with only the tail; a bounded deque does this for free.
        ring: collections.deque = collections.deque(maxlen=self._ring_size)
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        # Skip a truncated/corrupt line (e.g. crash mid-write),
                        # just like Session.load tolerates a bad final read.
                        continue
                    count += 1
                    seq = event.get("seq")
                    if isinstance(seq, int) and seq > max_seq:
                        max_seq = seq
                    ring.append(event)
        except Exception as exc:
            log.warning(f"Notebook replay failed: {exc}")
            return count

        with self._lock:
            self._seq = max(self._seq, max_seq)
            self._ring = ring
            self._now_view_cache = None
        return count

    def compact(self) -> int:
        """If the log exceeds max_disk_lines, rewrite keeping the newest N.

        The ONLY whole-file rewrite, and OFF the hot path — the caller gates
        this (hourly). Returns the number of lines DROPPED (0 if no rewrite).
        Never raises; a compaction fault leaves the original file intact.
        """
        try:
            if not self._path.exists():
                return 0

            # Cheap line count without holding the entire file in memory twice.
            total = 0
            with open(self._path, "r", encoding="utf-8") as fh:
                for _ in fh:
                    total += 1
            if total <= self._max_disk_lines:
                return 0

            keep = self._max_disk_lines
            kept_lines = collections.deque(maxlen=keep)
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        kept_lines.append(line if line.endswith("\n") else line + "\n")

            with self._lock:
                # Close the live append handle so we can atomically swap the
                # file out from under it, then reopen on next append.
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None

                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as out:
                    out.writelines(kept_lines)
                import os as _os
                _os.replace(tmp, self._path)

            return total - len(kept_lines)
        except Exception as exc:
            log.warning(f"Notebook compact failed: {exc}")
            return 0

    # ── Salience-callback bus (registered-but-dormant in P1) ───────────

    def register_salience_callback(
        self, cb: Callable, threshold: float = 0.8
    ) -> None:
        """Register `cb(event)` to fire from append() when salience >= threshold.

        DORMANT in Phase 1 (no callbacks registered yet). In Phase 2 the
        autonomous-wake registers here; the callback must offload expensive
        work (LLM invoke) to a thread so the appender never blocks.
        """
        with self._lock:
            self._salience_callbacks.append((cb, threshold))

    # ── Lifecycle ──────────────────────────────────────────────────────

    def close(self) -> None:
        """Flush and close the append handle. Call at shutdown. Never raises."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


# ── Module-level singleton for tools ───────────────────────────────────
#
# Mirrors session_state.register_session/get_session EXACTLY. Tools invoked by
# LLM agents (e.g. read_workspace) need the Notebook without a reference to the
# DJTretaBeing instance. Main registers it at startup; tools call
# get_notebook() to read.

_notebook_instance: Optional[Notebook] = None
_singleton_lock = threading.Lock()


def register_notebook(nb: Notebook) -> None:
    """Register the module-level Notebook singleton.

    Called exactly once by DJTretaBeing.__init__ after Notebook.replay().
    Tools that need to read the event log import get_notebook() from here.
    """
    global _notebook_instance
    with _singleton_lock:
        if _notebook_instance is not None and _notebook_instance is not nb:
            log.warning("Notebook singleton re-registered — replacing previous instance")
        _notebook_instance = nb


def get_notebook() -> Optional[Notebook]:
    """Get the registered Notebook singleton, or None if not yet registered.

    Tools should handle the None case defensively during daemon startup.
    """
    return _notebook_instance
