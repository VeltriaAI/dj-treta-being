"""DJ Treta v2 — Always-alive Being daemon.

The Being starts and stays alive. Playing a set is just one thing she does.
She can be idle, playing, or transitioning. She's always listening for commands.

Usage:
    python -m agent                    # Start Being (idle, waiting for commands)
    python -m agent --play melodic-techno --duration 60   # Start + play immediately
"""

import argparse
import json
import logging
import re
import signal
import threading
import time
from pathlib import Path

import httpx
from litellm import completion

from .config import load_config, Config
from .agents import create_dj_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dj-treta")

STATE_FILE = Path("/tmp/dj-treta-state.json")
COMMAND_FILE = Path("/tmp/dj-treta-command.json")
PERSIST_FILE = Path(__file__).parent.parent / ".beings" / "session.json"  # survives restarts


# ── Mixxx helpers ─────────────────────────────────────────────────────

def clean_mixxx(url: str):
    log.info("Cleaning Mixxx...")
    try:
        c = httpx.Client(base_url=url, timeout=5)
        for deck in [1, 2]:
            c.post("/api/pause", json={"deck": deck})
            c.post("/api/eject", json={"deck": deck})
            c.post("/api/volume", json={"deck": deck, "level": 1.0})
            for band in ["hi", "mid", "lo"]:
                c.post("/api/eq", json={"deck": deck, band: 1.0})
            c.post("/api/filter", json={"deck": deck, "value": 0.5})
        c.post("/api/crossfade", json={"position": 0.5})
        c.close()
        log.info("Mixxx cleaned")
    except Exception as e:
        log.warning(f"Clean failed: {e}")


def get_status(url: str) -> dict | None:
    try:
        return httpx.get(f"{url}/api/status", timeout=2).json()
    except Exception:
        return None


def get_track_info_api(url: str, deck: int) -> dict | None:
    try:
        return httpx.get(f"{url}/api/deck/{deck}/track_info", timeout=2).json()
    except Exception:
        return None


def scan_genres(music_dir: Path) -> list[str]:
    if not music_dir.exists():
        return []
    return [d.name for d in sorted(music_dir.iterdir()) if d.is_dir() and not d.name.startswith('.')]


