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
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

import httpx

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
PID_FILE = Path("/tmp/dj-treta.pid")
PERSIST_FILE = Path(__file__).parent.parent / ".beings" / "session.json"  # survives restarts


def _check_single_instance():
    """Ensure only one Being instance runs. Kill stale PIDs."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # signal 0 = check existence
            log.error(f"Another instance already running (PID {old_pid}). Kill it first or use the CLI.")
            sys.exit(1)
        except ProcessLookupError:
            PID_FILE.unlink()
        except ValueError:
            PID_FILE.unlink()

    PID_FILE.write_text(str(os.getpid()))


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
    r'|bass|eq|filter|volume|crossfade|sync'
    r'|hear|listen|sound|audio)\b',
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

def _quick_skip(config: Config, being=None) -> str:
    """Skip: find an unplayed track, load, sync, transition. Avoids current + played tracks."""
    try:
        status = get_status(config.mixxx.url)
        if not status:
            return "Mixxx not reachable"

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        idle = 2 if d1.get("playing") else 1

        # Build set of paths to skip (currently loaded + played)
        skip_paths = set()
        for deck_num in [1, 2]:
            tinfo = get_track_info_api(config.mixxx.url, deck_num)
            if tinfo and not tinfo.get("error"):
                fp = tinfo.get("file_path", "")
                if fp:
                    skip_paths.add(fp)

        # Also skip tracks from session history
        if being and being.tracks_played:
            session_file = PERSIST_FILE
            if session_file.exists():
                try:
                    session = json.loads(session_file.read_text())
                    for t in session.get("tracks_played", []):
                        title = t.get("title", "").lower()
                        # Match by title substring in filename
                        for genre_dir in config.library.music_path.iterdir():
                            if not genre_dir.is_dir():
                                continue
                            for f in genre_dir.iterdir():
                                if title and title in f.stem.lower():
                                    skip_paths.add(str(f))
                except Exception:
                    pass

        # Find first track NOT in skip set
        import random
        all_tracks = []
        for genre_dir in sorted(config.library.music_path.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.') or genre_dir.name == '_sets':
                continue
            for f in sorted(genre_dir.iterdir()):
                if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                    if str(f) not in skip_paths:
                        all_tracks.append(f)

        # Filter by mood/genre first
        if being and being.mood:
            mood_tracks = [t for t in all_tracks if being.mood in str(t.parent.name).lower()]
            if mood_tracks:
                all_tracks = mood_tracks

        if not all_tracks:
            # No unplayed tracks — ask agent to search and download
            if being and being.agent:
                log.info("Skip: no unplayed tracks, asking agent to find and download one...")
                try:
                    result = being.agent.run(
                        f"The library has no unplayed tracks for skipping. "
                        f"Current mood is {being.mood}. "
                        f"Use the library agent to search YouTube for a great {being.mood} track, "
                        f"download it, then tell me the file path."
                    )
                    # Check if something was downloaded
                    all_tracks = []
                    for genre_dir in sorted(config.library.music_path.iterdir()):
                        if not genre_dir.is_dir() or genre_dir.name.startswith('.') or genre_dir.name == '_sets':
                            continue
                        for f in sorted(genre_dir.iterdir()):
                            if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                                if str(f) not in skip_paths:
                                    all_tracks.append(f)
                except Exception as e:
                    log.error(f"Agent download failed: {e}")

            if not all_tracks:
                return "No tracks available — download failed"

        # Pick random to add variety
        track = random.choice(all_tracks)

        c = httpx.Client(base_url=config.mixxx.url, timeout=5)
        c.post("/api/load", json={"deck": idle, "track": str(track)})
        time.sleep(1.0)
        c.post("/api/sync", json={"deck": idle})
        c.post("/api/play", json={"deck": idle})
        time.sleep(0.3)
        c.post("/api/transition", json={"deck": idle, "duration": 20})
        c.close()

        log.info(f"Skip: transitioning to {track.stem} on Deck {idle}")

        # Record in being's played list
        if being:
            being.tracks_played.append({"title": track.stem, "time": time.time()})

        return f"Skipping — {track.stem}"
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
        self.planned_tracks: list[dict] = []
        self._last_command = ""
        self._last_result = ""
        self._transition_thread = None  # background thread for agent transitions
        self._talk_lock = threading.Lock()  # prevent concurrent agent.run calls

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
                "planned_tracks": self.planned_tracks,
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
        _check_single_instance()
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())
        signal.signal(signal.SIGINT, lambda s, f: self.stop())

        log.info("DJ Treta Being alive")

        # Create agent
        log.info("Creating DJ agent...")
        self.agent = create_dj_agent(self.config)

        # Try to restore previous session
        restored = self._restore_session()

        # Even without session, check if Mixxx has music playing
        if not restored:
            status = get_status(self.config.mixxx.url)
            if status:
                d1 = status.get("deck1", {})
                d2 = status.get("deck2", {})
                if d1.get("playing") or d2.get("playing"):
                    self.phase = "playing"
                    log.info("No saved session, but Mixxx has music playing — monitoring")

        # Start state writer
        self._state_thread = threading.Thread(target=self._state_loop, daemon=True)
        self._state_thread.start()

        # Start watchdog — catches silence even when agent.run blocks
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

        # Main loop — lightweight, never blocks
        while self._running:
            try:
                self._check_commands()

                if self.phase == "playing":
                    self._check_transition()

            except Exception as e:
                log.warning(f"Main loop error: {e}")

            time.sleep(3)

        log.info("DJ Treta Being shutting down")

    def stop(self):
        self._save_session()
        self._running = False
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

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
            return

        # Plan next tracks in background — failure here should NEVER kill the set
        threading.Thread(target=self._plan_ahead, daemon=True).start()

    def _plan_ahead(self):
        """Ask brain to plan the next 3 tracks — energy arc, key flow, mood journey."""
        if not self.agent:
            return
        played_list = [t.get("title", "?") for t in self.tracks_played]

        # Get current track info
        status = get_status(self.config.mixxx.url)
        current_bpm = 0
        current_key = ""
        if status:
            d1 = status.get("deck1", {})
            d2 = status.get("deck2", {})
            active = d1 if d1.get("playing") else d2
            current_bpm = active.get("bpm", 0)
            from .camelot import mixxx_key_to_musical, mixxx_key_to_camelot
            key_num = active.get("key", 0)
            musical = mixxx_key_to_musical(key_num) if key_num else ""
            camelot = mixxx_key_to_camelot(key_num) if key_num else ""
            current_key = f"{musical} ({camelot})" if musical else ""

        try:
            log.info("Planning next 3 tracks...")
            result = self.agent.run(
                f"Plan the next 3 tracks for this {self.mood} set.\n"
                f"Current BPM: {current_bpm:.0f}, Key: {current_key}\n"
                f"Already played: {played_list}\n\n"
                f"Use the library agent to browse available tracks.\n"
                f"For each planned track, tell me: title, why it fits, energy level (1-10).\n"
                f"Consider: BPM compatibility, key flow (Camelot), energy arc.\n\n"
                f"Return as:\n"
                f"NEXT 1: <track name> — <reason> (energy: X)\n"
                f"NEXT 2: <track name> — <reason> (energy: X)\n"
                f"NEXT 3: <track name> — <reason> (energy: X)"
            )
            result_str = str(result)
            log.info(f"Planned: {result_str[:300]}")

            # Parse planned tracks
            self.planned_tracks = []
            for line in result_str.split("\n"):
                line = line.strip()
                if line.startswith("NEXT"):
                    # Extract track name
                    parts = line.split(":", 1)
                    if len(parts) >= 2:
                        track_info = parts[1].strip()
                        name = track_info.split("—")[0].strip() if "—" in track_info else track_info.split("(")[0].strip()
                        reason = track_info.split("—")[1].strip() if "—" in track_info else ""
                        self.planned_tracks.append({"title": name, "reason": reason})

            if not self.planned_tracks:
                # Couldn't parse structured output — save raw
                self.planned_tracks = [{"title": result_str[:200], "reason": ""}]

        except Exception as e:
            log.warning(f"Planning failed: {e}")

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

    def _watchdog_loop(self):
        """Independent watchdog — catches silence even when agent blocks."""
        while self._running:
            try:
                if self.phase == "playing":
                    status = get_status(self.config.mixxx.url)
                    if status:
                        d1 = status.get("deck1", {})
                        d2 = status.get("deck2", {})
                        if not d1.get("playing") and not d2.get("playing"):
                            if d1.get("track_loaded"):
                                log.warning("WATCHDOG: silence! Starting Deck 1")
                                httpx.post(f"{self.config.mixxx.url}/api/play", json={"deck": 1}, timeout=3)
                                httpx.post(f"{self.config.mixxx.url}/api/crossfade", json={"position": 0.0}, timeout=3)
                            elif d2.get("track_loaded"):
                                log.warning("WATCHDOG: silence! Starting Deck 2")
                                httpx.post(f"{self.config.mixxx.url}/api/play", json={"deck": 2}, timeout=3)
                                httpx.post(f"{self.config.mixxx.url}/api/crossfade", json={"position": 1.0}, timeout=3)
                            else:
                                log.warning("WATCHDOG: silence and no tracks loaded!")
            except Exception:
                pass
            time.sleep(5)

    def _check_transition(self):
        """Check if transition needed — launches agent in background thread."""
        if self.set_duration > 0:
            elapsed = time.time() - self.set_start
            if elapsed >= self.set_duration:
                log.info("Set duration reached")
                self.stop_set()
                return

        # Don't check if a transition is already in progress
        if self._transition_thread and self._transition_thread.is_alive():
            return

        status = get_status(self.config.mixxx.url)
        if not status:
            return

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})

        if not d1.get("playing") and not d2.get("playing"):
            return  # watchdog handles silence

        active = d1 if d1.get("playing") else d2
        active_deck = 1 if d1.get("playing") else 2
        idle_deck = 2 if active_deck == 1 else 1
        active_remaining = active.get("remaining_seconds", 999)

        if 0 < active_remaining < 120:
            log.info(f"Track has {active_remaining:.0f}s remaining — launching transition")

            # Launch agent in background — main loop stays alive
            self._transition_thread = threading.Thread(
                target=self._do_transition,
                args=(active_deck, idle_deck, active_remaining),
                daemon=True,
            )
            self._transition_thread.start()

    def _do_transition(self, active_deck, idle_deck, active_remaining):
        """Run transition in background thread — agent does its thing."""
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
                f"BPM: {active_deck}, Key: ?\n"
                f"Mood: {self.mood}. {set_remaining}Idle deck: {idle_deck}\n\n"
                f"ALREADY PLAYED (DO NOT REPEAT): {played_list}\n\n"
                f"Steps:\n"
                f"1. hear_music(deck={active_deck}) — LISTEN to current track, feel its vibe and energy\n"
                f"2. Library agent: find unplayed track for {self.mood} that matches what you heard\n"
                f"3. Mixer agent: 'Load [FULL PATH] on deck {idle_deck}'\n"
                f"4. hear_music(deck={idle_deck}) — LISTEN to loaded track, compare with current\n"
                f"5. Based on what you heard, choose technique:\n"
                f"   - Similar energy/key → do_transition (smooth blend)\n"
                f"   - Different energy → do_bass_swap (EQ swap for clean handoff)\n"
                f"6. Mixer agent: execute do_transition or do_bass_swap(to_deck={idle_deck}, duration={min(45, int(active_remaining - 10))})\n"
                f"CRITICAL: You MUST call do_transition or do_bass_swap — or music stops."
            )
            log.info(f"Transition: {str(result)[:200]}")

            new_tinfo = get_track_info_api(self.config.mixxx.url, idle_deck)
            if new_tinfo and not new_tinfo.get("error"):
                self.tracks_played.append({"title": new_tinfo.get("title", "?"), "time": time.time()})

            threading.Thread(target=self._plan_ahead, daemon=True).start()
        except Exception as e:
            log.error(f"Transition error: {e}")

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

            if not self.agent:
                return "Brain not ready yet"

            # Check if this is a play request while idle
            if self.phase == "idle" and any(w in message.lower() for w in ["play", "start", "baja", "shuru", "bajao"]):
                mood = "deep"
                for m in ["melodic", "techno", "deep", "dark", "progressive", "ambient",
                          "chill", "vocal", "house", "psychill", "minimal", "bhojpuri",
                          "trance", "lofi", "bollywood"]:
                    if m in message.lower():
                        mood = m
                        break
                threading.Thread(target=self.play_set, args=(mood, 0), daemon=True).start()
                return f"Starting {mood} set — searching for tracks..."

            # Smart routing: 1 quick LLM call decides if tools needed
            def _talk_bg():
                try:
                    from litellm import completion
                    context = (
                        f"Phase: {self.phase}, Mood: {self.mood or 'none'}, "
                        f"Tracks played: {len(self.tracks_played)}"
                    )

                    # Quick classify: does this need tools?
                    classify = completion(
                        model=self.config.llm.model,
                        messages=[{"role": "user", "content":
                            f'Does this message need DJ tools (load track, change BPM, EQ, filter, skip, download, etc.) '
                            f'or is it just conversation? Answer ONLY "tools" or "chat".\n'
                            f'Message: "{message}"'}],
                        api_base=self.config.llm.api_base,
                        api_key=self.config.llm.api_key,
                        temperature=0, timeout=10,
                    )
                    needs_tools = "tool" in classify.choices[0].message.content.lower()

                    if needs_tools:
                        # Quick acknowledgment first — user sees this immediately
                        ack = completion(
                            model=self.config.llm.model,
                            messages=[
                                {"role": "system", "content": "You are DJ Treta. The listener asked you to do something. Give a SHORT one-line acknowledgment of what you're about to do. Be warm, natural."},
                                {"role": "user", "content": f'"{message}"'},
                            ],
                            api_base=self.config.llm.api_base,
                            api_key=self.config.llm.api_key,
                            temperature=0.7, timeout=10,
                        )
                        ack_text = ack.choices[0].message.content.strip()
                        self._last_result = f"{ack_text} ..."
                        self._write_state()
                        log.info(f"Talk ack: {ack_text}")

                        # Now do the actual work
                        with self._talk_lock:
                            result = str(self.agent.run(
                                f'{context}\n\nThe listener says: "{message}"\n\n'
                                f'Take action using your tools, then respond briefly with what you did.'
                            ))
                        result = f"{ack_text}\n\n{result}"
                    else:
                        # Fast chat — single LLM call (~3s)
                        resp = completion(
                            model=self.config.llm.model,
                            messages=[
                                {"role": "system", "content": "You are DJ Treta, an AI DJ Being. Be brief, direct, warm. 1-3 sentences."},
                                {"role": "user", "content": f'{context}\n\n"{message}"'},
                            ],
                            api_base=self.config.llm.api_base,
                            api_key=self.config.llm.api_key,
                            temperature=0.7, timeout=15,
                        )
                        result = resp.choices[0].message.content.strip()

                    self._last_command = cmd
                    self._last_result = result
                    self._write_state()
                    log.info(f"Talk result ({'tools' if needs_tools else 'chat'}): {result[:200]}")
                except Exception as e:
                    self._last_result = f"Error: {e}"
                    self._write_state()

            threading.Thread(target=_talk_bg, daemon=True).start()
            return "processing..."  # immediate return, result comes async

        elif cmd == "play":
            mood = args.get("mood", "melodic-techno")
            duration = args.get("duration", 0)
            threading.Thread(target=self.play_set, args=(mood, duration), daemon=True).start()
            return f"Starting {mood} set{f' ({duration}m)' if duration > 0 else ' (infinite)'}"

        elif cmd == "stop":
            return self.stop_set()

        elif cmd == "change_mood":
            self.mood = args.get("mood", self.mood)
            return f"Mood changed to {self.mood}"

        elif cmd == "skip":
            return _quick_skip(self.config, self)

        elif cmd == "transition_now":
            return _quick_skip(self.config, self)

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
                "planned_tracks": self.planned_tracks,
            }
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser(description="DJ Treta Being — always alive daemon")
    parser.add_argument("--play", default=None, help="Optional: start playing immediately with this mood")
    parser.add_argument("--duration", type=int, default=0, help="Set duration in minutes (0=infinite)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    being = DJTretaBeing(config)

    if args.play:
        threading.Thread(target=being.play_set, args=(args.play, args.duration), daemon=True).start()

    being.start()
    # Default: starts idle, waiting for talk commands
    # "djtreta talk 'play something melodic'" → she starts playing
