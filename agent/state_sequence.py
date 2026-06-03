"""E4 — State Sequencing & Set Archive.

A "State" is a full mixer snapshot (both decks: volume, EQ hi/mid/lo, filter;
crossfader; tempo/bpm) taken at a single moment in time. A StateSequence is an
ordered list of (State, bar_duration) pairs that describes how a set evolves.

Three capabilities live here:
  1. capture_state(status_dict)  — build a State from a Mixxx /api/status response
  2. apply_state(state)          — push a State back to Mixxx (re-apply every field)
  3. StateSequence.record()      — append a State when the mixer meaningfully changes
  4. StateSequence.replay()      — re-apply states in order on a simple timed loop
  5. archive_set()               — persist a finished set to a rolling JSONL file
  6. get_set_archive()           — load the N most recent archived sets
  7. replay_set()                — find a set by ID and re-apply its state sequence

Integration seam for Agent B's ArrangementIntent
-------------------------------------------------
Agent B's planner will produce ArrangementIntent objects like:
    ArrangementIntent(
        track_path=..., technique=..., energy=0.8, bars=16,
        target_state=...  # optional — let B specify the mixer goal
    )
A B-side integration shim (to be authored in the planner layer) can call
`capture_state` to snapshot the mixer before each arrangement block and pass
the resulting State as `target_state`, OR build a State from scratch given
the intent's energy/EQ targets. The StateSequence is deliberately clean and
importable for exactly this use.

Clock-locking
-------------
Full bar-quantized replay waits for E1's master clock (Agent A). For now
`replay()` uses a simple timed loop with `bar_duration_s` derived from
BPM; swap in the E1 clock tick at integration. The integration point is
clearly marked with `# TODO(E1-integration)`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .runtime_paths import runtime_dir
from .tools.helpers import _dj_post, _mixxx_get, _mixxx_failed

log = logging.getLogger("dj-treta")

# ── Tuning constants ──────────────────────────────────────────────────

# How much any single mixer dimension must change before we record a new State.
# Prevents recording a new snapshot on every tiny EQ nudge.
_VOL_THRESHOLD = 0.02       # volume/crossfader (0–1 range)
_EQ_THRESHOLD = 0.05        # EQ bands (0–4 range, relative change)
_FILTER_THRESHOLD = 0.03    # filter (0–1 range)
_BPM_THRESHOLD = 0.5        # BPM

# How long to wait (seconds) between replay steps when no BPM is known.
_REPLAY_FALLBACK_BAR_S = 2.0

# Archive: rolling JSONL file, one JSON object per finished set.
_ARCHIVE_FILENAME = "dj-treta-set-archive.jsonl"

# How many bars a State should occupy when recorded without an explicit
# bar_duration. Planner/E1 will override this at integration.
_DEFAULT_BAR_DURATION = 4


# ── State dataclass ───────────────────────────────────────────────────

@dataclass
class EQSnapshot:
    """EQ snapshot for one deck. All values in the Mixxx range 0.0–4.0 (1.0 = neutral)."""
    hi: float = 1.0
    mid: float = 1.0
    lo: float = 1.0


@dataclass
class DeckSnapshot:
    """Full snapshot of one deck's mixer controls."""
    volume: float = 1.0
    eq: EQSnapshot = field(default_factory=EQSnapshot)
    filter: float = 0.5   # 0.0 = full high-pass, 0.5 = neutral, 1.0 = full low-pass


@dataclass
class State:
    """Mixer snapshot — the atomic unit of the State Sequencer.

    Mirrors deadmau5's "States" concept: a complete mixer configuration at a
    moment in time, designed to be captured, persisted, and replayed.

    Fields
    ------
    deck1, deck2    : per-deck volume / EQ / filter
    crossfader      : 0.0 = full Deck 1, 0.5 = center, 1.0 = full Deck 2
    bpm             : master tempo at the time of capture (informational;
                      actual clock sync is E1's job)
    ts              : Unix timestamp of capture
    label           : optional human/agent annotation (e.g. "drop", "breakdown")
    """
    deck1: DeckSnapshot = field(default_factory=DeckSnapshot)
    deck2: DeckSnapshot = field(default_factory=DeckSnapshot)
    crossfader: float = 0.5
    bpm: float = 0.0
    ts: float = field(default_factory=time.time)
    label: str = ""


# ── Capture ───────────────────────────────────────────────────────────

