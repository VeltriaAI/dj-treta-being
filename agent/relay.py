"""Relay engine — pushes DJ state to dj.treta.life WebSocket.

Ported from dj-treta-live/relay-agent/relay.py.
Merged into the being — no separate process needed.
"""

import asyncio
import json
import logging
import math
import time
from collections import deque
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")

# ── Camelot Key Mapping ──────────────────────────────────────────────

MIXXX_KEY_TO_MUSICAL = {
    0: "", 1: "C", 2: "Db", 3: "D", 4: "Eb", 5: "E", 6: "F",
    7: "F#", 8: "G", 9: "Ab", 10: "A", 11: "Bb", 12: "B",
    13: "Cm", 14: "C#m", 15: "Dm", 16: "Ebm", 17: "Em", 18: "Fm",
    19: "F#m", 20: "Gm", 21: "G#m", 22: "Am", 23: "Bbm", 24: "Bm",
}

KEY_TO_CAMELOT = {
    "C": "8B", "Db": "3B", "D": "10B", "Eb": "5B", "E": "12B", "F": "7B",
    "F#": "2B", "G": "9B", "Ab": "4B", "A": "11B", "Bb": "6B", "B": "1B",
    "Cm": "5A", "C#m": "12A", "Dm": "7A", "Ebm": "2A", "Em": "9A", "Fm": "4A",
    "F#m": "11A", "Gm": "6A", "G#m": "1A", "Am": "8A", "Bbm": "3A", "Bm": "10A",
}


def format_key(mixxx_key: int) -> str:
    musical = MIXXX_KEY_TO_MUSICAL.get(mixxx_key, "")
    camelot = KEY_TO_CAMELOT.get(musical, "")
    return f"{musical} ({camelot})" if musical else ""


def parse_title(raw: str, file_path: str = "") -> tuple[str, str]:
    """Parse 'Uploader - Artist - Title' into (artist, title)."""
    if not raw:
        if file_path:
            raw = Path(file_path).stem
        else:
            return ("", "Unknown")
    parts = raw.split(" - ", 2)
    if len(parts) >= 3:
        return (parts[1].strip(), parts[2].strip())
    elif len(parts) == 2:
        return (parts[0].strip(), parts[1].strip())
    return ("", raw.strip())


# ── Perception Engine ────────────────────────────────────────────────

