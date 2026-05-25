"""Heartbeat mixin — monitors deck state and triggers transitions.
# v7.0 Evolution Protocol — self-improving DJ

Priority order:
1. Silence → emergency recovery
2. Track ending + idle ready → auto-transition
3. Scheduled transition file → execute it
4. Agent decides (past 50% played)
5. Backup load (idle empty, past threshold)
"""

import json
import logging
import threading
import time
from pathlib import Path

import httpx
from .runtime_paths import runtime_path

log = logging.getLogger("dj-treta")


def _idle_was_played(idle_path: str, tracks_played: list) -> bool:
    """True iff `idle_path` matches any entry in `tracks_played`.

    Match handles the relative-vs-absolute path mismatch between the two
    sources we draw on at runtime:
      - `idle_path` comes from `get_deck_paths(...)` which normalizes to
        a path *relative* to `library.music_path` (e.g.
        `melodic-techno/Eric Luttrell - LOVE.mp3`).
      - `tracks_played[].path` is the raw `tinfo.file_path` Mixxx reports,
        which is an *absolute* path (e.g.
        `/Users/.../melodic-techno/Eric Luttrell - LOVE.mp3`).

    Direct equality silently misses every replay. We match on:
      1. exact equality (cheap, covers same-shape entries)
      2. absolute-ends-with-relative (`abs.endswith("/" + rel)`)
      3. basename equality (fallback — different libraries, same filename)

    Empty `idle_path` always returns False.
    """
    if not idle_path:
        return False
    idle_basename = idle_path.rsplit("/", 1)[-1]
    for t in (tracks_played or []):
        p = (t.get("path") or t.get("file_path") or "") if isinstance(t, dict) else ""
        if not p:
            continue
        if p == idle_path:
            return True
        if p.endswith("/" + idle_path):
            return True
        if p.rsplit("/", 1)[-1] == idle_basename:
            return True
    return False


def _filter_playlist_for_decks(
    playlist: dict | None,
    active_path: str,
    idle_path: str,
) -> dict | None:
    """Return a playlist copy with tracks whose path matches either
    deck's currently-loaded file removed. No-op when playlist is empty or
    no tracks match the exclusions.

    Protects against the stale-playlist case: planner's rank-1 is still
    the track DJ just loaded (because planner hasn't replanned yet). DJ
    must not see that candidate as loadable.
    """
    if not playlist or not playlist.get("tracks"):
        return playlist
    excluded = {p for p in (active_path, idle_path) if p}
    if not excluded:
        return playlist
    filtered = [t for t in playlist["tracks"] if t.get("path") not in excluded]
    if len(filtered) == len(playlist["tracks"]):
        return playlist  # nothing to filter — return original
    new_playlist = dict(playlist)
    new_playlist["tracks"] = filtered
    return new_playlist