def count_tracks(music_dir: Path) -> int:
    n = 0
    for d in music_dir.iterdir():
        if d.is_dir() and not d.name.startswith('.'):
            n += sum(1 for f in d.iterdir() if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'))
    return n


# ── Talk fast-path ────────────────────────────────────────────────────

_ACTION_PATTERN = re.compile(
    r'\b(play|load|skip|transition|mix|blend|swap|download|search|find'
    r'|change|switch|darker|lighter|harder|softer|build|drop|cut'
    r'|bass|eq|filter|volume|crossfade|sync)\b',
    re.IGNORECASE,
)

def fast_talk(message: str, config: Config, context: str) -> str:
    try:
        resp = completion(
            model=config.llm.model,
            messages=[
                {"role": "system", "content": "You are DJ Treta, an AI DJ Being. Be brief, direct, warm. 1-3 sentences."},
                {"role": "user", "content": f'"{message}"\n\n{context}\n\nRespond naturally.'},
            ],
            api_base=config.llm.api_base,
            api_key=config.llm.api_key,
            temperature=config.llm.temperature,
            timeout=config.llm.timeout,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"(error: {e})"


# ── Quick skip ────────────────────────────────────────────────────────

def _quick_skip(config: Config) -> str:
    try:
        status = get_status(config.mixxx.url)
        if not status:
            return "Mixxx not reachable"

        d1 = status.get("deck1", {})
        idle = 2 if d1.get("playing") else 1

        c = httpx.Client(base_url=config.mixxx.url, timeout=5)
        for genre_dir in sorted(config.library.music_path.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            for f in sorted(genre_dir.iterdir()):
                if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                    c.post("/api/load", json={"deck": idle, "track": str(f)})
                    time.sleep(1.0)
                    c.post("/api/sync", json={"deck": idle})
                    c.post("/api/play", json={"deck": idle})
                    time.sleep(0.3)
                    c.post("/api/transition", json={"deck": idle, "duration": 20})
                    c.close()
                    return f"Skipping — transitioning to {f.stem}"
        c.close()
        return "No tracks found"
    except Exception as e:
        return f"Skip error: {e}"


# ── Being ─────────────────────────────────────────────────────────────

class DJTretaBeing:
    """The always-alive DJ Being."""

    def __init__(self, config: Config):
        self.config = config
        self.agent = None
        self._running = True

        # Set state
        self.phase = "idle"  # idle, playing, stopped
        self.mood = ""
        self.set_start = 0.0
        self.set_duration = 0  # 0 = infinite
        self.tracks_played: list[dict] = []
        self._last_command = ""
        self._last_result = ""

    def _save_session(self):
        """Persist session state to disk — survives restarts."""
        try:
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "phase": self.phase,
                "mood": self.mood,
                "set_start": self.set_start,
                "set_duration": self.set_duration,
                "tracks_played": self.tracks_played,
                "saved_at": time.time(),
            }
            PERSIST_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _restore_session(self):
        """Restore session from disk if available and recent (< 1 hour old)."""
        try:
            if not PERSIST_FILE.exists():
                return False
            data = json.loads(PERSIST_FILE.read_text())
            saved_at = data.get("saved_at", 0)
            age = time.time() - saved_at
            if age > 3600:  # older than 1 hour, ignore
                log.info("Old session found but expired (>1h), starting fresh")
                return False

            self.mood = data.get("mood", self.mood)
            self.tracks_played = data.get("tracks_played", [])
            self.set_start = data.get("set_start", 0)
            self.set_duration = data.get("set_duration", 0)
            phase = data.get("phase", "idle")

            log.info(f"Restored session: mood={self.mood}, tracks={len(self.tracks_played)}, phase={phase}")

            # Check if Mixxx still has something playing
            status = get_status(self.config.mixxx.url)
            if status:
                d1 = status.get("deck1", {})
                d2 = status.get("deck2", {})
                if d1.get("playing") or d2.get("playing"):
                    self.phase = "playing"
                    log.info("Mixxx still has music playing — resuming")
                    return True

            # Mixxx silent but session was playing — restart the set
            if phase == "playing" and self.mood:
                self.phase = "idle"  # play_set will set to playing
                log.info(f"Resuming {self.mood} set...")
                threading.Thread(target=self.play_set, args=(self.mood, 0), daemon=True).start()
                return True

            return True
        except Exception as e:
            log.warning(f"Could not restore session: {e}")
            return False

    def start(self):
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())
        signal.signal(signal.SIGINT, lambda s, f: self.stop())

        log.info("DJ Treta Being alive")

        # Create agent
        log.info("Creating DJ agent...")
        self.agent = create_dj_agent(self.config)

        # Try to restore previous session
        self._restore_session()

        # Start state writer
        self._state_thread = threading.Thread(target=self._state_loop, daemon=True)
        self._state_thread.start()

        # Main loop — always alive, check commands, manage set
        while self._running:
            self._check_commands()

            if self.phase == "playing":
                self._check_transition()

            time.sleep(3)

        log.info("DJ Treta Being shutting down")

    def stop(self):
        self._save_session()
        self._running = False

    def play_set(self, mood: str, duration: int = 0):
        """Start a DJ set. duration=0 means play forever until told to stop."""
        self.mood = mood
        self.set_duration = duration * 60 if duration > 0 else 0
        self.set_start = time.time()
        self.tracks_played = []
        self.phase = "playing"

        n_tracks = count_tracks(self.config.library.music_path)
        genres = scan_genres(self.config.library.music_path)

        clean_mixxx(self.config.mixxx.url)

        log.info(f"Starting {mood} set{f' ({duration}m)' if duration > 0 else ' (infinite)'}")

        try:
            result = self.agent.run(
                f"You're starting a {mood} DJ set{f' for {duration} minutes' if duration > 0 else ''}. "
                f"Mixxx is running, both decks are empty. {n_tracks} tracks in: {', '.join(genres)}. "
                f"Music dir: {self.config.library.music_path}\n\n"
                f"Steps:\n"
                f"1. Use library agent to find a great opener for {mood}\n"
                f"2. Get the FULL FILE PATH\n"
                f"3. Tell mixer agent: 'Load [PATH] on deck 1, play, set crossfader to 0.0'\n"
                f"4. Verify with get_dj_status\n\nGo."
            )
            log.info(f"First track: {str(result)[:200]}")
            tinfo = get_track_info_api(self.config.mixxx.url, 1)
            if tinfo and not tinfo.get("error"):
                self.tracks_played.append({"title": tinfo.get("title", "?"), "time": time.time()})
        except Exception as e:
            log.error(f"Failed to start set: {e}")
            self.phase = "idle"

    def stop_set(self):
        """Stop the current set with a fade out."""
        if self.phase != "playing":
            return "Not playing"
        log.info("Stopping set...")
        try:
            self.agent.run("The set is over. Fade out gracefully over 30 seconds using the mixer agent.")
        except Exception:
            pass
        self.phase = "idle"
        return "Set stopped"

    def _check_transition(self):
        """Check if current track needs a transition."""
        # Check set duration
        if self.set_duration > 0:
            elapsed = time.time() - self.set_start
            if elapsed >= self.set_duration:
                log.info("Set duration reached")
                self.stop_set()
                return

        status = get_status(self.config.mixxx.url)
        if not status:
            return

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        active = d1 if d1.get("playing") else d2
        active_deck = 1 if d1.get("playing") else 2
        idle_deck = 2 if active_deck == 1 else 1
        active_remaining = active.get("remaining_seconds", 999)

        # Nothing playing? Something went wrong
        if not d1.get("playing") and not d2.get("playing"):
            if d1.get("track_loaded") or d2.get("track_loaded"):
                log.warning("Nothing playing! Force-starting...")
                deck = 1 if d1.get("track_loaded") else 2
                httpx.post(f"{self.config.mixxx.url}/api/play", json={"deck": deck}, timeout=3)
                xf = 0.0 if deck == 1 else 1.0
                httpx.post(f"{self.config.mixxx.url}/api/crossfade", json={"position": xf}, timeout=3)
            return

        if 0 < active_remaining < 120:
            log.info(f"Track has {active_remaining:.0f}s remaining — transitioning")

            played_list = [t.get("title", "?") for t in self.tracks_played]
            current_title = ""
            tinfo = get_track_info_api(self.config.mixxx.url, active_deck)
            if tinfo and not tinfo.get("error"):
                current_title = tinfo.get("title", "")

            set_remaining = ""
            if self.set_duration > 0:
                sr = self.set_duration - (time.time() - self.set_start)
                set_remaining = f"{sr:.0f}s left in set. "

            try:
                result = self.agent.run(
                    f"Time to transition! Deck {active_deck} has {active_remaining:.0f}s remaining.\n"
                    f"Current: {current_title}\n"
                    f"BPM: {active.get('bpm', '?')}, Key: {active.get('key', '?')}\n"
                    f"Mood: {self.mood}. {set_remaining}Idle deck: {idle_deck}\n\n"
                    f"ALREADY PLAYED (DO NOT REPEAT): {played_list}\n\n"
                    f"1. Library agent: find unplayed track for {self.mood}\n"
                    f"2. Mixer agent: load on deck {idle_deck} (FULL PATH), sync, do_transition(to_deck={idle_deck}, duration={min(60, int(active_remaining - 15))})\n"
                    f"3. Verify deck {idle_deck} is playing"
                )
                log.info(f"Transition: {str(result)[:200]}")

                # Record new track
                new_tinfo = get_track_info_api(self.config.mixxx.url, idle_deck)
                if new_tinfo and not new_tinfo.get("error"):
                    self.tracks_played.append({"title": new_tinfo.get("title", "?"), "time": time.time()})
            except Exception as e:
                log.error(f"Transition error: {e}")

            # Post-transition safety
            time.sleep(5)
            post = get_status(self.config.mixxx.url)
            if post and not post.get("deck1", {}).get("playing") and not post.get("deck2", {}).get("playing"):
                log.warning("Nothing playing after transition! Emergency start")
                httpx.post(f"{self.config.mixxx.url}/api/play", json={"deck": idle_deck}, timeout=3)
                xf = 0.0 if idle_deck == 1 else 1.0
                httpx.post(f"{self.config.mixxx.url}/api/crossfade", json={"position": xf}, timeout=3)

    def _check_commands(self):
        if not COMMAND_FILE.exists():
            return

        try:
            raw = json.loads(COMMAND_FILE.read_text())
            COMMAND_FILE.unlink()
        except Exception:
            return

        cmd = raw.get("command", "")
        args = raw.get("args", {})
        log.info(f"Command: {cmd}")

        self._last_command = cmd
        self._last_result = "processing..."
        self._write_state()

        try:
            result = self._handle_command(cmd, args)
        except Exception as e:
            result = f"Error: {e}"

        self._last_command = cmd
        self._last_result = result
        self._write_state()
        log.info(f"Result: {result[:200]}")

    def _handle_command(self, cmd: str, args: dict) -> str:
        if cmd == "talk":
            message = args.get("message", "")
            if not message:
                return "No message"
            status = get_status(self.config.mixxx.url)
            ctx = f"Phase: {self.phase}, Mood: {self.mood}, Tracks: {len(self.tracks_played)}"
            if status:
                ctx += f"\nMixxx: {json.dumps(status, indent=2)[:400]}"
            return fast_talk(message, self.config, ctx)

        elif cmd == "play":
            mood = args.get("mood", "melodic-techno")
            duration = args.get("duration", 0)
            # Run in thread so command returns quickly
            threading.Thread(target=self.play_set, args=(mood, duration), daemon=True).start()
            return f"Starting {mood} set{f' ({duration}m)' if duration > 0 else ' (infinite)'}"

        elif cmd == "stop":
            return self.stop_set()

        elif cmd == "change_mood":
            self.mood = args.get("mood", self.mood)
            return f"Mood changed to {self.mood}"

        elif cmd == "skip":
            return _quick_skip(self.config)

        elif cmd == "transition_now":
            return _quick_skip(self.config)

        elif cmd == "extend_set":
            extra = args.get("minutes", 30)
            self.set_duration += extra * 60
            return f"Extended by {extra}m"

        else:
            return f"Unknown: {cmd}"

    def _state_loop(self):
        save_counter = 0
        while self._running:
            self._write_state()
            save_counter += 1
            if save_counter % 5 == 0:  # persist to disk every 10s
                self._save_session()
            time.sleep(2)

    def _write_state(self):
        try:
            elapsed = time.time() - self.set_start if self.set_start > 0 else 0
            remaining = max(0, self.set_duration - elapsed) if self.set_duration > 0 else 0

            # Get current track from Mixxx
            current = {"title": "", "bpm": 0, "key": "", "remaining": 0}
            status = get_status(self.config.mixxx.url)
            if status:
                d1 = status.get("deck1", {})
                d2 = status.get("deck2", {})
                active_deck = 1 if d1.get("playing") else 2
                active = d1 if d1.get("playing") else d2
                tinfo = get_track_info_api(self.config.mixxx.url, active_deck)
                if tinfo and not tinfo.get("error"):
                    current["title"] = tinfo.get("title", "")
                current["bpm"] = active.get("bpm", 0)
                current["key"] = active.get("key", 0)
                current["remaining"] = active.get("remaining_seconds", 0)

            state = {
                "phase": self.phase,
                "mood": self.mood,
                "tracks_played": len(self.tracks_played),
                "set_elapsed": round(elapsed),
                "set_remaining": round(remaining) if self.set_duration > 0 else "infinite",
                "consecutive_errors": 0,
                "last_command": self._last_command,
                "last_command_result": self._last_result,
                "current_track": current,
                "next_track": None,
            }
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser(description="DJ Treta Being")
    parser.add_argument("--play", default=None, help="Start playing immediately with this mood")
    parser.add_argument("--duration", type=int, default=0, help="Set duration in minutes (0=infinite)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    being = DJTretaBeing(config)

    if args.play:
        # Start playing on a background thread, being stays alive
        threading.Thread(target=being.play_set, args=(args.play, args.duration), daemon=True).start()

    being.start()