def capture_state(status_dict: dict, label: str = "") -> State:
    """Build a State from a Mixxx /api/status payload.

    The status_dict shape is the JSON returned by Mixxx /api/status:
      {
        "deck1": {
          "volume": 0.8,
          "eq": {"hi": 1.0, "mid": 1.0, "lo": 1.0},
          "filter": 0.5,
          ...
        },
        "deck2": { ... },
        "crossfader": 0.5,
        "bpm": 128.0,
        ...
      }

    Missing keys fall back to neutral defaults so the function is safe even
    against partial payloads (Mixxx sometimes omits fields when a deck is empty).
    """
    def _deck(d: dict) -> DeckSnapshot:
        eq_raw = d.get("eq") or {}
        return DeckSnapshot(
            volume=float(d.get("volume", 1.0)),
            eq=EQSnapshot(
                hi=float(eq_raw.get("hi", 1.0)),
                mid=float(eq_raw.get("mid", 1.0)),
                lo=float(eq_raw.get("lo", 1.0)),
            ),
            filter=float(d.get("filter", 0.5)),
        )

    return State(
        deck1=_deck(status_dict.get("deck1") or {}),
        deck2=_deck(status_dict.get("deck2") or {}),
        crossfader=float(status_dict.get("crossfader", 0.5)),
        bpm=float(status_dict.get("bpm", 0.0)),
        ts=time.time(),
        label=label,
    )


# ── Apply ─────────────────────────────────────────────────────────────

def apply_state(state: State) -> dict:
    """Re-apply a State to Mixxx via the HTTP API.

    Calls /api/volume, /api/eq, /api/filter, /api/crossfade for each deck.
    BPM/tempo is NOT re-applied here — tempo changes belong to the master
    clock (E1). That field is stored for informational/archive purposes only.

    Returns a summary dict: {"applied": [...endpoints...], "errors": [...]}
    """
    applied: list[str] = []
    errors: list[str] = []

    def _post(path: str, data: dict, label: str) -> None:
        result = _dj_post(path, data)
        if result.get("error"):
            errors.append(f"{label}: {result['error']}")
        else:
            applied.append(label)

    for deck_num, deck_snap in ((1, state.deck1), (2, state.deck2)):
        _post("/api/volume",
              {"deck": deck_num, "level": deck_snap.volume},
              f"deck{deck_num}_volume")

        _post("/api/eq",
              {"deck": deck_num,
               "hi": deck_snap.eq.hi,
               "mid": deck_snap.eq.mid,
               "lo": deck_snap.eq.lo},
              f"deck{deck_num}_eq")

        _post("/api/filter",
              {"deck": deck_num, "value": deck_snap.filter},
              f"deck{deck_num}_filter")

    _post("/api/crossfade",
          {"position": state.crossfader},
          "crossfader")

    return {"applied": applied, "errors": errors}


# ── StateSequence ─────────────────────────────────────────────────────

@dataclass
class StateEntry:
    """One step in a StateSequence: a mixer snapshot + how many bars it holds."""
    state: State
    bar_duration: int = _DEFAULT_BAR_DURATION