class HeartbeatMixin:

    def _deck_owned_by_external(self, deck_num: int) -> bool:
        """True if a non-treta being has claimed this deck via MCP.

        Phase 7 co-being gate. Used to skip auto-load / auto-transition for
        decks that an external Being (Himani, Serra, Manish) has reserved.
        """
        try:
            owner = (self.session.deck_ownership or {}).get(int(deck_num))
        except Exception:
            owner = None
        return owner is not None and owner != "treta"

    def _sync_deck_ownership(self):
        """Sync co-being ownership from /tmp/dj-treta-deck-ownership.json.

        The MCP server (dj_take_deck / dj_release_deck tools) writes to this
        file. Daemon syncs it into session.deck_ownership on every heartbeat
        tick so the in-process guards see fresh state.

        File format: {"1": {"being_id": "himani", "taken_at": 12345.6}, ...}
        In-memory shape: {1: "himani", 2: "treta"} — just deck→owner_id.
        """
        import os
        ownership_file = str(runtime_path("deck-ownership.json"))
        if not os.path.exists(ownership_file):
            if self.session.deck_ownership:
                # File removed externally — clear in-memory ownership.
                self.session.deck_ownership = {}
            return
        try:
            with open(ownership_file) as f:
                raw = json.load(f) or {}
            # Normalize: keys → int, values → being_id string. Accept both
            # the record shape ({"being_id": "x", "taken_at": ...}) and a
            # raw string value for flexibility.
            normalized = {}
            for k, v in raw.items():
                if not str(k).isdigit():
                    continue
                if isinstance(v, dict):
                    bid = v.get("being_id")
                elif isinstance(v, str):
                    bid = v
                else:
                    bid = None
                if bid:
                    normalized[int(k)] = bid
            if normalized != self.session.deck_ownership:
                self.session.deck_ownership = normalized
        except Exception as exc:
            log.warning(f"deck_ownership sync failed: {exc}")

    def _detect_manual_transition(self, status, active_deck, idle_deck):
        """Sarathi: infer when Manish is driving a transition on the FLX4.

        The FLX4 writes the same Mixxx ControlObjects Treta reads via the API,
        so his physical crossfader moves show up here for free. We watch two
        signals, tick-over-tick:
          1. Crossfader moved a lot (Δ>0.15) while no Treta transition is in
             flight → he's mid-move. Set manish_in_motion (gates P4 so she
             doesn't talk over him) for a 90s window.
          2. Active deck role flipped while he was in motion → he completed it
             himself. Mark any pending suggestion 'manish_executed_manually'
             (revealed preference) and log it for learning.
        Only meaningful in sarathi_mode; no-op otherwise.
        """
        if not getattr(self.session, "sarathi_mode", False):
            return
        try:
            xfader = float(status.get("crossfader", 0) or 0)
            # Channel (volume) faders — Manish mixes with these, crossfader
            # parked center. Watch them too or his transitions are invisible.
            vol1 = float((status.get("deck1") or {}).get("volume", 1) or 0)
            vol2 = float((status.get("deck2") or {}).get("volume", 1) or 0)
            prev_xfader = getattr(self, "_last_xfader", None)
            prev_vol1 = getattr(self, "_last_vol1", None)
            prev_vol2 = getattr(self, "_last_vol2", None)
            prev_active = getattr(self, "_last_active_deck", None)
            self._last_xfader = xfader
            self._last_vol1 = vol1
            self._last_vol2 = vol2
            self._last_active_deck = active_deck
            now = time.time()

            # His transition just executed (we executed nothing) — attribute it.
            if (prev_active is not None and prev_active != active_deck
                    and getattr(self.session, "manish_in_motion", False)
                    and not self._transition_pending):
                log.info(f"[sarathi] Manish completed a manual transition (deck {prev_active}→{active_deck})")
                self._record_playing_tracks()
                for d in list(self.session.directives):
                    if d.get("status") == "active" and d.get("kind") == "transition_suggestion":
                        p = d.get("payload") or {}
                        self.session.mark_satisfied(d.get("id"))
                        try:
                            from .memory import store_thought
                            store_thought(
                                ts=now, agent_id="treta:learning",
                                decision_text=(
                                    f"Manish took the transition himself (deck {prev_active}→{active_deck}); "
                                    f"I had suggested {p.get('technique')} → deck {p.get('to_deck')}."
                                ),
                                context={"decision": "manish_executed_manually", "payload": p},
                            )
                        except Exception:
                            pass
                if self.current_set and isinstance(self.current_set.get("energy_arc"), list):
                    self.current_set["energy_arc"].append({
                        "t": round(now - self.current_set["started_at"]),
                        "event": "transition", "technique": "manual_flx4", "to_deck": active_deck,
                    })
                self.session.manish_in_motion = False

            # He's working a transition if EITHER the crossfader moved a lot OR
            # a channel fader moved a lot — and nothing of ours is in flight
            # (our own channel-fader blends move the volumes too, but those run
            # under _transition_pending, so they're excluded here).
            xfader_moved = prev_xfader is not None and abs(xfader - prev_xfader) > 0.15
            vol_moved = (
                (prev_vol1 is not None and abs(vol1 - prev_vol1) > 0.10)
                or (prev_vol2 is not None and abs(vol2 - prev_vol2) > 0.10)
            )
            if (xfader_moved or vol_moved) and not self._transition_pending:
                self.session.manish_in_motion = True
                self.session.manish_motion_until = now + 90
                _what = (f"xfader {prev_xfader:.2f}→{xfader:.2f}" if xfader_moved
                         else f"vol {prev_vol1:.2f}/{prev_vol2:.2f}→{vol1:.2f}/{vol2:.2f}")
                log.info(f"[sarathi] manual transition in progress ({_what}) — going quiet 90s")
        except Exception as exc:
            log.debug(f"[sarathi] manual-transition detect failed (non-fatal): {exc}")

    def _heartbeat(self):
        """Pure Python heartbeat. Reads mix_out from DB. No flags, no timers."""
        from .main import _get_status, _active_idle_decks, _ensure_mixxx

        # Sync co-being deck ownership (Phase 7) before any decisions.
        self._sync_deck_ownership()

        # Retire stale shape directives so prompt rendering doesn't show
        # an expired DIRECTIVE FROM TRETA line. Surgical (load_track,
        # transition_now) directives don't auto-expire — they retire via
        # satisfaction or supersession. See plan: atomic-cuddling-manatee.
        try:
            self.session.expire_stale()
        except Exception as exc:
            log.debug(f"directive expire_stale failed: {exc}")

        # ── Self-schedule fire ─────────────────────────────────────
        # Treta can wake herself via schedule_self(). Fire any entries
        # whose at_ts has passed; inject callback_directive as a shape
        # directive she'll see on the next chat tick. See evolution
        # plan Tier 2.1.
        try:
            now = time.time()
            fired_any = False
            for entry in self.session.self_schedule:
                if not entry.get("fired") and now >= entry.get("at_ts", 0):
                    entry["fired"] = True
                    entry["fired_at"] = now
                    fired_any = True
                    reason = entry.get("reason", "")
                    callback = entry.get("callback_directive", "")
                    if callback:
                        self.session.add_directive(
                            kind="shape",
                            target="dj",
                            payload={"text": f"[self-schedule] {reason}: {callback}"},
                            ttl_seconds=90.0,
                            supersede_kinds=[],   # don't supersede other shape; this is its own thread
                        )
                    log.info(f"[self-schedule] fired: {reason[:80]}")
            # Force a flush after firing so the fired=True state persists.
            if fired_any:
                self.session.flush()
        except Exception as exc:
            log.debug(f"self_schedule check failed: {exc}")

        # ── Meta-control: dj_paused ────────────────────────────────
        # Treta can pause the DJ to take direct control. We still run
        # the silence-recovery safety net below; pause only skips the
        # creative DJ-invoke (P4). See evolution plan Tier 2.3.
        # Note: silence recovery (P1) and scheduled-transition exec (P3)
        # remain active — pausing DJ shouldn't kill the music if Treta
        # forgets to do anything.

        status = _get_status(self.config.mixxx.url)
        if not status:
            _ensure_mixxx(self.config)
            return

        active_deck, idle_deck = _active_idle_decks(status)

        # Sarathi: detect Manish working a transition on the FLX4 (crossfader
        # / deck-role deltas read straight from the ControlObjects he moves).
        self._detect_manual_transition(status, active_deck, idle_deck)

        d_active = status.get(f"deck{active_deck}", {})
        d_idle = status.get(f"deck{idle_deck}", {})
        position = float(d_active.get("position_seconds", 0) or 0)
        remaining = float(d_active.get("remaining_seconds", 0) or 0)
        playing = d_active.get("playing", False)
        idle_loaded = d_idle.get("track_loaded", False)
        idle_remaining = float(d_idle.get("remaining_seconds", 0) or 0)

        nothing_playing = (not status.get("deck1", {}).get("playing")
                           and not status.get("deck2", {}).get("playing"))

        # Sarathi Mode flag — Manish drives; the deterministic systems below
        # back off so they don't fight him. Auto mode is unaffected.
        _sarathi = bool(getattr(self.session, "sarathi_mode", False))

        # === PRIORITY 1: SILENCE — emergency recovery (ALWAYS runs, even if agent busy) ===
        if nothing_playing:
            self._next_sleep = 5
            now_ts = time.time()
            if not getattr(self, "_silence_since", None):
                # FIRST silent read — do NOT emergency yet. A single transient
                # status read showing both decks not-playing (Mixxx hiccup at a
                # track boundary or under load) would otherwise dump an
                # emergency track over a deck that's actually playing. Record
                # the time, re-check in 2s, and only act if it's still silent.
                self._silence_since = now_ts
                self._next_sleep = 2
                return
            # Sarathi: Manish drives. Two cases:
            #  - COLD START (nothing has ever played this session) → never
            #    auto-play; he starts the set himself. Just wait.
            #  - MID-SET dropout (music was playing, then silence) → give an
            #    8s grace + hold while he's mid-move, then rescue (a track
            #    ended and he got distracted — music shouldn't stay dead).
            # Auto mode recovers instantly in all cases (it owns the set).
            if _sarathi:
                if not getattr(self, "_music_has_played", False):
                    return  # cold start — wait for Manish to play deck 1
                in_motion = (getattr(self.session, "manish_in_motion", False)
                             and now_ts < getattr(self.session, "manish_motion_until", 0.0))
                if in_motion or (now_ts - self._silence_since) < 8.0:
                    return  # hold — he may be cueing / working the deck
            # Emergency runs regardless of _agent_busy — music must never stop
            if not getattr(self, '_emergency_running', False):
                self._emergency_running = True
                threading.Thread(target=self._emergency_play, daemon=True).start()
            return
        # Playing → reset the silence timer + remember music has played (so
        # Sarathi's mid-set rescue can tell a real dropout from a cold start).
        self._silence_since = None
        self._music_has_played = True

        idle_ready = idle_loaded and idle_remaining > 60
        duration = float(d_active.get("duration", 0) or 0)

        # === PRIORITY 2: Auto-transition safety net (track ending) ===
        # Fires when active is within 30s of ending and idle is ready. This
        # is the pre-A2 safety net — Python-forced 15s crossfade when DJ
        # didn't schedule anything in time. Signals (user_skip, set_ending,
        # idle_needs_load) are now executed directly in P3.5 below, NOT via
        # this watchdog.
        # Phase 7: if either deck is claimed by an external Being, skip —
        # co-being owns that deck and DJ must not auto-transition into it.
        if (self._deck_owned_by_external(idle_deck)
                or self._deck_owned_by_external(active_deck)):
            owner_i = self.session.deck_ownership.get(int(idle_deck))
            owner_a = self.session.deck_ownership.get(int(active_deck))
            log.debug(
                f"P2 auto-transition skipped — deck {active_deck} owner={owner_a}, "
                f"deck {idle_deck} owner={owner_i}"
            )
        # Sarathi Mode: do NOT auto-transition — Manish owns every transition
        # on the FLX4. If he misses one entirely and the track ends, P1 silence
        # recovery (with its grace window) is the only safety net. Auto mode
        # keeps the <30s watchdog crossfade.
        elif (not _sarathi
                and idle_ready
                and remaining < 30
                and remaining > 0 and playing
                and not self._agent_busy and not self._transition_pending):
            log.info(f"Auto-transition (track ending): {remaining:.0f}s left, crossfading to deck {idle_deck}")
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("log", {"text": f"Auto-transition (track ending): {remaining:.0f}s left, crossfading to deck {idle_deck}"})
            from .tools import do_transition
            self._transition_pending = True
            def _auto():
                try:
                    result = do_transition(idle_deck, 15)
                    log.info(f"Auto-transition result: {str(result)[:200]}")
                    if self.current_set and isinstance(self.current_set.get("energy_arc"), list):
                        self.current_set["energy_arc"].append({
                            "t": round(time.time() - self.current_set["started_at"]),
                            "event": "transition", "technique": "auto_crossfade", "to_deck": idle_deck,
                        })
                    self._record_playing_tracks()
                    # Flag idle for a fresh load on next tick (P3.5 will execute).
                    self.session.idle_needs_load = True
                except Exception as e:
                    log.error(f"Auto-transition error: {e}")
                finally:
                    self._transition_pending = False
            threading.Thread(target=_auto, daemon=True).start()
            self._next_sleep = 5
            return

        # === PRIORITY 3: Execute scheduled transition (Python handles timing) ===
        # Phase 7: skip if the scheduled transition targets an externally-owned
        # deck. Treta shouldn't push music onto a co-being's deck.
        if not self._transition_pending:
            sched_file = runtime_path("scheduled-transition.json")
            if sched_file.exists():
                try:
                    sched = json.loads(sched_file.read_text())
                    sched_to_deck = sched.get("toDeck") or sched.get("to_deck") or idle_deck
                    if self._deck_owned_by_external(sched_to_deck):
                        owner = self.session.deck_ownership.get(int(sched_to_deck))
                        log.debug(
                            f"P3 scheduled transition skipped — deck "
                            f"{sched_to_deck} owned by {owner}"
                        )
                        sched_file.unlink(missing_ok=True)
                    else:
                        sched_file.unlink(missing_ok=True)  # delete BEFORE starting executor (#67)
                        self._transition_pending = True
                        threading.Thread(
                            target=self._execute_scheduled_transition,
                            args=(sched,), daemon=True
                        ).start()
                except Exception as e:
                    log.warning(f"Bad scheduled transition file: {e}")
                    sched_file.unlink(missing_ok=True)

        # === PRIORITY 3.5: Direct Python signal executors ===
        # Phase A2 moved user_skip / set_ending / idle_needs_load routing
        # into the DJ prompt as a Signals block — that accumulated enough
        # conditional branches that Flash started dropping ~46% of signal-
        # driven invocations. DJ's charm comes back when its prompt only
        # asks for creative decisions (which technique at which section);
        # mechanical routing (skip → crossfade, idle empty → load rank-1,
        # set ending → echo out) belongs in Python.
        self._execute_signals(status, active_deck, idle_deck, position,
                              remaining, idle_loaded, idle_remaining)

        # === PRIORITY 4: Agent decides transition (Software 3.0) ===
        # Fires when active has < 120s remaining and idle is ready. Issue
        # #76 moved this earlier than "past-half" so Flash latency + any
        # defer_decision retries still leave room before track end.
        # DJ picks technique (crossfade / bass_swap / echo_out / hard_cut)
        # based on BPM/key/energy gap between active and next-loaded.
        # Phase 7: if the idle deck is externally owned, DJ has nothing to
        # schedule into — skip the agent call entirely. Active-only DJing
        # (just let the current track finish) is the right behaviour.
        sched_file_exists = runtime_path("scheduled-transition.json").exists()
        # v9 Tier-2: invoke DJ as soon as both decks are ready, not just in
        # the last 120s. Real DJs decide transitions ahead of time — by the
        # time you're 30s out, you've already pre-cued, beat-matched, and
        # pre-aligned phrase boundaries. The earlier we surface mix_out as
        # the IDEAL transition point, the more time DJ has to defer/decide
        # consciously instead of being forced into a watchdog rescue.
        # `dj_deferred_until` (added in #76) still throttles repeat asks,
        # so the cost is bounded — DJ controls cadence via defer_decision.
        transition_window = idle_ready and remaining > 0
        # Issue #76: respect defer_decision — DJ told us to ask later.
        deferred_until = getattr(self.session, "dj_deferred_until", 0.0) or 0.0
        # Sarathi: don't re-invoke the DJ agent for a new suggestion while one
        # is already live — otherwise she re-suggests every tick the window's
        # open (the suggestion spam). One suggestion per window.
        _live_sugg = False
        if _sarathi:
            try:
                from .tools.sarathi import list_pending_suggestions
                _live_sugg = bool(list_pending_suggestions())
            except Exception:
                _live_sugg = False
        if getattr(self.session, "dj_paused", False):
            log.debug("P4 DJ invoke skipped — dj_paused (Treta has the wheel)")
        elif _sarathi and _live_sugg:
            log.debug("P4 DJ invoke skipped — live suggestion already pending (sarathi, no re-suggest)")
        elif (getattr(self.session, "manish_in_motion", False)
                and time.time() < getattr(self.session, "manish_motion_until", 0.0)):
            log.debug("P4 DJ invoke skipped — manish_in_motion (he's working a transition on the FLX4)")
        elif time.time() < deferred_until:
            log.debug(
                f"P4 DJ invoke skipped — deferred until {deferred_until:.0f} "
                f"({deferred_until - time.time():.0f}s left)"
            )
        elif self._deck_owned_by_external(idle_deck):
            owner = self.session.deck_ownership.get(int(idle_deck))
            log.debug(
                f"P4 DJ invoke skipped — idle deck {idle_deck} owned by {owner}"
            )
        elif (transition_window
                and not self._agent_busy and not self._transition_pending
                and not sched_file_exists):
            from .db import get_track_by_path

            # Get metadata for both tracks
            active_meta = None
            idle_meta = None
            active_file = ""
            idle_file = ""
            try:
                tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{active_deck}/track_info", timeout=2).json()
                active_file = tinfo.get("file_path", "")
                active_meta = get_track_by_path(active_file) if active_file else None
            except Exception:
                pass
            try:
                tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{idle_deck}/track_info", timeout=2).json()
                idle_file = tinfo.get("file_path", "")
                idle_meta = get_track_by_path(idle_file) if idle_file else None
            except Exception:
                pass

            # Get track names
            active_track = active_meta.get("title", "") if active_meta else ""
            idle_track = idle_meta.get("title", "") if idle_meta else ""

            # Build context with timelines
            active_section = self._get_current_section(active_meta, position)
            active_timeline = self._format_timeline(active_meta)
            idle_timeline = self._format_timeline(idle_meta)
            active_bpm = d_active.get("bpm", 0)
            active_file_bpm = d_active.get("file_bpm", 0) or active_bpm
            idle_bpm = d_idle.get("bpm", 0)
            idle_file_bpm = d_idle.get("file_bpm", 0) or idle_bpm
            active_key = active_meta.get("key_musical", "?") if active_meta else "?"
            idle_key = idle_meta.get("key_musical", "?") if idle_meta else "?"

            # v9 Tier-2: pre-computed transition points + Camelot key + energy.
            # These come straight from the analyzer-populated tracks table —
            # surfacing them here means DJ doesn't have to invent at_position
            # values from raw timeline strings.
            def _meta_get(meta, key, default=None):
                if not meta:
                    return default
                v = meta.get(key)
                return v if v is not None else default

            active_camelot = _meta_get(active_meta, "key_camelot", "")
            idle_camelot = _meta_get(idle_meta, "key_camelot", "")
            active_energy = _meta_get(active_meta, "energy_peak")
            idle_energy = _meta_get(idle_meta, "energy_peak")
            active_mix_in = _meta_get(active_meta, "mix_in_seconds")
            active_mix_out = _meta_get(active_meta, "mix_out_seconds")
            idle_mix_in = _meta_get(idle_meta, "mix_in_seconds")
            idle_mix_out = _meta_get(idle_meta, "mix_out_seconds")
            idle_duration = _meta_get(idle_meta, "duration_seconds")

            from .prompts import build_dj_user_message

            # v8.2 — compute whether idle deck holds an already-played track.
            # Surfacing this explicitly to the DJ prompt fixes the observed
            # bug (2026-05-03 set) where DJ scheduled crossfades into played
            # tracks because the info was buried in implicit playlist
            # filtering. Compare by basename to bridge relative/absolute
            # path forms (same approach as the [SIGNAL] check below).
            _idle_already_played = False
            try:
                _idle_basename = (idle_file or "").rsplit("/", 1)[-1]
                if _idle_basename:
                    _played_paths_check = {
                        (t.get("path") or t.get("file_path") or "")
                        for t in (self.tracks_played or [])
                    }
                    _played_paths_check.discard("")
                    _idle_already_played = any(
                        p == idle_file
                        or (idle_file and p.endswith("/" + idle_file))
                        or p.rsplit("/", 1)[-1] == _idle_basename
                        for p in _played_paths_check
                    )
            except Exception:
                _idle_already_played = False

            # BUG-2 fix: filter the playlist so it never shows tracks
            # already on either deck. Otherwise, on a small library with a
            # stale playlist, DJ picks rank-1 which is still the
            # now-active track (or the track it just loaded on idle).
            # Planner's replan hasn't caught up yet; DJ must defend against
            # loading duplicates. This mirrors pick_next_candidate's
            # exclude_paths semantics from the Python fallback path.
            filtered_playlist = _filter_playlist_for_decks(
                getattr(self.session, "playlist", None),
                active_file,
                idle_file,
            )

            # Phase 7: list decks claimed by external Beings so DJ doesn't try
            # to schedule transitions onto them.
            external_decks = [
                int(d) for d, o in (self.session.deck_ownership or {}).items()
                if o != "treta"
            ]

            # Pinned-idle directive — when Treta has issued a play_specific_track
            # or replace_deck(path=…), surface the named title/path so the DJ
            # prompt renders an unambiguous "IDLE DECK PINNED TO" block. Belt
            # to the planner's suspenders; the planner usually loads the pinned
            # track first, but if it hasn't yet (race, deck contention) the DJ
            # gets an explicit instruction to load_track before transitioning.
            # See plan: atomic-cuddling-manatee.
            pinned_idle_title = ""
            pinned_idle_path = ""
            pinned_idle_loaded = False
            transition_now_pending = False
            try:
                d_load = self.session.find_active_directive(
                    "load_track", target="planner", deck=idle_deck,
                )
                if d_load:
                    p = d_load.get("payload") or {}
                    pinned_idle_path = p.get("path", "") or ""
                    pinned_idle_title = p.get("title", "") or ""
                    if pinned_idle_path and idle_file:
                        pinned_idle_loaded = (
                            pinned_idle_path == idle_file
                            or pinned_idle_path.endswith("/" + idle_file)
                            or idle_file.endswith(
                                "/" + pinned_idle_path.rsplit("/", 1)[-1]
                            )
                        )
                d_trans = self.session.find_active_directive(
                    "transition_now", target="dj", deck=idle_deck,
                )
                if d_trans:
                    # Only surface as pending when the bound load is satisfied
                    # (or there's no bound load). Otherwise we'd ask DJ to
                    # transition into a track that isn't loaded yet.
                    bound_id = (d_trans.get("payload") or {}).get("bound_to")
                    if bound_id:
                        # Check the bound load_track directive's status.
                        bound = next(
                            (d for d in self.session.directives
                             if d.get("id") == bound_id),
                            None,
                        )
                        if bound and bound.get("status") == "satisfied":
                            transition_now_pending = True
                    else:
                        transition_now_pending = True
                # One-and-done: once surfaced (bound load is loaded on the
                # idle deck), mark the directive satisfied. The DJ gets the
                # prompt rule this cycle; subsequent cycles fall back to
                # normal transition logic. This avoids re-asserting the
                # directive on every heartbeat after it's done its job.
                if transition_now_pending and d_trans:
                    self.session.mark_satisfied(d_trans.get("id"))
            except Exception as exc:
                log.debug(f"pinned-idle resolve failed: {exc}")

            instruction = build_dj_user_message(
                active_track=active_track,
                position=position,
                duration=duration,
                remaining=remaining,
                active_bpm=active_bpm,
                active_file_bpm=active_file_bpm,
                active_key=active_key,
                active_section=active_section,
                active_timeline=active_timeline,
                idle_track=idle_track,
                idle_deck=idle_deck,
                idle_bpm=idle_bpm,
                idle_file_bpm=idle_file_bpm,
                idle_key=idle_key,
                idle_timeline=idle_timeline,
                transition_pending=self._transition_pending,
                dj_directive=self.dj_directive,
                playlist=filtered_playlist,
                mood_profile=getattr(self.session, "mood_profile", None),
                external_decks=external_decks,
                active_camelot=active_camelot,
                active_energy=active_energy,
                active_mix_in=active_mix_in,
                active_mix_out=active_mix_out,
                idle_duration=idle_duration,
                idle_camelot=idle_camelot,
                idle_energy=idle_energy,
                idle_mix_in=idle_mix_in,
                idle_mix_out=idle_mix_out,
                idle_already_played=_idle_already_played,
                pinned_idle_title=pinned_idle_title,
                pinned_idle_path=pinned_idle_path,
                pinned_idle_loaded=pinned_idle_loaded,
                transition_now_pending=transition_now_pending,
            )

            # Sarathi Mode: prepend a MODE line so the DJ agent suggests
            # instead of schedules. The system prompt branches on this.
            if getattr(self.session, "sarathi_mode", False):
                instruction = (
                    "MODE: SARATHI — the user drives transitions on the "
                    "controller. Call suggest_transition (NOT "
                    "schedule_transition). You propose the moment + technique "
                    "with a plain-language reason; the user executes, or says "
                    "'do it' and it fires.\n\n"
                ) + instruction
            else:
                instruction = (
                    "MODE: AUTONOMOUS — you execute transitions via "
                    "schedule_transition.\n\n"
                ) + instruction

            self._agent_busy = True

            def _run():
                try:
                    result = self._invoke_agent(instruction, fresh_session=True)
                    # Suppress blank log lines on Flash empty response
                    # (~60% drop rate on niche prompts). The safety net at
                    # P2 catches the miss; the empty line only pollutes TUI.
                    if (result or "").strip():
                        log.info(f"DJ decision: {result[:500]}")
                        if hasattr(self, '_ws_broadcast'):
                            self._ws_broadcast("log", {"text": f"DJ decision: {result[:200]}"})
                    # Shape directives no longer cleared here — they have
                    # a TTL and auto-expire via session.expire_stale().
                    # The transition_now directive (if present) will be
                    # marked satisfied when schedule_transition fires
                    # (handled in the transition tool, not here).
                    # See plan: atomic-cuddling-manatee.
                    self._record_playing_tracks()
                    self._check_set_duration()
                except Exception as e:
                    import traceback
                    log.error(f"DJ decision error: {type(e).__name__}: {e}")
                    log.error(traceback.format_exc()[:500])
                finally:
                    self._agent_busy = False

            threading.Thread(target=_run, daemon=True).start()
            self._next_sleep = 15
            return

        # v8 Phase 7: P5 backup-load DELETED. DJ owns deck loading via its
        # load_track tool, reading session.playlist. If the DJ agent fails
        # to load and silence looms, P1 silence recovery catches it with an
        # emergency play. That's the safety invariant — music never stops —
        # without Python making selection decisions.

        # === Everything fine — dynamic sleep ===
        if duration > 0 and position < (duration * 0.5):
            # First half: sleep longer
            time_until_half = (duration * 0.5) - position
            self._next_sleep = min(15, max(5, time_until_half / 3))
        elif remaining > 120:
            self._next_sleep = min(15, max(5, remaining / 10))
        else:
            self._next_sleep = 5

        self._record_playing_tracks()
        self._check_set_duration()

    def _format_timeline(self, meta) -> str:
        """Format track timeline for agent prompt."""
        if not meta:
            return "(no analysis)"
        timeline_str = meta.get("timeline", "")
        if not timeline_str:
            return f"BPM:{meta.get('bpm','?')} Key:{meta.get('key_musical','?')} Energy:{meta.get('energy_peak','?')}"
        try:
            import json as _json
            sections = _json.loads(timeline_str) if isinstance(timeline_str, str) else timeline_str
            parts = [f"{s['start']}s-{s['end']}s {s['section']}(energy:{s['energy']})" for s in sections]
            return " → ".join(parts)
        except Exception:
            return "(analysis error)"

    def _get_current_section(self, meta, position) -> str:
        """What section is the track currently in?"""
        if not meta or not meta.get("timeline"):
            return "unknown"
        try:
            import json as _json
            sections = _json.loads(meta["timeline"]) if isinstance(meta["timeline"], str) else meta["timeline"]
            for s in sections:
                if float(s["start"]) <= position <= float(s["end"]):
                    return f"{s['section']} (energy:{s['energy']}, {s['start']}s-{s['end']}s)"
            return "past end"
        except Exception:
            return "unknown"

    def _execute_signals(self, status, active_deck, idle_deck, position,
                         remaining, idle_loaded, idle_remaining):
        """Execute Session signals directly in Python — no DJ invocation.

        Signals are mechanical routing (skip → crossfade, idle empty →
        load rank-1, set ending → echo out) and belong on the Python side
        per Software 3.0. Keeping these out of the DJ prompt restores
        Flash's reliability on the creative transitions P4 handles.

        Called from _heartbeat() between P3 (scheduled exec) and P4 (DJ
        creative invocation). Each signal executor is idempotent and
        self-cleans its signal on success.
        """
        sched_file = runtime_path("scheduled-transition.json")
        lock_file = runtime_path("transition-pending.lock")

        # Stale-lock TTL guard: a transition-pending.lock file that's
        # older than 120s is almost always orphaned (transition aborted
        # mid-execution, daemon crashed before unlinking, etc.). Without
        # this, the lock blocks user_skip indefinitely (observed live
        # 8 May — skip silently failed for 14 minutes until manual rm).
        # 120s is well past the longest plausible transition duration
        # (echo_out caps at ~64s, crossfade at ~60s) plus generous slack.
        try:
            if lock_file.exists():
                age = time.time() - lock_file.stat().st_mtime
                if age > 120:
                    log.warning(
                        f"[SIGNAL] stale transition lock ({age:.0f}s old) "
                        f"— unlinking before signal exec"
                    )
                    lock_file.unlink(missing_ok=True)
        except Exception as exc:
            log.debug(f"stale lock check failed: {exc}")

        # Phase 7: if idle deck is externally owned, Python signals MUST NOT
        # write into it. DJ still manages treta-owned decks normally, but
        # skip/idle-load routed through idle_deck is pure co-being territory.
        idle_owned_external = self._deck_owned_by_external(idle_deck)

        # ── user_skip → immediate crossfade via scheduled-transition file ──
        user_skip = getattr(self.session, "user_skip", None)
        if user_skip and idle_owned_external:
            owner = self.session.deck_ownership.get(int(idle_deck))
            log.debug(
                f"[SIGNAL] user_skip dropped — idle deck {idle_deck} owned by {owner}"
            )
            self.session.user_skip = None
        elif user_skip and not self._transition_pending \
                and not sched_file.exists() and not lock_file.exists():
            if idle_loaded and idle_remaining > 10:
                at_position = int(position + 2)
                duration = 15 if remaining > 15 else max(2, int(remaining - 1))
                sched = {
                    "toDeck": idle_deck,
                    "atPosition": at_position,
                    "technique": "crossfade",
                    "duration": duration,
                    "activeDeck": active_deck,
                    "scheduledAt": position,
                    "executesIn": 2,
                    "bpmAfter": "keep",
                    "glideDuration": 10,
                }
                sched_file.write_text(json.dumps(sched, indent=2))
                log.info(
                    f"[SIGNAL] user_skip → scheduled crossfade to deck "
                    f"{idle_deck} at pos {at_position}, duration {duration}s"
                )
                if hasattr(self, '_ws_broadcast'):
                    self._ws_broadcast("log", {
                        "text": f"Skip: crossfade to deck {idle_deck} in 2s"
                    })
                self.session.user_skip = None
                return  # next tick's P3 picks up the scheduled file
            else:
                # Idle deck empty or almost done → can't skip yet. Try to
                # load first via idle_needs_load signal; skip stays set for
                # the next tick.
                self.session.idle_needs_load = True

        # ── set_ending → lowest-energy echo_out (stub: no active setter) ──
        if getattr(self.session, "set_ending", False):
            log.info("[SIGNAL] set_ending set — no Python executor wired yet, clearing")
            self.session.set_ending = False

        # ── auto-set idle_needs_load when idle deck holds an already-played
        #    track (2026-05-02 fix). Without this trigger, the played-on-idle
        #    case is invisible — outgoing track stays loaded after transition,
        #    next transition would replay it. Detected live: 3 cycles of
        #    NEXT==Hypnotica after Hypnotica had finished playing.
        #
        #    Path-format note: get_deck_paths returns relative paths
        #    (genre/filename.mp3), session.tracks_played stores absolute
        #    paths (/Users/.../genre/filename.mp3). Compare by filename
        #    suffix to bridge the two formats.
        try:
            from .playback_applier import get_deck_paths as _gd
            _idle_path_now = (_gd(self.config.mixxx.url) or {}).get(idle_deck, "") or ""
            if _idle_path_now and not getattr(self.session, "idle_needs_load", False):
                if _idle_was_played(_idle_path_now, self.tracks_played):
                    if self._library_exhausted():
                        # No fresh track left — accept the replay instead of
                        # spinning the guard. Log once until the library grows.
                        if not getattr(self, "_exhausted_logged", False):
                            log.warning(
                                f"[REPLAY-GUARD] library exhausted (all played) — "
                                f"allowing replay on deck {idle_deck}, not forcing reload"
                            )
                            self._exhausted_logged = True
                    else:
                        self._exhausted_logged = False
                        log.warning(
                            f"[REPLAY-GUARD] idle deck {idle_deck} holds already-played "
                            f"({Path(_idle_path_now).stem[:60]}) — setting idle_needs_load"
                        )
                        if hasattr(self, "_ws_broadcast"):
                            self._ws_broadcast("log", {
                                "text": (
                                    f"[REPLAY-GUARD] idle deck {idle_deck} held already-"
                                    f"played ({Path(_idle_path_now).stem[:60]}); reloading"
                                )
                            })
                        self.session.idle_needs_load = True
        except Exception as _exc:
            # Silent failure here was hiding the bug — log at debug so it
            # shows up in TUI without spamming production runs.
            log.debug(f"[REPLAY-GUARD] auto-detect skipped: {_exc}")

        # ── Sarathi: she SUGGESTS, she does NOT load ────────────────────────
        # Manish loads + mixes the decks himself; Treta only advises via
        # suggest_transition. Suppress ALL automatic idle-deck loading so she
        # never drops a track onto the deck he's mid-transition into (his deck
        # picks are his own). Explicit surgical load_track directives below
        # (replace_deck / play_specific_track) still apply — those are direct
        # commands, not auto-prep — but she doesn't emit those in Sarathi.
        # Genuine silence recovery (P1) is a separate path and still runs.
        if getattr(self.session, "sarathi_mode", False):
            if getattr(self.session, "idle_needs_load", False):
                log.debug("[sarathi] idle_needs_load suppressed — she suggests, Manish loads")
                self.session.idle_needs_load = False

        # ── Surgical load_track directive overrides the gates below ──────────
        # An active load_track directive for the idle deck (from replace_deck /
        # play_specific_track) is an EXPLICIT instruction. It must outrank both
        # the owned-external drop and the idle-stale gate — otherwise "change
        # deck N to X" silently no-ops whenever the deck already holds a valid,
        # unplayed track (BUG: replace_deck never lands; the directive check
        # lived inside _load_next_on_idle, which the gates never reached).
        _force_directive_load = False
        try:
            import os as _os
            _d_load = self.session.find_active_directive(
                "load_track", target="planner", deck=idle_deck,
            )
            if _d_load:
                _want = (_d_load.get("payload") or {}).get("path", "")
                from .playback_applier import get_deck_paths as _gdp
                _idle_now = (_gdp(self.config.mixxx.url) or {}).get(idle_deck, "") or ""
                # directive path is absolute; get_deck_paths is relative — compare basenames.
                if _want and _os.path.basename(_want) != _os.path.basename(_idle_now):
                    _force_directive_load = True
        except Exception:
            pass

        # ── idle_needs_load → load rank-1 via existing helper (BUG-17 dedup) ──
        idle_needs_load = getattr(self.session, "idle_needs_load", False)
        if _force_directive_load and not self._transition_pending:
            try:
                log.info(
                    f"[DIRECTIVE] surgical load_track on idle deck {idle_deck} — "
                    f"bypassing ownership/stale gates (explicit replace)"
                )
                self._load_next_on_idle(status)
                self.session.idle_needs_load = False
            except Exception as e:
                log.error(f"[DIRECTIVE] surgical load failed: {e}")
        elif idle_needs_load and idle_owned_external:
            owner = self.session.deck_ownership.get(int(idle_deck))
            log.debug(
                f"[SIGNAL] idle_needs_load dropped — deck {idle_deck} owned by {owner}"
            )
            self.session.idle_needs_load = False
        elif idle_needs_load and not self._transition_pending:
            # Idle counts as stale when:
            #   - empty (no track loaded), OR
            #   - the loaded track has < 60s remaining (won't survive
            #     a transition window), OR
            #   - the loaded track is the SAME file as the active deck.
            #     This last case (BUG-A: same-track-on-both-decks) used to
            #     escape the check: signal_set + idle_loaded + plenty of
            #     time-left = treated as fine, so the executor never
            #     swapped, and the next "transition" played the same track
            #     to the user, looking stuck.
            from .playback_applier import get_deck_paths
            deck_paths = get_deck_paths(self.config.mixxx.url)
            active_path = deck_paths.get(active_deck, "") or ""
            idle_path = deck_paths.get(idle_deck, "") or ""
            duplicate = bool(active_path) and active_path == idle_path

            # Match using the basename-aware helper — Mixxx file_path
            # (absolute) vs get_deck_paths (relative-to-music_dir) would
            # otherwise never compare equal, so a plain `in played_paths`
            # check silently passed every replay through.
            already_played = _idle_was_played(idle_path, self.tracks_played)
            # If the library is exhausted (every track played), an already-
            # played idle track is NOT "stale" — there's nothing fresher to
            # load, so reloading just spins the guard. Allow the replay.
            exhausted = self._library_exhausted()

            idle_stale = (
                (not idle_loaded)
                or idle_remaining < 60
                or duplicate
                or (already_played and not exhausted)
            )
            if idle_stale:
                try:
                    if duplicate:
                        log.info(
                            f"[SIGNAL] idle_needs_load → idle deck {idle_deck} "
                            f"holds the same track as active deck {active_deck}; "
                            f"forcing reload"
                        )
                    elif already_played:
                        log.warning(
                            f"[REPLAY-GUARD] idle_needs_load → idle deck {idle_deck} "
                            f"holds already-played track ({Path(idle_path).stem[:60]}); "
                            f"forcing reload"
                        )
                        if hasattr(self, "_ws_broadcast"):
                            self._ws_broadcast("log", {
                                "text": (
                                    f"[REPLAY-GUARD] forcing reload on deck "
                                    f"{idle_deck} (held played: "
                                    f"{Path(idle_path).stem[:60]})"
                                )
                            })
                    self._load_next_on_idle(status)
                    log.info("[SIGNAL] idle_needs_load → loaded rank-1 on idle deck")
                    self.session.idle_needs_load = False
                except Exception as e:
                    log.error(f"[SIGNAL] idle_needs_load executor failed: {e}")
            else:
                # Idle is fine — signal was stale, clear it.
                self.session.idle_needs_load = False

    def _library_exhausted(self) -> bool:
        """True if every track in the current-mood library has already played
        this set — no FRESH track left to load. When exhausted, the replay-guard
        must stop forcing reloads (a thin library otherwise spins forever:
        force reload → only played tracks available → reload a played one →
        guard re-fires). Better to allow a replay than to loop."""
        try:
            import glob
            import os
            slug = (self.mood or "").strip().lower().replace(" ", "-") or "melodic-techno"
            folder = self.config.library.music_path / slug
            local = {os.path.basename(p).lower()
                     for p in glob.glob(str(folder / "*.mp3"))
                     if not os.path.basename(p).startswith("._")}
            if not local:
                return False
            played = set()
            for t in (self.tracks_played or []):
                p = t.get("path") or t.get("file_path") or ""
                if p:
                    played.add(os.path.basename(p).lower())
            return len(local - played) == 0
        except Exception:
            return False

    def _emergency_play(self):
        """Silence! Direct API play first (fast + reliable), agent fallback for empty library."""
        from .main import _get_status

        self._emergency_count += 1
        try:
            url = self.config.mixxx.url

            # Try direct API first — pick any track from library, load, play
            import glob
            all_tracks = glob.glob(str(self.config.library.music_path / "**/*.mp3"), recursive=True)
            # Prefer originals when youtube source is off
            if not self.config.sources.youtube and self.config.sources.treta_originals:
                tracks = [t for t in all_tracks if "DJ Treta" in Path(t).name]
                if not tracks:
                    tracks = all_tracks  # fallback — music never stops
            else:
                tracks = all_tracks
            # BUG-11 fix (Phase A2 dry run #2 2026-04-19): prefer tracks
            # NOT already played this set. Previously emergency_play picked
            # uniformly at random, so once the library was exhausted and
            # DJ was saying "waiting" (BUG-10), emergency fallback would
            # replay already-played tracks — e.g. Samsara replayed as
            # track #6 after the first cycle. If ALL tracks are played,
            # fall back to full set (music-never-stops still wins).
            #
            # BUG-15 fix: legacy pre-BUG-8 tracks_played entries have no
            # `path` field, so path-based dedup misses them. Add a
            # title-overlap fallback with the same boilerplate-strip +
            # 0.8 threshold used elsewhere (BUG-14). An mp3 whose filename
            # fuzzy-matches any played title is treated as already-played.
            if tracks:
                played_paths = {
                    (t.get("path") or t.get("file_path") or "")
                    for t in (self.tracks_played or [])
                }
                played_paths.discard("")
                _BP = {
                    "original", "mix", "live", "extended", "remix", "feat",
                    "ft", "ft.", "official", "video", "audio", "edit",
                    "radio", "lyric", "visual", "visualizer", "dub", "vip",
                    "remastered", "vs", "vs.", "&", "-", "",
                }
                def _sig(t):
                    w = t.lower().replace("(", " ").replace(")", " ")\
                        .replace(",", " ").replace("[", " ").replace("]", " ")\
                        .replace(".mp3", " ").split()
                    return {x for x in w if x not in _BP}
                played_sigs = [
                    _sig(t.get("title", ""))
                    for t in (self.tracks_played or [])
                    if t.get("title")
                ]
                def _is_played(track_path: str) -> bool:
                    if track_path in played_paths:
                        return True
                    # Fallback to title-fuzzy against filename stem.
                    from pathlib import Path as _P
                    fn = _P(track_path).stem
                    cwords = _sig(fn)
                    if not cwords:
                        return False
                    for ps in played_sigs:
                        if not ps:
                            continue
                        overlap = len(cwords & ps) / min(len(cwords), len(ps))
                        if overlap >= 0.8:
                            return True
                    return False
                unplayed = [t for t in tracks if not _is_played(t)]
                if unplayed:
                    tracks = unplayed
                    log.info(f"Emergency pool: {len(unplayed)} unplayed tracks (from {len(all_tracks)} total)")
            if tracks:
                import random
                track = random.choice(tracks)
                # Load with retry — Mixxx may not be ready right after boot
                for attempt in range(3):
                    httpx.post(f"{url}/api/load", json={"deck": 1, "track": track}, timeout=5)
                    time.sleep(2)
                    st = _get_status(url)
                    if st and st.get("deck1", {}).get("track_loaded"):
                        break
                    log.warning(f"Emergency load attempt {attempt+1} — not loaded yet")
                    time.sleep(3)

                # Reset rate to native — emergency tracks should play at file BPM
                httpx.post(f"{url}/api/control",
                           json={"group": "[Channel1]", "key": "rate_ratio", "value": 1.0}, timeout=3)
                httpx.post(f"{url}/api/control",
                           json={"group": "[Channel1]", "key": "sync_enabled", "value": 0}, timeout=3)
                # Volume MUST be up — idle decks are kept at vol 0, so an
                # emergency play would be silent without this (music-never-stop).
                httpx.post(f"{url}/api/volume", json={"deck": 1, "level": 1.0}, timeout=3)
                httpx.post(f"{url}/api/play", json={"deck": 1}, timeout=3)
                httpx.post(f"{url}/api/crossfade", json={"position": 0.0}, timeout=3)
                time.sleep(2)
                log.info(f"Emergency play: {Path(track).stem[:50]} (rate reset)")
                self._record_playing_tracks()
                return

            # Empty library — generate directly (bypass agent to avoid blocking)
            if self.config.sources.treta_originals:
                log.info("Emergency: generating track directly (no agent)")
                from .tools import generate_track as _gen
                result = _gen(
                    prompt=f"Atmospheric {self.mood or 'melodic-techno'} track with driving rhythm and evolving textures",
                    bpm=125, key="A minor", genre=self.mood or "melodic-techno",
                    duration="full", name="Emergency Pulse",
                )
                log.info(f"Emergency generate: {result[:200]}")
                # Try to load + play the generated track
                if "Generated:" in result:
                    filepath = result.split("Generated: ")[1].split(" |")[0]
                    httpx.post(f"{url}/api/load", json={"deck": 1, "track": filepath}, timeout=5)
                    time.sleep(2)
                    httpx.post(f"{url}/api/volume", json={"deck": 1, "level": 1.0}, timeout=3)
                    httpx.post(f"{url}/api/play", json={"deck": 1}, timeout=3)
                    httpx.post(f"{url}/api/crossfade", json={"position": 0.0}, timeout=3)
                    log.info(f"Emergency play: {Path(filepath).stem[:50]}")
                    self._record_playing_tracks()
            elif self.config.sources.youtube:
                # Empty library + youtube=true: route to library_manager peer
                # via library_need signal — DJ agent's v8 prompt explicitly
                # forbids search_music/download_track (those belong to the
                # library peer), so calling DJ here just produces the
                # "I cannot fulfill" refuse-loop we observed 2026-05-02
                # (28 wasted Flash calls / $0.012 before this fix landed).
                # Library_manager runs as a peer thread (library_loop.py)
                # and processes library_need signals natively.
                mood = self.mood or "melodic-techno"
                self.session.library_need = {
                    "mood": mood,
                    "count": 3,
                    "reason": "emergency_play — empty library, silence imminent",
                    "ts": time.time(),
                }
                log.info(
                    f"[SOS] empty library — emitted library_need(mood={mood}, "
                    f"count=3) for library_manager peer to fulfill"
                )
            self._record_playing_tracks()
        except Exception as e:
            import traceback
            log.error(f"Emergency play error: {type(e).__name__}: {e}")
            log.error(traceback.format_exc()[:500])
        finally:
            self._emergency_running = False
            self._agent_busy = False
