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
        self._last_waveform_track = ""  # only send waveform once per track

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
        await ws.send(json.dumps(state))

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
                "energyArc": s.get("energy_arc", [])[-20:],
                "startedAt": s.get("started_at", 0),
            }

        # VU
        d1_live = live.get("deck1", {})
        d2_live = live.get("deck2", {})

        # Waveform — only send once per track change (3842 × 3 = heavy)
        waveform = None  # null = frontend keeps previous
        track_key = f"{active_deck}:{title}"
        if track_key != self._last_waveform_track:
            self._last_waveform_track = track_key
            if active_info.get("waveform_summary"):
                ws = active_info["waveform_summary"]
                if ws.get("has_waveform"):
                    waveform = {
                        "low": ws.get("low", []),
                        "mid": ws.get("mid", []),
                        "high": ws.get("high", []),
                        "data_size": ws.get("data_size", 0),
                    }

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
            "set": set_info,
            "brain": {
                "lastDecision": self.being._last_result[:300] if self.being._last_result else "",
            },
            "waveform": waveform,
            "history": self._history[-20:],
            "listeners": 0,
            "timestamp": int(time.time() * 1000),
        }