class StateSequence:
    """Ordered list of StateEntry objects — the full arc of a DJ set.

    Usage during a live set
    -----------------------
    Call `record(status_dict)` whenever the agent changes a mixer dimension.
    The method snapshots the current Mixxx state and appends it only if the
    mixer meaningfully changed (above the threshold constants).

    Usage for replay
    ----------------
    Call `replay()` to re-apply the sequence in order, sleeping between steps.
    Full bar-lock waits for E1's clock; for now we derive bar_duration_s from
    the State's BPM. See the TODO(E1-integration) marker below.
    """

    def __init__(self):
        self._entries: list[StateEntry] = []

    # ── Recording ─────────────────────────────────────────────────────

    def record(
        self,
        status_dict: dict,
        bar_duration: int = _DEFAULT_BAR_DURATION,
        label: str = "",
        force: bool = False,
    ) -> Optional[State]:
        """Capture and append a State if the mixer has meaningfully changed.

        Args:
            status_dict: Mixxx /api/status payload dict.
            bar_duration: How many bars this state should hold during replay.
            label: Optional annotation (e.g. "drop entry", "breakdown").
            force: Always append even if below change thresholds.

        Returns:
            The new State if it was appended, None if it was skipped (no
            meaningful change).
        """
        new_state = capture_state(status_dict, label=label)

        if not force and self._entries:
            last = self._entries[-1].state
            if not _meaningful_change(last, new_state):
                return None

        self._entries.append(StateEntry(state=new_state, bar_duration=bar_duration))
        log.debug(
            "StateSequence.record: step %d captured (bpm=%.1f, label=%r)",
            len(self._entries), new_state.bpm, label,
        )
        return new_state

    def record_now(
        self,
        bar_duration: int = _DEFAULT_BAR_DURATION,
        label: str = "",
        force: bool = False,
    ) -> Optional[State]:
        """Fetch /api/status from Mixxx and record if changed.

        Convenience wrapper that calls _mixxx_get internally.
        """
        status = _mixxx_get("/api/status")
        if _mixxx_failed(status):
            log.warning("StateSequence.record_now: Mixxx unreachable — skipping")
            return None
        return self.record(status, bar_duration=bar_duration, label=label, force=force)

    # ── Replay ────────────────────────────────────────────────────────

    def replay(self, stop_event=None) -> None:
        """Re-apply the sequence to Mixxx in order.

        Sleeps `bar_duration_s` between steps, derived from the State's BPM.
        If BPM is unknown, falls back to `_REPLAY_FALLBACK_BAR_S` seconds/step.

        Args:
            stop_event: optional threading.Event; if set(), replay exits early.
                        Pass a stop event from the heartbeat so this doesn't
                        block the live set when called from a background thread.

        TODO(E1-integration): replace the sleep with a bar-quantized yield
        against Agent A's master clock tick. The integration point:
            bar_tick = clock.wait_next_bar()   # E1 yields here
            apply_state(entry.state)
        """
        if not self._entries:
            log.info("StateSequence.replay: no states recorded — nothing to replay")
            return

        log.info("StateSequence.replay: starting replay of %d states", len(self._entries))

        for i, entry in enumerate(self._entries):
            if stop_event is not None and stop_event.is_set():
                log.info("StateSequence.replay: stop requested at step %d", i)
                break

            result = apply_state(entry.state)
            if result["errors"]:
                log.warning(
                    "StateSequence.replay: step %d errors: %s",
                    i, result["errors"],
                )
            else:
                log.debug(
                    "StateSequence.replay: step %d applied (%d endpoints)",
                    i, len(result["applied"]),
                )

            # Derive sleep from BPM. A bar = 4 beats.
            # TODO(E1-integration): swap this sleep for a clock.wait_next_bar() call.
            bpm = entry.state.bpm
            if bpm > 0:
                seconds_per_beat = 60.0 / bpm
                bar_duration_s = seconds_per_beat * 4 * entry.bar_duration
            else:
                bar_duration_s = _REPLAY_FALLBACK_BAR_S * entry.bar_duration

            if stop_event is not None:
                stop_event.wait(bar_duration_s)
            else:
                time.sleep(bar_duration_s)

        log.info("StateSequence.replay: complete")

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> list[dict]:
        """Serialize to a JSON-friendly list."""
        result = []
        for entry in self._entries:
            result.append({
                "bar_duration": entry.bar_duration,
                "state": {
                    "deck1": {
                        "volume": entry.state.deck1.volume,
                        "eq": asdict(entry.state.deck1.eq),
                        "filter": entry.state.deck1.filter,
                    },
                    "deck2": {
                        "volume": entry.state.deck2.volume,
                        "eq": asdict(entry.state.deck2.eq),
                        "filter": entry.state.deck2.filter,
                    },
                    "crossfader": entry.state.crossfader,
                    "bpm": entry.state.bpm,
                    "ts": entry.state.ts,
                    "label": entry.state.label,
                },
            })
        return result

    @classmethod
    def from_dict(cls, data: list[dict]) -> "StateSequence":
        """Deserialize from the format produced by to_dict()."""
        seq = cls()
        for item in data:
            s = item["state"]
            d1 = s.get("deck1") or {}
            d2 = s.get("deck2") or {}
            state = State(
                deck1=DeckSnapshot(
                    volume=float(d1.get("volume", 1.0)),
                    eq=EQSnapshot(**{k: float(v) for k, v in (d1.get("eq") or {}).items()
                                    if k in ("hi", "mid", "lo")}),
                    filter=float(d1.get("filter", 0.5)),
                ),
                deck2=DeckSnapshot(
                    volume=float(d2.get("volume", 1.0)),
                    eq=EQSnapshot(**{k: float(v) for k, v in (d2.get("eq") or {}).items()
                                    if k in ("hi", "mid", "lo")}),
                    filter=float(d2.get("filter", 0.5)),
                ),
                crossfader=float(s.get("crossfader", 0.5)),
                bpm=float(s.get("bpm", 0.0)),
                ts=float(s.get("ts", 0.0)),
                label=s.get("label", ""),
            )
            seq._entries.append(StateEntry(
                state=state,
                bar_duration=int(item.get("bar_duration", _DEFAULT_BAR_DURATION)),
            ))
        return seq

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


# ── Change detection ─────────────────────────────────────────────────

