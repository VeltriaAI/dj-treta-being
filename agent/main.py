"""DJ Treta v2 — Agent-first main loop.

No state machine. No deterministic scheduling.
Just agents with tools, called in cycles.

Usage:
    python -m agent --mood melodic-techno --duration 60
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
from .agents import create_dj_agent, create_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dj-treta")

STATE_FILE = Path("/tmp/dj-treta-state.json")
COMMAND_FILE = Path("/tmp/dj-treta-command.json")


# ── Mixxx helpers ─────────────────────────────────────────────────────

def clean_mixxx(url: str):
    """Reset Mixxx to clean state."""
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
        log.warning(f"Clean failed (Mixxx may not be running): {e}")


def get_status(url: str) -> dict | None:
    try:
        return httpx.get(f"{url}/api/status", timeout=2).json()
    except Exception:
        return None


def get_track_info(url: str, deck: int) -> dict | None:
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


# ── State writer (background thread) ─────────────────────────────────

class StateWriter:
    """Polls Mixxx and writes state for TUI/MCP."""

    def __init__(self, url: str, mood: str, start_time: float, duration: int):
        self.url = url
        self.mood = mood
        self.start_time = start_time
        self.duration = duration
        self.tracks_played: list[dict] = []
        self.phase = "starting"
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._last_command = ""
        self._last_result = ""

    def start(self):
        self._thread.start()

    def stop(self):
        self._running = False

    def record_track(self, title: str):
        self.tracks_played.append({"title": title, "played_at": time.time()})

    def _loop(self):
        while self._running:
            try:
                status = get_status(self.url)
                elapsed = time.time() - self.start_time
                remaining = max(0, self.duration - elapsed)

                state = {
                    "phase": self.phase,
                    "mood": self.mood,
                    "tracks_played": len(self.tracks_played),
                    "set_elapsed": round(elapsed),
                    "set_remaining": round(remaining),
                    "consecutive_errors": 0,
                    "last_command": self._last_command,
                    "last_command_result": self._last_result,
                    "current_track": {"title": "", "bpm": 0, "key": "", "remaining": 0},
                    "next_track": None,
                }

                if status:
                    # Find active deck
                    d1 = status.get("deck1", {})
                    d2 = status.get("deck2", {})
                    xf = status.get("crossfader", 0)

                    if d1.get("playing") and not d2.get("playing"):
                        active = d1
                        active_num = 1
                    elif d2.get("playing") and not d1.get("playing"):
                        active = d2
                        active_num = 2
                    elif xf < -0.3:
                        active = d1
                        active_num = 1
                    else:
                        active = d2
                        active_num = 2

                    # Get track name
                    title = ""
                    tinfo = get_track_info(self.url, active_num)
                    if tinfo and not tinfo.get("error"):
                        title = tinfo.get("title", "")

                    state["current_track"] = {
                        "title": title,
                        "bpm": active.get("bpm", 0),
                        "key": active.get("key", 0),
                        "remaining": active.get("remaining_seconds", 0),
                    }

                STATE_FILE.write_text(json.dumps(state, indent=2))
            except Exception:
                pass
            time.sleep(2)


# ── Talk fast-path ────────────────────────────────────────────────────

_ACTION_PATTERN = re.compile(
    r'\b(play|load|skip|transition|mix|blend|swap|download|search|find'
    r'|change|switch|darker|lighter|harder|softer|build|drop|cut'
    r'|bass|eq|filter|volume|crossfade|sync)\b',
    re.IGNORECASE,
)

def fast_talk(message: str, config: Config, context: str) -> str:
    """Fast conversation — direct LLM call, no tools. ~3s."""
    try:
        resp = completion(
            model=config.llm.model,
            messages=[
                {"role": "system", "content": "You are DJ Treta, an AI DJ Being. Be brief, direct, warm. 1-3 sentences."},
                {"role": "user", "content": f'Treta (Claude) says: "{message}"\n\n{context}\n\nRespond naturally.'},
            ],
            api_base=config.llm.api_base,
            api_key=config.llm.api_key,
            temperature=config.llm.temperature,
            timeout=config.llm.timeout,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"(error: {e})"


# ── Command handler ───────────────────────────────────────────────────

def check_commands(agent, config: Config, state_writer: StateWriter) -> str | None:
    """Check for external commands. Returns response or None."""
    if not COMMAND_FILE.exists():
        return None

    try:
        raw = json.loads(COMMAND_FILE.read_text())
        COMMAND_FILE.unlink()
    except Exception:
        return None

    cmd = raw.get("command", "")
    args = raw.get("args", {})
    log.info(f"Command: {cmd} {args}")

    state_writer._last_command = cmd
    state_writer._last_result = "processing..."

    try:
        if cmd == "talk":
            message = args.get("message", "")
            if not message:
                result = "No message"
            elif _ACTION_PATTERN.search(message):
                # Needs tools — run through agent
                result = str(agent.run(f'The listener says: "{message}". Respond and take action if needed.'))
            else:
                # Fast path — direct LLM
                status = get_status(config.mixxx.url)
                ctx = f"Status: {json.dumps(status, indent=2)[:500]}" if status else ""
                result = fast_talk(message, config, ctx)

        elif cmd == "change_mood":
            new_mood = args.get("mood", "melodic-techno")
            state_writer.mood = new_mood
            result = f"Mood changed to {new_mood}"

        elif cmd == "skip":
            result = str(agent.run(
                "The listener wants to skip this track NOW. "
                "Use the library agent to find the next track, then use the mixer agent to load it on the idle deck, "
                "sync it, and do a quick 20-second transition."
            ))

        elif cmd == "transition_now":
            result = str(agent.run("Start transitioning to the next track now."))

        elif cmd == "stop":
            result = "Stopping"
            state_writer.phase = "stopped"

        else:
            result = f"Unknown command: {cmd}"

    except Exception as e:
        result = f"Error: {e}"

    state_writer._last_command = cmd
    state_writer._last_result = result
    log.info(f"Command result: {result[:200]}")
    return result


# ── Main ──────────────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser(description="DJ Treta v2")
    parser.add_argument("--mood", default="melodic-techno")
    parser.add_argument("--duration", type=int, default=60, help="Set duration in minutes")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    mood = args.mood
    duration_sec = args.duration * 60
    start_time = time.time()

    # Setup
    _running = True
    def handle_signal(sig, frame):
        nonlocal _running
        log.info("Stop signal")
        _running = False
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info(f"DJ Treta v2 starting — mood: {mood}, duration: {args.duration}m")

    # Clean Mixxx
    clean_mixxx(config.mixxx.url)

    # Count library
    n_tracks = count_tracks(config.library.music_path)
    genres = scan_genres(config.library.music_path)
    log.info(f"Library: {n_tracks} tracks across {', '.join(genres)}")

    if n_tracks == 0:
        log.error("No tracks! Add music to ~/Music/DJTreta/")
        return

    # Create agent
    log.info("Creating DJ agent...")
    agent = create_dj_agent(config)

    # Start state writer
    sw = StateWriter(config.mixxx.url, mood, start_time, duration_sec)
    sw.phase = "playing"
    sw.start()

    # ── DJ Loop ──
    log.info("Starting set...")

    # First track
    try:
        result = agent.run(
            f"You're starting a {mood} DJ set for {args.duration} minutes. "
            f"Mixxx is running on port 7778, both decks are empty. {n_tracks} tracks available in genres: {', '.join(genres)}. "
            f"Music dir: {config.library.music_path}\n\n"
            f"Steps:\n"
            f"1. Use library agent to list tracks and find a great opener for {mood}\n"
            f"2. IMPORTANT: Get the FULL FILE PATH from the library agent result\n"
            f"3. Tell mixer agent: 'Load [FULL PATH] on deck 1, play it, and set crossfader to 0.0 (deck 1)'\n"
            f"4. Confirm it's playing with get_dj_status\n\n"
            f"Go."
        )
        log.info(f"First track: {str(result)[:200]}")
    except Exception as e:
        log.error(f"Agent failed on first track: {e}")
        return

    # Main loop — each cycle checks if transition needed + handles commands
    while _running:
        elapsed = time.time() - start_time
        remaining = duration_sec - elapsed

        if remaining <= 0:
            log.info("Set duration reached")
            try:
                agent.run("The set is over. Fade out the current track gracefully over 30 seconds.")
            except Exception:
                pass
            break

        # Handle external commands
        check_commands(agent, config, sw)

        # Check if transition needed
        try:
            status = get_status(config.mixxx.url)
            if status:
                d1 = status.get("deck1", {})
                d2 = status.get("deck2", {})

                # Find active deck
                active = d1 if d1.get("playing") else d2
                active_remaining = active.get("remaining_seconds", 999)

                if 0 < active_remaining < 120:
                    log.info(f"Track has {active_remaining:.0f}s remaining — asking agent to transition")
                    try:
                        result = agent.run(
                            f"The current track has {active_remaining:.0f}s remaining. "
                            f"Set mood is {sw.mood}. {remaining:.0f}s left in the set. "
                            f"Use the library agent to pick the next track (consider BPM, key, energy), "
                            f"then use the mixer agent to load it, sync, and transition. "
                            f"Choose transition duration based on remaining time (max {int(active_remaining - 15)}s)."
                        )
                        log.info(f"Transition result: {str(result)[:200]}")
                        sw.record_track(str(result)[:100])
                    except Exception as e:
                        log.error(f"Agent transition error: {e}")

                    # Wait for transition to finish before next check
                    time.sleep(30)
                    continue
        except Exception as e:
            log.warning(f"Status check error: {e}")

        time.sleep(5)  # poll every 5 seconds

    # Shutdown
    sw.phase = "stopped"
    sw.stop()
    log.info(f"Set complete. {len(sw.tracks_played)} tracks in {elapsed:.0f}s")