class PerceptionEngine:
    """Derives energy, mood, beat phase from VU meter history."""

    def __init__(self, buffer_size: int = 300):
        self.history: deque[dict] = deque(maxlen=buffer_size)

    def add_reading(self, live: dict):
        self.history.append({
            "master_vu_left": live.get("master_vu_left", 0),
            "master_vu_right": live.get("master_vu_right", 0),
            "deck1": live.get("deck1", {}),
            "deck2": live.get("deck2", {}),
            "timestamp": live.get("timestamp", 0),
        })

    def analyze(self, active_deck: int = 1) -> dict:
        result = {
            "energy": 0.0, "energyDirection": "steady", "beatPhase": "silent",
            "tension": 0, "density": 0.0, "mood": "silent",
            "transitionReady": False, "breakdownDetected": False,
            "buildupDetected": False, "dropDetected": False, "masterLoudness": 0.0,
        }
        if not self.history:
            return result

        latest = self.history[-1]
        avg_vu = (latest["master_vu_left"] + latest["master_vu_right"]) / 2
        result["masterLoudness"] = avg_vu
        result["energy"] = round(min(10, avg_vu * 12), 1)
        n = len(self.history)

        # Energy direction
        if n >= 20:
            recent = list(self.history)[-10:]
            prev = list(self.history)[-20:-10]
            re = sum((r["master_vu_left"] + r["master_vu_right"]) / 2 for r in recent) / 10 * 12
            pe = sum((r["master_vu_left"] + r["master_vu_right"]) / 2 for r in prev) / 10 * 12
            delta = re - pe
            if delta > 1.5: result["energyDirection"] = "building"
            elif delta > 0.4: result["energyDirection"] = "rising"
            elif delta < -1.5: result["energyDirection"] = "dropping"
            elif delta < -0.4: result["energyDirection"] = "falling"

        # Beat phase
        dk = latest.get(f"deck{active_deck}", latest.get("deck1", {}))
        if not dk.get("playing") or dk.get("bpm", 0) <= 0:
            result["beatPhase"] = "silent"
        elif dk.get("beat_active"):
            result["beatPhase"] = "kick"
        elif 0.4 < dk.get("beat_distance", 0) < 0.6:
            result["beatPhase"] = "offbeat"
        else:
            result["beatPhase"] = "between"

        # Breakdown / buildup / drop detection
        if n >= 40:
            r20 = list(self.history)[-20:]
            p20 = list(self.history)[-40:-20]
            re2 = sum((r["master_vu_left"] + r["master_vu_right"]) / 2 for r in r20) / 20 * 12
            pe2 = sum((r["master_vu_left"] + r["master_vu_right"]) / 2 for r in p20) / 20 * 12
            if pe2 > 2 and re2 < pe2 * 0.6:
                result["breakdownDetected"] = True
        if result["energyDirection"] == "building":
            result["buildupDetected"] = True
        if n >= 10:
            vr = list(self.history)[-5:]
            jb = list(self.history)[-10:-5]
            vr_avg = sum((r["master_vu_left"] + r["master_vu_right"]) / 2 for r in vr) / 5 * 12
            jb_avg = sum((r["master_vu_left"] + r["master_vu_right"]) / 2 for r in jb) / 5 * 12
            if jb_avg < 3 and vr_avg > jb_avg + 3:
                result["dropDetected"] = True

        # Density
        if n >= 5:
            window = min(n, 50)
            vus = [(list(self.history)[-1-i]["master_vu_left"] + list(self.history)[-1-i]["master_vu_right"]) / 2 for i in range(window)]
            mean = sum(vus) / len(vus)
            if mean > 0.01:
                variance = sum((v - mean) ** 2 for v in vus) / len(vus)
                cv = math.sqrt(variance) / mean
                result["density"] = round(min(10, max(0, (1 - cv) * 10)), 1)

        # Tension
        tension = 0
        if result["buildupDetected"]: tension += 4
        if result["energyDirection"] in ("rising", "building"): tension += 2
        if result["density"] > 7 and result["energy"] > 7: tension += 2
        if result["breakdownDetected"]: tension += 3
        result["tension"] = min(10, tension)

        # Mood from BPM + energy
        bpm = dk.get("bpm", 0)
        e = result["energy"]
        if bpm <= 0: result["mood"] = "silent"
        elif e < 3: result["mood"] = "melancholic" if bpm < 120 else "dark"
        elif e > 7: result["mood"] = "euphoric" if bpm >= 128 else "driving"
        elif result["density"] > 7 and e > 5: result["mood"] = "hypnotic"
        elif bpm < 100: result["mood"] = "chill"
        elif bpm < 120: result["mood"] = "dreamy"
        elif bpm < 128: result["mood"] = "groovy"
        elif bpm < 135: result["mood"] = "driving"
        elif bpm < 145: result["mood"] = "energetic"
        else: result["mood"] = "intense"

        # Transition readiness
        if result["buildupDetected"] or result["dropDetected"]:
            result["transitionReady"] = False
        elif result["breakdownDetected"]:
            result["transitionReady"] = True
        elif result["energyDirection"] in ("falling", "dropping"):
            result["transitionReady"] = True
        elif result["energyDirection"] == "steady" and e < 5:
            result["transitionReady"] = True

        return result


# ── Relay Engine ─────────────────────────────────────────────────────

