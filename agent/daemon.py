"""DJ Treta Daemon — Main loop and state machine.

STARTING → PLAYING → PREPARING → TRANSITIONING → PLAYING (loop)
                                                     ↓
                                                  RECOVERY (on errors)
                                                     ↓
                                                  STOPPED (via signal)

Run: python -m agent.daemon --mood techno-deep --duration 60
"""

import json
import signal
import sys
import time
import logging
from pathlib import Path

import httpx

from .config import load_config, Config
from .state import DJState, DJPhase, TrackState
from .brain import DJBrain
from .executor import TransitionExecutor
from .perception import PerceptionEngine
from .selector import scan_library, filter_candidates, suggest_technique
from .camelot import mixxx_key_to_musical, mixxx_key_to_camelot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dj-treta")

STATE_FILE = Path("/tmp/dj-treta-state.json")
COMMAND_FILE = Path("/tmp/dj-treta-command.json")


class DJDaemon:
    """The autonomous DJ daemon."""

    def __init__(self, config: Config, mood: str = "techno-deep", duration_min: int = 60):
        self.config = config
        self.state = DJState(
            mood=mood,
            set_duration_target=duration_min * 60,
        )
        self.brain = DJBrain(config)
        self.executor = TransitionExecutor(config)
        self.perception = PerceptionEngine(config)
        self._running = False
        self._preparing = False  # flag to prevent duplicate brain calls
        self._library: list[dict] = []

    def start(self):
        """Start the daemon loop."""
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

        log.info(f"DJ Treta starting — mood: {self.state.mood}, duration: {self.state.set_duration_target // 60}m")

        # Scan library
        self._library = scan_library(self.config.library.music_path)
        log.info(f"Library: {len(self._library)} tracks")

        if not self._library:
            log.error("No tracks in library! Add tracks to ~/Music/DJTreta/")
            return

        self.state.phase = DJPhase.STARTING
        self.state.set_start_time = time.time()
        self._write_state()

        # Clean Mixxx state from previous sessions
        self._clean_mixxx()

        try:
            self._start_first_track()
            self._loop()
        except Exception as e:
            log.error(f"Fatal error: {e}")
        finally:
            self._shutdown()

    def _loop(self):
        """Main daemon loop — runs at poll_hz."""
        interval = 1.0 / self.config.daemon.poll_hz

        while self._running:
            try:
                self._tick()
                self.state.consecutive_errors = 0
            except Exception as e:
                self.state.consecutive_errors += 1
                log.warning(f"Tick error ({self.state.consecutive_errors}): {e}")

                if self.state.consecutive_errors >= self.config.daemon.max_errors:
                    log.error("Too many errors, entering recovery")
                    self.state.phase = DJPhase.RECOVERY
                    self._recover()

            self._write_state()

            # Check if set duration reached
            if self.state.set_remaining <= 0:
                log.info("Set duration reached, fading out")
                self._fade_out()
                break

            time.sleep(interval)

    def _check_commands(self):
        """Check for external commands from MCP via command file."""
        if not COMMAND_FILE.exists():
            return

        try:
            raw = json.loads(COMMAND_FILE.read_text())
            COMMAND_FILE.unlink()  # consume it
        except Exception:
            return

        cmd = raw.get("command", "")
        args = raw.get("args", {})
        log.info(f"Command received: {cmd} {args}")

        # Write acknowledgment
        self.state.last_command = cmd
        self.state.last_command_result = "processing..."
        self._write_state()

        try:
            result = self._handle_command(cmd, args)
            self.state.last_command_result = result
            log.info(f"Command result: {result}")
        except Exception as e:
            self.state.last_command_result = f"error: {e}"
            log.error(f"Command error: {e}")

        self._write_state()

    def _handle_command(self, cmd: str, args: dict) -> str:
        """Handle a single command."""
        if cmd == "change_mood":
            new_mood = args.get("mood", self.state.mood)
            self.state.mood = new_mood
            return f"Mood changed to {new_mood}. Next track will match."

        elif cmd == "skip":
            # Fast skip — no brain call, just emergency load + hard cut
            self._emergency_next()
            return "Skipped to next track"

        elif cmd == "talk":
            # Two-way conversation with the brain
            message = args.get("message", "")
            if not message:
                return "No message provided"
            response = self.brain.talk(message, self._build_context())
            return response

        elif cmd == "transition_now":
            # Ask brain to handle the full transition
            self._prepare_and_transition()
            return "Transition complete"

        elif cmd == "extend_set":
            extra = args.get("minutes", 30)
            self.state.set_duration_target += extra * 60
            return f"Set extended by {extra}m. New remaining: {self.state.set_remaining // 60:.0f}m"

        elif cmd == "stop":
            self._fade_out()
            return "Fading out and stopping"

        else:
            return f"Unknown command: {cmd}"

    def _build_context(self) -> dict:
        """Build context dict for brain calls."""
        self._enrich_current_track()
        return {
            "mood": self.state.mood,
            "current_bpm": self.state.current_track.bpm,
            "current_key": self.state.current_track.key,
            "current_energy": self.state.current_track.energy,
            "current_remaining": self.state.current_track.remaining,
            "target_energy": "maintain",
            "tracks_played": [t["title"] for t in self.state.tracks_played],
            "idle_deck": self.state.idle_deck,
            "set_elapsed": self.state.set_elapsed,
            "set_remaining": self.state.set_remaining,
        }

    # ── Safety constants ──
    MIN_REMAINING_TO_PREPARE = 45   # don't prepare if <45s left, go emergency
    MIN_TRANSITION_DURATION = 10    # never transition shorter than 10s
    MAX_TRANSITION_DURATION = 120   # never longer than 120s
    TRANSITION_BUFFER = 15          # start transition when remaining < duration + buffer

    def _tick(self):
        """Single daemon tick — monitor and act.

        Simplified: no PREPARING/TRANSITIONING phases. Brain handles everything.
        Daemon just monitors remaining time and calls brain when it's time to transition.
        """
        # Check for external commands first
        self._check_commands()

        perc = self.perception.poll()
        if not perc:
            raise ConnectionError("Mixxx not reachable")

        # Update current track remaining
        active = perc.active
        self.state.current_track.remaining = active.remaining_seconds

        # Safety: if crossfader points at empty deck, fix it
        if self.state.phase == DJPhase.PLAYING:
            self._ensure_crossfader_on_active(perc)

        if self.state.phase == DJPhase.PLAYING:
            remaining = active.remaining_seconds

            if remaining <= 0 or not active.playing:
                log.warning("Track ended unexpectedly!")
                self._emergency_next()

            elif remaining <= self.MIN_REMAINING_TO_PREPARE:
                log.warning(f"Only {remaining:.0f}s left, too late for brain — emergency")
                self._emergency_next()

            elif remaining <= self.config.transitions.lookahead_seconds:
                if not self._preparing:
                    # Ask brain to pick next track AND handle the transition
                    self._preparing = True
                    log.info(f"Track ending in {remaining:.0f}s — asking brain to prepare and transition")
                    self._prepare_and_transition()

    def _ensure_crossfader_on_active(self, perc):
        """Safety: if crossfader is pointing at an empty/silent deck, fix it.

        Note: perc.crossfader is Mixxx raw (-1 to +1).
        /api/crossfade expects 0-1 (0=deck1, 1=deck2).
        """
        active_deck = self.state.active_deck
        idle_deck = self.state.idle_deck

        active_playing = perc.deck1.playing if active_deck == 1 else perc.deck2.playing
        idle_playing = perc.deck1.playing if idle_deck == 1 else perc.deck2.playing

        if active_playing and not idle_playing:
            # Active deck has audio, idle doesn't — crossfader must be on active
            current_xf = perc.crossfader  # Mixxx raw: -1 (deck1) to +1 (deck2)
            if active_deck == 1 and current_xf > 0.2:
                log.warning(f"Crossfader at {current_xf:.2f} but Deck 2 empty — fixing to Deck 1")
                self._set_crossfader(0.0)  # API: 0.0 = deck1
            elif active_deck == 2 and current_xf < -0.2:
                log.warning(f"Crossfader at {current_xf:.2f} but Deck 1 empty — fixing to Deck 2")
                self._set_crossfader(1.0)  # API: 1.0 = deck2

    def _set_crossfader(self, position: float):
        """Set crossfader position safely. API expects 0.0 (deck1) to 1.0 (deck2)."""
        try:
            client = httpx.Client(base_url=self.config.mixxx.url, timeout=2)
            client.post("/api/crossfade", json={"position": position})
            client.close()
        except Exception:
            pass

    def _clean_mixxx(self):
        """Reset Mixxx to clean state — eject old tracks, reset mixer."""
        log.info("Cleaning Mixxx state...")
        try:
            client = httpx.Client(base_url=self.config.mixxx.url, timeout=5)
            for deck in [1, 2]:
                client.post("/api/pause", json={"deck": deck})
                client.post("/api/eject", json={"deck": deck})
                client.post("/api/volume", json={"deck": deck, "level": 1.0})
                for band in ["hi", "mid", "lo"]:
                    client.post("/api/eq", json={"deck": deck, band: 1.0})
                client.post("/api/filter", json={"deck": deck, "value": 0.5})
            client.post("/api/crossfade", json={"position": 0.5})  # center
            client.close()
            log.info("Mixxx cleaned — both decks ejected, mixer reset")
        except Exception as e:
            log.warning(f"Could not clean Mixxx (may not be running yet): {e}")

    def _start_first_track(self):
        """Load and play the first track."""
        log.info("Asking brain to pick first track...")

        context = {
            "mood": self.state.mood,
            "current_bpm": 0,
            "current_key": "",
            "current_energy": 5,
            "target_energy": 5,
            "tracks_played": [],
            "idle_deck": 1,
            "set_elapsed": 0,
            "set_remaining": self.state.set_duration_target,
        }

        try:
            decision = self.brain.decide_next_track(context)
            track_path = decision.get("track_path", "")

            if not track_path or not Path(track_path).exists():
                # Fallback: pick first track from library
                track_path = self._library[0]["path"]
                log.warning(f"Brain didn't return valid path, using: {Path(track_path).name}")

            self.state.current_track = TrackState(
                path=track_path,
                title=Path(track_path).stem,
                energy=decision.get("energy", 5),
            )
            # Ensure track is loaded and playing (brain may not always do this)
            self._ensure_playing(1, track_path)
            self.state.record_track(self.state.current_track)
            self.state.phase = DJPhase.PLAYING
            log.info(f"Playing: {self.state.current_track.title}")

        except Exception as e:
            log.error(f"Brain failed on first track: {e}")
            # Fallback
            track_path = self._library[0]["path"]
            self.state.current_track = TrackState(path=track_path, title=Path(track_path).stem)
            self._ensure_playing(1, track_path)
            self.state.record_track(self.state.current_track)
            self.state.phase = DJPhase.PLAYING
            log.info(f"Fallback first track: {self.state.current_track.title}")

    def _ensure_playing(self, deck: int, track_path: str):
        """Ensure a track is loaded on deck and playing."""
        client = httpx.Client(base_url=self.config.mixxx.url, timeout=5)
        try:
            client.post("/api/load", json={"deck": deck, "track": track_path})
            time.sleep(0.5)  # let Mixxx process the load
            client.post("/api/play", json={"deck": deck})
            # Set crossfader to this deck (API: 0.0=deck1, 1.0=deck2)
            xf = 0.0 if deck == 1 else 1.0
            client.post("/api/crossfade", json={"position": xf})
        except Exception as e:
            log.warning(f"_ensure_playing error: {e}")
        finally:
            client.close()

    def _prepare_and_transition(self):
        """Ask brain to pick next track, load it, and transition — brain does everything."""
        self._enrich_current_track()

        context = self._build_context()
        remaining = self.state.current_track.remaining
        max_duration = max(30, int(remaining - self.TRANSITION_BUFFER))
        idle_deck = self.state.idle_deck

        prompt = f"""Time to transition. Pick the next track, load it on deck {idle_deck},
enable sync, and execute the transition.

Current state:
- Mood: {context.get('mood')}
- Current BPM: {context.get('current_bpm')}
- Current key: {context.get('current_key')}
- Current energy: {context.get('current_energy')}
- Remaining on current track: {remaining:.0f}s
- Tracks played: {context.get('tracks_played')}
- Idle deck: {idle_deck}

Steps:
1. Use list_library_tracks to find tracks (pick from {context.get('mood')} genre or compatible)
2. Use load_track to load onto deck {idle_deck}
3. Use set_sync on deck {idle_deck}
4. Use do_transition or do_bass_swap (deck={idle_deck}, duration=30-{max_duration}s)

Do NOT pick a track already played. Consider BPM (±6) and key compatibility."""

        try:
            log.info("Brain is selecting and transitioning...")
            result = str(self.brain.agent.run(prompt))
            log.info(f"Brain transition result: {result[:200]}")

            # After brain finishes, update state
            # Check which deck is now active (the one that's playing and has crossfader)
            perc = self.perception.poll()
            if perc:
                if perc.deck1.playing and not perc.deck2.playing:
                    new_active = 1
                elif perc.deck2.playing and not perc.deck1.playing:
                    new_active = 2
                elif idle_deck == 1:
                    new_active = 1
                else:
                    new_active = 2

                # Update deck tracking
                self.state.active_deck = new_active
                self.state.idle_deck = 1 if new_active == 2 else 2

                # Read new track info
                status = httpx.get(f"{self.config.mixxx.url}/api/status", timeout=3).json()
                deck_info = status.get(f"deck{new_active}", {})
                self.state.current_track = TrackState(
                    bpm=deck_info.get("bpm", 0),
                    duration=deck_info.get("duration", 0),
                    remaining=deck_info.get("remaining_seconds", 0),
                )
                # Try to get title from track_info
                try:
                    tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{new_active}/track_info", timeout=3).json()
                    self.state.current_track.title = tinfo.get("title", "") or tinfo.get("artist", "") or f"Deck {new_active}"
                    self.state.current_track.artist = tinfo.get("artist", "")
                except Exception:
                    self.state.current_track.title = f"Deck {new_active} track"

                key_num = deck_info.get("key", 0)
                if key_num > 0:
                    musical = mixxx_key_to_musical(key_num)
                    if musical:
                        camelot = mixxx_key_to_camelot(key_num)
                        self.state.current_track.key = f"{musical} ({camelot})" if camelot else musical

                self.state.record_track(self.state.current_track)
                log.info(f"Now playing: {self.state.current_track.title} on Deck {new_active}")

        except Exception as e:
            log.error(f"Brain transition failed: {e}")
            self._emergency_next()
        finally:
            self._preparing = False
            self.state.next_track = None

    def _enrich_current_track(self):
        """Read BPM/key from Mixxx for the current track."""
        try:
            client = httpx.Client(base_url=self.config.mixxx.url, timeout=3)
            status = client.get("/api/status").json()
            deck = status.get(f"deck{self.state.active_deck}", {})
            self.state.current_track.bpm = deck.get("bpm", 0)
            key_num = deck.get("key", 0)
            if key_num > 0:
                musical = mixxx_key_to_musical(key_num)
                if musical:
                    camelot = mixxx_key_to_camelot(key_num)
                    self.state.current_track.key = f"{musical} ({camelot})" if camelot else musical
            self.state.current_track.duration = deck.get("duration", 0)
            client.close()
        except Exception as e:
            log.warning(f"Could not enrich track info: {e}")

    def _load_and_sync(self, track_path: str):
        """Load a track on the idle deck and enable beat sync."""
        deck = self.state.idle_deck
        client = httpx.Client(base_url=self.config.mixxx.url, timeout=5)
        try:
            # Load the track
            client.post("/api/load", json={"deck": deck, "track": track_path})
            time.sleep(1.0)  # give Mixxx time to analyze BPM

            # Enable sync so BPM matches the playing deck
            client.post("/api/sync", json={"deck": deck})
            log.info(f"Loaded + synced on Deck {deck}")

            # Read back what Mixxx detected
            status = client.get("/api/status").json()
            deck_info = status.get(f"deck{deck}", {})
            if self.state.next_track:
                self.state.next_track.bpm = deck_info.get("bpm", 0)
                key_num = deck_info.get("key", 0)
                if key_num > 0:
                    musical = mixxx_key_to_musical(key_num)
                    if musical:
                        camelot = mixxx_key_to_camelot(key_num)
                        self.state.next_track.key = f"{musical} ({camelot})" if camelot else musical
                log.info(f"Deck {deck}: {self.state.next_track.bpm} BPM, key {self.state.next_track.key}")

        except Exception as e:
            log.warning(f"_load_and_sync error: {e}")
        finally:
            client.close()

    def _emergency_next(self):
        """Emergency: load any unplayed track, sync, and hard cut. No brain involved."""
        played_paths = {self.state.current_track.path}
        for t in self.state.tracks_played:
            played_paths.add(t.get("path", ""))

        idle = self.state.idle_deck
        for t in self._library:
            if t["path"] not in played_paths:
                log.warning(f"Emergency: loading {Path(t['path']).stem} on Deck {idle}")
                try:
                    client = httpx.Client(base_url=self.config.mixxx.url, timeout=5)
                    client.post("/api/load", json={"deck": idle, "track": t["path"]})
                    time.sleep(1.0)
                    client.post("/api/sync", json={"deck": idle})
                    client.post("/api/play", json={"deck": idle})
                    time.sleep(0.3)
                    # Hard cut: crossfader to new deck, silence old
                    xf = 0.0 if idle == 1 else 1.0
                    client.post("/api/crossfade", json={"position": xf})
                    old_deck = 1 if idle == 2 else 2
                    client.post("/api/pause", json={"deck": old_deck})
                    client.close()
                except Exception as e:
                    log.error(f"Emergency load failed: {e}")
                    return

                # Update state
                self.state.swap_decks()
                self.state.current_track = TrackState(
                    path=t["path"],
                    title=Path(t["path"]).stem,
                )
                self.state.record_track(self.state.current_track)
                self._preparing = False
                log.info(f"Emergency: now playing {self.state.current_track.title}")
                return

        log.warning("All tracks played — looping current track")

    def _recover(self):
        """Recovery mode — try to stabilize or restart Mixxx."""
        log.info("Recovery: checking Mixxx state...")
        perc = self.perception.poll()

        if perc and (perc.deck1.playing or perc.deck2.playing):
            log.info("Music still playing, resuming")
            self.state.consecutive_errors = 0
            self.state.phase = DJPhase.PLAYING
            return

        # Mixxx is down — try to restart it
        log.warning("Mixxx is down. Attempting restart...")
        self.state.last_command_result = "Mixxx crashed — restarting..."
        self._write_state()

        if self._restart_mixxx():
            log.info("Mixxx restarted. Reloading last track...")
            self.state.consecutive_errors = 0
            # Reload the track that was playing
            if self.state.current_track.path:
                self._ensure_playing(self.state.active_deck, self.state.current_track.path)
            self.state.phase = DJPhase.PLAYING
            self.state.last_command_result = "Mixxx recovered — music resumed"
        else:
            # Second attempt failed
            log.error("Could not restart Mixxx. Waiting...")
            # Don't stop — keep trying every recovery cycle
            time.sleep(5)
            self.state.consecutive_errors = 0  # reset to allow more recovery attempts

    def _restart_mixxx(self) -> bool:
        """Attempt to restart Mixxx. Returns True if successful."""
        import subprocess

        mixxx_bin = Path.home() / "workspace" / "mixxx-treta" / "build" / "mixxx"
        res_path = Path.home() / "workspace" / "mixxx-treta" / "res"
        settings_path = Path.home() / "Library" / "Application Support" / "Mixxx"

        if not mixxx_bin.exists():
            log.error(f"Mixxx binary not found at {mixxx_bin}")
            return False

        try:
            subprocess.Popen(
                [str(mixxx_bin),
                 "--resourcePath", str(res_path),
                 "--settingsPath", str(settings_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("Mixxx process launched, waiting for API...")

            # Wait for API to come up (max 15 seconds)
            for i in range(30):
                time.sleep(0.5)
                perc = self.perception.poll()
                if perc is not None:
                    log.info(f"Mixxx API responding after {(i+1)*0.5:.1f}s")
                    return True

            log.error("Mixxx started but API not responding after 15s")
            return False

        except Exception as e:
            log.error(f"Failed to launch Mixxx: {e}")
            return False

    def _fade_out(self):
        """Graceful end of set — fade out over 30 seconds."""
        log.info("Fading out...")
        import httpx
        client = httpx.Client(base_url=self.config.mixxx.url, timeout=5)
        try:
            for i in range(60):  # 30 seconds at 2fps
                vol = 1.0 - (i / 60)
                client.post("/api/volume", json={"deck": self.state.active_deck, "volume": round(vol, 3)})
                time.sleep(0.5)
            client.post("/api/pause", json={"deck": self.state.active_deck})
        except Exception:
            pass
        finally:
            client.close()

        self.state.phase = DJPhase.STOPPED
        self._running = False

    def _shutdown(self):
        """Clean shutdown."""
        log.info(f"Shutting down. Played {len(self.state.tracks_played)} tracks in {self.state.set_elapsed:.0f}s")
        self._write_state()
        self.perception.close()
        self.executor.close()

    def _handle_stop(self, signum, frame):
        log.info("Stop signal received")
        self._running = False

    def _write_state(self):
        """Write current state to JSON for MCP to read."""
        try:
            STATE_FILE.write_text(json.dumps(self.state.to_dict(), indent=2))
        except Exception:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DJ Treta Daemon")
    parser.add_argument("--mood", default="techno-deep", help="Set mood")
    parser.add_argument("--duration", type=int, default=60, help="Set duration in minutes")
    parser.add_argument("--config", default=None, help="Config file path")
    args = parser.parse_args()

    config = load_config(args.config)
    daemon = DJDaemon(config, mood=args.mood, duration_min=args.duration)
    daemon.start()


if __name__ == "__main__":
    main()