def _meaningful_change(prev: State, curr: State) -> bool:
    """Return True if curr differs from prev by more than any threshold.

    Used by StateSequence.record to skip trivial/noise-level changes.
    """
    def _eq_changed(a: EQSnapshot, b: EQSnapshot) -> bool:
        return (
            abs(a.hi - b.hi) > _EQ_THRESHOLD
            or abs(a.mid - b.mid) > _EQ_THRESHOLD
            or abs(a.lo - b.lo) > _EQ_THRESHOLD
        )

    if abs(prev.deck1.volume - curr.deck1.volume) > _VOL_THRESHOLD:
        return True
    if abs(prev.deck2.volume - curr.deck2.volume) > _VOL_THRESHOLD:
        return True
    if _eq_changed(prev.deck1.eq, curr.deck1.eq):
        return True
    if _eq_changed(prev.deck2.eq, curr.deck2.eq):
        return True
    if abs(prev.deck1.filter - curr.deck1.filter) > _FILTER_THRESHOLD:
        return True
    if abs(prev.deck2.filter - curr.deck2.filter) > _FILTER_THRESHOLD:
        return True
    if abs(prev.crossfader - curr.crossfader) > _VOL_THRESHOLD:
        return True
    if prev.bpm > 0 and curr.bpm > 0:
        if abs(prev.bpm - curr.bpm) > _BPM_THRESHOLD:
            return True
    return False


# ── Set archive ───────────────────────────────────────────────────────

def _archive_path() -> Path:
    """Resolve the set-archive JSONL file path."""
    return runtime_dir() / _ARCHIVE_FILENAME


def archive_set(
    set_id: str,
    started_at: float,
    ended_at: float,
    mood: str,
    state_sequence: StateSequence,
    tracks_played: list,
    recording_path: str = "",
) -> Path:
    """Persist a finished set to the rolling set-archive JSONL.

    Each line is one JSON object:
    {
      "set_id": "set-20260525-201300",
      "started_at": 1748189580.0,
      "ended_at":   1748192400.0,
      "mood": "melodic-techno",
      "state_sequence": [...],      # StateSequence.to_dict()
      "tracks_played": [...],       # session.tracks_played snapshot
      "recording_path": "/path/to/recording.wav",
      "archived_at": 1748192401.0
    }

    The audio itself is captured by the live stream recorder; we only store
    the path/reference here — no re-encoding.

    Returns the archive file path.
    """
    record = {
        "set_id": set_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "mood": mood,
        "state_sequence": state_sequence.to_dict() if state_sequence else [],
        "tracks_played": list(tracks_played),
        "recording_path": recording_path,
        "archived_at": time.time(),
    }
    path = _archive_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        log.info(
            "archive_set: wrote set %r (%d states, %d tracks) to %s",
            set_id, len(state_sequence), len(tracks_played), path,
        )
    except Exception as exc:
        log.warning("archive_set: write failed: %s", exc)
    return path


def get_set_archive(n: int = 10) -> list[dict]:
    """Load the N most recent archived sets from the JSONL file.

    Returns a list of dicts (newest first). `state_sequence` is included
    in raw dict form — call StateSequence.from_dict(entry["state_sequence"])
    to get a live object.

    Args:
        n: How many sets to return. 0 = return all.
    """
    path = _archive_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        sets = [json.loads(line) for line in lines if line.strip()]
        # Newest first
        sets.reverse()
        return sets[:n] if n > 0 else sets
    except Exception as exc:
        log.warning("get_set_archive: read failed: %s", exc)
        return []


def replay_set(set_id: str, stop_event=None) -> dict:
    """Find a set by ID in the archive and replay its state sequence.

    Loads the archived StateSequence and calls StateSequence.replay().
    Returns a summary dict.

    Args:
        set_id: The set_id string (e.g. "set-20260525-201300").
        stop_event: optional threading.Event for early exit.
    """
    path = _archive_path()
    if not path.exists():
        return {"error": "No archive file found", "set_id": set_id}

    target: Optional[dict] = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("set_id") == set_id:
                target = obj
    except Exception as exc:
        return {"error": f"Archive read failed: {exc}", "set_id": set_id}

    if target is None:
        return {"error": f"Set {set_id!r} not found in archive", "set_id": set_id}

    seq_data = target.get("state_sequence") or []
    if not seq_data:
        return {"error": f"Set {set_id!r} has no recorded states", "set_id": set_id}

    seq = StateSequence.from_dict(seq_data)
    log.info("replay_set: replaying %r (%d states)", set_id, len(seq))
    seq.replay(stop_event=stop_event)

    return {
        "set_id": set_id,
        "states_replayed": len(seq),
        "mood": target.get("mood", ""),
        "tracks_in_set": len(target.get("tracks_played") or []),
        "recording_path": target.get("recording_path", ""),
    }