class RelayEngine:
    """Pushes DJ state to dj.treta.life WebSocket server."""

    def __init__(self, config, being):
        self.config = config
        self.being = being
        self.perception = PerceptionEngine()
        self._history = []
        self._last_active_title = ""
        self._track_info = {}
        self._last_waveform_track = {1: "", 2: ""}  # per-deck waveform tracking
        self._pushes_since_connect = 0  # send waveform on first few pushes after connect
        self._decision_log = []  # timestamped brain decisions
        self._last_energy_sample = 0.0  # wall clock of last energy arc sample

    async def run(self):
        """Main relay loop — connect and push state."""
        import websockets
        import websockets.exceptions

        url = self.config.relay.server_url
        token = self.config.relay.token
        hz = self.config.relay.push_hz

        while self.being._running:
            try:
                headers = {"Authorization": f"Bearer {token}"}
                async with websockets.connect(
                    url, additional_headers=headers,
                    ping_interval=20, ping_timeout=10, close_timeout=5,
                ) as ws:
                    log.info(f"Relay connected to {url}")
                    self._pushes_since_connect = 0
                    self._last_waveform_track = {1: "", 2: ""}  # force waveform on reconnect
                    interval = 1.0 / hz
                    while self.being._running:
                        start = time.time()
                        await self._poll_and_push(ws)
                        elapsed = time.time() - start
                        await asyncio.sleep(max(0, interval - elapsed))
            except Exception as e:
                log.warning(f"Relay connection lost: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _poll_and_push(self, ws):
        """Poll Mixxx, assemble state, push."""
        mixxx = self.config.mixxx.url

        # Poll live data (VU meters, beats)
        try:
            r = httpx.get(f"{mixxx}/api/live", timeout=1)
            live = r.json()
            self.perception.add_reading(live)
        except Exception:
            live = {}

        # Poll status
        try:
            status = httpx.get(f"{mixxx}/api/status", timeout=1).json()
        except Exception:
            status = {}

        # Poll track info (less frequently)
        for dk in [1, 2]:
            if status.get(f"deck{dk}", {}).get("track_loaded"):
                try:
                    self._track_info[dk] = httpx.get(
                        f"{mixxx}/api/deck/{dk}/track_info", timeout=1
                    ).json()
                except Exception:
                    pass

        # Assemble and send
        state = self._assemble_state(live, status)

        # Sample energy arc every 10s — append to set for DB storage + realtime push
        now = time.time()
        if now - self._last_energy_sample >= 10 and self.being.current_set:
            self._last_energy_sample = now
            self._sample_energy_arc(state, status)

        await ws.send(json.dumps(state))

    def _get_current_intent(self, perc, active_status, status, idle_deck) -> str:
        """Generate human-readable intent from perception + state."""
        e = perc["energy"]
        direction = perc["energyDirection"]
        remaining = active_status.get("remaining_seconds", 0)
        idle_loaded = status.get(f"deck{idle_deck}", {}).get("track_loaded", False)

        if perc["breakdownDetected"]:
            return f"Breakdown detected at energy {e:.1f}. Holding steady, letting the moment breathe."
        elif perc["dropDetected"]:
            return f"Drop hit! Energy surged to {e:.1f}. Riding the peak."
        elif perc["buildupDetected"]:
            return f"Buildup in progress. Energy climbing at {e:.1f}. Tension building."
        elif direction == "building":
            return f"Energy rising to {e:.1f}. Building momentum for the next phase."
        elif direction == "dropping":
            return f"Controlled descent from {e:.1f}. Creating space for the next build."
        elif remaining < 120 and idle_loaded:
            return f"Track ending in {remaining:.0f}s. Next track ready. Preparing transition."
        elif remaining < 120 and not idle_loaded:
            return f"Track ending in {remaining:.0f}s. Searching for the next track."
        elif e > 7:
            return f"Peak energy at {e:.1f}. Maintaining intensity."
        elif e < 3:
            return f"Low energy at {e:.1f}. Ambient passage."
        else:
            return f"Steady groove at energy {e:.1f}. Flow state."

    def _get_transition_analysis(self, active_status, status, idle_deck) -> str:
        """Generate transition analysis from deck states."""
        idle_status = status.get(f"deck{idle_deck}", {})
        if not idle_status.get("track_loaded"):
            return "No track loaded on standby deck."

        active_bpm = active_status.get("bpm", 0)
        idle_bpm = idle_status.get("bpm", 0)
        active_key = active_status.get("key", 0)
        idle_key = idle_status.get("key", 0)

        if not active_bpm or not idle_bpm:
            return "Analyzing tracks..."

        # Key compatibility
        active_key_str = format_key(active_key)
        idle_key_str = format_key(idle_key)

        # Determine technique based on BPM difference
        bpm_diff = abs(active_bpm - idle_bpm)
        if bpm_diff < 2:
            technique = "Smooth blend"
            curve = "S-type"
        elif bpm_diff < 5:
            technique = "Bass swap"
            curve = "S-type"
        else:
            technique = "Filter sweep"
            curve = "Linear"

        return (
            f"{technique} locked at {idle_bpm:.1f} BPM. "
            f"Key: {active_key_str} → {idle_key_str}. "
            f"Crossfader curve: {curve}."
        )

    def _assemble_state(self, live: dict, status: dict) -> dict:
        """Build DJWebState for the frontend."""
        from .main import _active_idle_decks

        if not status:
            return {"phase": "offline", "timestamp": int(time.time() * 1000)}

        active_deck, idle_deck = _active_idle_decks(status)
        active_status = status.get(f"deck{active_deck}", {})
        active_info = self._track_info.get(active_deck, {})
        idle_info = self._track_info.get(idle_deck, {})

        # Parse title
        raw_title = active_info.get("title", "")
        file_path = active_info.get("file_path", "")
        artist, title = parse_title(raw_title, file_path)

        # Key
        key_str = format_key(active_status.get("key", 0))

        # Perception
        perc = self.perception.analyze(active_deck)

        # Track history
        if title and title != self._last_active_title:
            self._last_active_title = title
            self._history.append({
                "title": title, "artist": artist,
                "playedAt": time.strftime("%H:%M"),
                "energy": round(perc["energy"]),
            })

        # Next track
        next_track = None
        idle_raw = idle_info.get("title", "")
        if idle_raw and idle_raw != raw_title:
            na, nt = parse_title(idle_raw, idle_info.get("file_path", ""))
            next_track = {"title": nt, "artist": na}

        # Phase
        phase = "offline"
        if any(status.get(f"deck{d}", {}).get("playing") for d in [1, 2]):
            phase = "playing"

        # Set info — full metadata for archive UI
        set_info = {"elapsed": 0, "remaining": 0, "tracksPlayed": 0}
        if self.being.current_set:
            s = self.being.current_set
            elapsed = round(time.time() - s["started_at"])
            target_secs = s["target_duration"] * 60
            set_info = {
                "id": s["id"],
                "number": s.get("set_number", 0),
                "title": s.get("title", ""),
                "mood": s.get("mood", ""),
                "genre": s.get("genre", ""),
                "status": s.get("status", "live"),
                "elapsed": elapsed,
                "remaining": max(0, target_secs - elapsed),
                "targetDuration": target_secs,
                "tracksPlayed": len(self.being.tracks_played),
                "peakEnergy": s.get("peak_energy", 0),
                "energyArc": s.get("energy_arc", [])[-60:],  # last 10 min at 10s intervals
                "startedAt": s.get("started_at", 0),
            }

        # VU
        d1_live = live.get("deck1", {})
        d2_live = live.get("deck2", {})

        # Per-deck data for Neural Deck page
        decks = {}
        for dk in [1, 2]:
            dk_status = status.get(f"deck{dk}", {})
            dk_info = self._track_info.get(dk, {})
            dk_raw = dk_info.get("title", "")
            dk_artist, dk_title = parse_title(dk_raw, dk_info.get("file_path", ""))
            dk_key = format_key(dk_status.get("key", 0))
            dk_live = live.get(f"deck{dk}", {})

            # Waveform per deck — only send once per track change
            dk_waveform = None
            dk_track_key = f"{dk}:{dk_title}"
            if dk_track_key != self._last_waveform_track.get(dk, ""):
                self._last_waveform_track[dk] = dk_track_key
                if dk_info.get("waveform_summary"):
                    ws = dk_info["waveform_summary"]
                    if ws.get("has_waveform"):
                        dk_waveform = {
                            "low": ws.get("low", []),
                            "mid": ws.get("mid", []),
                            "high": ws.get("high", []),
                            "data_size": ws.get("data_size", 0),
                        }

            decks[f"deck{dk}"] = {
                "title": dk_title or "",
                "artist": dk_artist,
                "bpm": round(dk_status.get("bpm", 0), 1),
                "key": dk_key,
                "playing": dk_status.get("playing", False),
                "trackLoaded": dk_status.get("track_loaded", False),
                "syncEnabled": dk_status.get("sync_enabled", False),
                "duration": round(dk_status.get("duration", 0), 1),
                "elapsed": round(dk_status.get("position_seconds", 0), 1),
                "remaining": round(dk_status.get("remaining_seconds", 0), 1),
                "volume": round(dk_status.get("volume", 1), 2),
                "eqHi": round(dk_status.get("eq_hi", 1), 1),
                "eqMid": round(dk_status.get("eq_mid", 1), 1),
                "eqLo": round(dk_status.get("eq_lo", 1), 1),
                "vuLeft": round(dk_live.get("vu_left", 0), 3),
                "vuRight": round(dk_live.get("vu_right", 0), 3),
                "waveform": dk_waveform,
            }

        # Decision log — track brain decisions with timestamps
        current_decision = self.being._last_result[:200] if self.being._last_result else ""
        if current_decision and (not self._decision_log or self._decision_log[-1]["text"] != current_decision):
            self._decision_log.append({
                "time": time.strftime("%H:%M:%S"),
                "text": current_decision,
            })
            if len(self._decision_log) > 50:
                self._decision_log = self._decision_log[-50:]

        # Harmonic map — current + next key for Camelot wheel
        idle_status = status.get(f"deck{idle_deck}", {})
        active_key_num = active_status.get("key", 0)
        idle_key_num = idle_status.get("key", 0)
        active_musical = MIXXX_KEY_TO_MUSICAL.get(active_key_num, "")
        idle_musical = MIXXX_KEY_TO_MUSICAL.get(idle_key_num, "")
        active_camelot = KEY_TO_CAMELOT.get(active_musical, "")
        idle_camelot = KEY_TO_CAMELOT.get(idle_musical, "")

        # Key movement description
        key_movement = ""
        if active_camelot and idle_camelot:
            try:
                a_num = int(active_camelot[:-1])
                i_num = int(idle_camelot[:-1])
                diff = (i_num - a_num) % 12
                if diff == 0 and active_camelot[-1] != idle_camelot[-1]:
                    key_movement = "Relative key (parallel)"
                elif diff == 0:
                    key_movement = "Same key"
                elif diff == 1 or diff == 11:
                    key_movement = f"Energy {'Boost' if diff == 1 else 'Drop'} (+{diff} semitones)"
                elif diff <= 2 or diff >= 10:
                    key_movement = "Compatible key"
                else:
                    key_movement = f"Key jump ({diff} steps)"
            except ValueError:
                pass

        # Transition countdown
        transition_countdown = ""
        remaining_secs = active_status.get("remaining_seconds", 0)
        if remaining_secs > 0 and idle_status.get("track_loaded"):
            mins = int(remaining_secs // 60)
            secs = int(remaining_secs % 60)
            transition_countdown = f"{mins:02d}:{secs:02d}"

        return {
            "phase": phase,
            "activeDeck": active_deck,
            "currentTrack": {
                "title": title or "Unknown",
                "artist": artist,
                "bpm": round(active_status.get("bpm", 0), 1),
                "key": key_str,
                "energy": round(perc["energy"]),
                "duration": round(active_status.get("duration", 0), 1),
                "elapsed": round(active_status.get("position_seconds", 0), 1),
                "remaining": round(active_status.get("remaining_seconds", 0), 1),
            },
            "nextTrack": next_track,
            "mood": perc["mood"] if perc["mood"] != "silent" else (self.being.mood or ""),
            "perception": {
                "energy": perc["energy"],
                "energyDirection": perc["energyDirection"],
                "beatPhase": perc["beatPhase"],
                "density": perc["density"],
                "mood": perc["mood"],
            },
            "vu": {
                "masterLeft": round(live.get("master_vu_left", 0), 3),
                "masterRight": round(live.get("master_vu_right", 0), 3),
                "deck1Left": round(d1_live.get("vu_left", 0), 3),
                "deck1Right": round(d1_live.get("vu_right", 0), 3),
                "deck2Left": round(d2_live.get("vu_left", 0), 3),
                "deck2Right": round(d2_live.get("vu_right", 0), 3),
            },
            "crossfader": round(live.get("crossfader", 0), 3),
            "decks": decks,
            "transition": {
                "strategy": self._get_transition_analysis(active_status, status, idle_deck),
                "countdown": transition_countdown,
                "scheduled": self._get_scheduled_transition(),
            },
            "harmonicMap": {
                "currentKey": active_camelot,
                "nextKey": idle_camelot,
                "currentMusical": active_musical,
                "nextMusical": idle_musical,
                "movement": key_movement,
            },
            "set": set_info,
            "brain": {
                "lastDecision": current_decision,
                "currentIntent": self._get_current_intent(perc, active_status, status, idle_deck),
                "transitionAnalysis": self._get_transition_analysis(active_status, status, idle_deck),
                "processingLoad": round(min(100, perc["tension"] * 10 + (10 if perc["buildupDetected"] else 0) + (15 if perc["dropDetected"] else 0))),
                "decisionLog": self._decision_log[-20:],
            },
            "history": self._history[-20:],
            "finishedSet": self._get_finished_set(),
            "listeners": 0,
            "timestamp": int(time.time() * 1000),
        }

    def _get_scheduled_transition(self) -> dict | None:
        """Read scheduled transition data from temp file."""
        try:
            f = Path("/tmp/dj-treta-scheduled-transition.json")
            if f.exists():
                return json.loads(f.read_text())
        except Exception:
            pass
        return None

    def _get_finished_set(self) -> dict | None:
        """Return finished set data once, then clear."""
        finished = getattr(self.being, 'last_finished_set', None)
        if finished:
            self.being.last_finished_set = None  # clear after sending
            return {
                "id": finished["id"],
                "title": finished.get("title", ""),
                "mood": finished.get("mood", ""),
                "genre": finished.get("genre", ""),
                "trackCount": finished.get("track_count", 0),
                "peakEnergy": finished.get("peak_energy", 0),
                "energyArc": finished.get("energy_arc", []),
                "startedAt": finished.get("started_at", 0),
                "endedAt": finished.get("ended_at", 0),
                "durationMinutes": round((finished.get("ended_at", 0) - finished.get("started_at", 0)) / 60, 1),
            }
        return None

    def _sample_energy_arc(self, state: dict, status: dict):
        """Sample energy data point every 10s — stored on set for DB + pushed to frontend."""
        try:
            s = self.being.current_set
            if not s:
                return

            elapsed = round(time.time() - s["started_at"])
            perc = state.get("perception", {})
            energy = perc.get("energy", 0)
            mood = perc.get("mood", "")
            direction = perc.get("energyDirection", "steady")

            # Current track + section
            ct = state.get("currentTrack", {})
            track = ct.get("title", "")
            track_pos = ct.get("elapsed", 0)

            # Get section from DB timeline
            section = ""
            from .db import get_track_by_path
            active_deck = state.get("activeDeck", 1)
            info = self._track_info.get(active_deck, {})
            file_path = info.get("file_path", "")
            if file_path:
                meta = get_track_by_path(file_path)
                if meta and meta.get("timeline"):
                    try:
                        sections = json.loads(meta["timeline"]) if isinstance(meta["timeline"], str) else meta["timeline"]
                        for sec in sections:
                            if float(sec["start"]) <= track_pos <= float(sec["end"]):
                                section = sec.get("section", "")
                                break
                    except Exception:
                        pass

            sample = {
                "t": elapsed,
                "energy": round(energy, 1),
                "direction": direction,
                "mood": mood,
                "track": track[:50],
                "section": section,
                "bpm": ct.get("bpm", 0),
            }

            # Append to set's energy_arc
            if not isinstance(s.get("energy_arc"), list):
                s["energy_arc"] = []
            s["energy_arc"].append(sample)

            # Update peak energy
            if energy > s.get("peak_energy", 0):
                s["peak_energy"] = round(energy, 1)

        except Exception:
            pass
