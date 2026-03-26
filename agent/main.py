"""DJ Treta v2.0 — Pure Software 3.0

The Being starts, stays alive, and decides everything.
No watchdog. No state machine. No deterministic DJ logic.
Just an agent that looks at reality and acts.

Usage:
    python -m agent              # Start Being
    djtreta start                # Same, via CLI
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
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
PID_FILE = Path("/tmp/dj-treta.pid")
PERSIST_FILE = Path(__file__).parent.parent / ".beings" / "session.json"


# ── Infrastructure helpers ────────────────────────────────────────────

def _check_single_instance():
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text().strip()), 0)
            log.error("Another instance already running.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            PID_FILE.unlink()
    PID_FILE.write_text(str(os.getpid()))


def _ensure_mixxx(url: str):
    """Start Mixxx if not running."""
    try:
        httpx.get(f"{url}/api/status", timeout=2)
        return  # already running
    except Exception:
        pass

    log.info("Mixxx not running — starting it")
    mixxx_bin = Path.home() / "workspace" / "mixxx-treta" / "build" / "mixxx"
    if not mixxx_bin.exists():
        log.error(f"Mixxx not found: {mixxx_bin}")
        return

    subprocess.Popen(
        [str(mixxx_bin),
         "--resourcePath", str(Path.home() / "workspace" / "mixxx-treta" / "res"),
         "--settingsPath", str(Path.home() / "Library" / "Application Support" / "Mixxx")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for i in range(30):
        time.sleep(0.5)
        try:
            httpx.get(f"{url}/api/status", timeout=2)
            log.info(f"Mixxx up after {(i+1)*0.5:.1f}s")
            return
        except Exception:
            pass
    log.error("Mixxx failed to start")


def _get_status(url: str) -> dict | None:
    try:
        return httpx.get(f"{url}/api/status", timeout=2).json()
    except Exception:
        return None


def _count_tracks(music_dir: Path) -> int:
    n = 0
    if not music_dir.exists():
        return 0
    for d in music_dir.iterdir():
        if d.is_dir() and not d.name.startswith('.'):
            n += sum(1 for f in d.iterdir() if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'))
    return n


# ── The Being ─────────────────────────────────────────────────────────

class DJTretaBeing:

    def __init__(self, config: Config):
        self.config = config
        self.agent = None
        self._running = True
        self._agent_busy = False
        self._talk_lock = threading.Lock()

        # State (shared with TUI via state file)
        self.mood = ""
        self.tracks_played: list[dict] = []
        self._last_command = ""
        self._last_command_id = ""
        self._last_result = ""

    def start(self):
        _check_single_instance()
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())
        signal.signal(signal.SIGINT, lambda s, f: self.stop())

        log.info("DJ Treta Being alive")

        _ensure_mixxx(self.config.mixxx.url)
        self._restore_session()

        log.info("Creating DJ agent...")
        self.agent = create_dj_agent(self.config)

        # State writer for TUI (infrastructure)
        threading.Thread(target=self._state_loop, daemon=True).start()

        # Main loop — the Being's heartbeat
        log.info("Ready. Listening.")
        while self._running:
            try:
                self._check_commands()

                if not self._agent_busy:
                    self._pulse()

            except Exception as e:
                log.warning(f"Loop error: {e}")

            time.sleep(5)

        log.info("DJ Treta Being shutting down")

    def stop(self):
        self._save_session()
        self._running = False
        PID_FILE.unlink(missing_ok=True)

    # ── Pulse — the Being looks at reality and decides ────────────────

    def _pulse(self):
        """Every 5s: look at Mixxx, decide if action needed, call agent if yes."""
        status = _get_status(self.config.mixxx.url)
        if not status:
            _ensure_mixxx(self.config.mixxx.url)
            return

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        d1_playing = d1.get("playing", False)
        d2_playing = d2.get("playing", False)

        needs_action = False

        if not d1_playing and not d2_playing:
            needs_action = True  # SILENCE
        elif d1_playing and d1.get("remaining_seconds", 999) < 120:
            needs_action = True  # Track ending soon
        elif d2_playing and d2.get("remaining_seconds", 999) < 120:
            needs_action = True  # Track ending soon

        if needs_action:
            context = self._build_context(status)
            self._agent_busy = True
            threading.Thread(target=self._agent_act, args=(context,), daemon=True).start()

    def _agent_act(self, context):
        """Agent looks at reality and does what a DJ should do."""
        try:
            played_list = [t.get("title", "?") for t in self.tracks_played]
            result = self.agent.run(
                f"{context}\n\n"
                f"Already played: {played_list}\n\n"
                f"You are DJing. Look at the state and do what's needed:\n"
                f"- If NOTHING is playing: find a track, load on deck 1, play it\n"
                f"- If a track is ending (<2 min): find next track, load on idle deck, do_transition\n"
                f"- If library is empty: search YouTube, download tracks, then play\n"
                f"- Pick individual tracks (3-8 min), not full sets\n"
                f"- Use do_transition for smooth crossfade\n"
                f"Go."
            )
            log.info(f"Agent acted: {str(result)[:200]}")

            # Record what's playing now
            try:
                status = _get_status(self.config.mixxx.url)
                if status:
                    for dk in [1, 2]:
                        if status.get(f"deck{dk}", {}).get("playing"):
                            tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=3).json()
                            if tinfo and not tinfo.get("error"):
                                title = tinfo.get("title", "")
                                if title and not any(t.get("title") == title for t in self.tracks_played):
                                    self.tracks_played.append({"title": title, "time": time.time()})
            except Exception:
                pass

        except Exception as e:
            log.error(f"Agent error: {e}")
        finally:
            self._agent_busy = False

    # ── Commands from TUI/MCP ─────────────────────────────────────────

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
        cmd_id = raw.get("id", "")
        log.info(f"Command: {cmd}")

        self._last_command = cmd
        self._last_command_id = cmd_id
        self._last_result = "processing..."
        self._write_state()

        try:
            result = self._handle_command(cmd, args, cmd_id)
        except Exception as e:
            result = f"Error: {e}"

        if result != "processing...":
            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Result: {result[:200]}")

    def _handle_command(self, cmd, args, cmd_id):
        if cmd == "talk":
            message = args.get("message", "")
            if not message:
                return "No message"
            if not self.agent:
                return "Brain not ready"

            # Check if this is a play request
            if any(w in message.lower() for w in ["play", "start", "baja", "shuru", "bajao"]):
                for m in ["melodic", "techno", "deep", "dark", "progressive", "ambient",
                          "chill", "vocal", "house", "psychill", "minimal", "bhojpuri",
                          "trance", "lofi", "bollywood", "psytrance"]:
                    if m in message.lower():
                        self.mood = m
                        break
                if not self.mood:
                    self.mood = "deep"

            threading.Thread(target=self._agent_talk, args=(message, cmd_id), daemon=True).start()
            return "processing..."

        elif cmd == "skip":
            threading.Thread(
                target=self._agent_skip, daemon=True
            ).start()
            return "processing..."

        elif cmd == "stop":
            threading.Thread(
                target=lambda: self.agent.run("Fade out the current track gracefully over 30 seconds.") if self.agent else None,
                daemon=True
            ).start()
            return "Fading out..."

        elif cmd == "change_mood":
            self.mood = args.get("mood", self.mood)
            return f"Mood changed to {self.mood}"

        else:
            return f"Unknown: {cmd}"

    def _agent_talk(self, message, cmd_id):
        """Hybrid: LLM classify → fast chat or full agent."""
        try:
            context = self._build_context(_get_status(self.config.mixxx.url))

            # Quick classify (1 LLM call, ~2s)
            classify = completion(
                model=self.config.llm.model,
                messages=[{"role": "user", "content":
                    f'Is this message a REQUEST to DO something (play a track, skip, change EQ, download, adjust BPM) '
                    f'or just a QUESTION/CONVERSATION (asking about plans, chatting, asking what\'s playing)?\n'
                    f'Answer ONLY "tools" if it\'s a request to take action, or "chat" if it\'s conversation.\n'
                    f'Message: "{message}"'}],
                api_base=self.config.llm.api_base,
                api_key=self.config.llm.api_key,
                temperature=0, timeout=10,
            )
            needs_tools = "tool" in classify.choices[0].message.content.lower()

            if needs_tools:
                with self._talk_lock:
                    result = str(self.agent.run(
                        f'{context}\n\nThe listener says: "{message}"\n\n'
                        f'Take action using your tools, then respond briefly.'
                    ))
            else:
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

            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Talk done: {result[:200]}")
        except Exception as e:
            self._last_command_id = cmd_id
            self._last_result = f"Error: {e}"
            self._write_state()

    def _agent_skip(self):
        """Agent decides skip — she picks track and technique."""
        try:
            context = self._build_context(_get_status(self.config.mixxx.url))
            result = self.agent.run(
                f"{context}\n\n"
                f"SKIP NOW. Find a new track, load it on the idle deck, "
                f"and do_transition quickly (20s). Go."
            )
            self._last_result = f"Skipped: {str(result)[:150]}"
            self._write_state()
        except Exception as e:
            self._last_result = f"Skip error: {e}"
            self._write_state()

    # ── Context from reality ──────────────────────────────────────────

    def _build_context(self, status):
        if not status:
            return "Mixxx not responding."

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        parts = [f"Mood: {self.mood or 'not set'}"]
        parts.append(f"Tracks played: {len(self.tracks_played)}")
        parts.append(f"Library: {_count_tracks(self.config.library.music_path)} tracks")

        for dk, d in [(1, d1), (2, d2)]:
            if d.get("track_loaded"):
                state = "PLAYING" if d.get("playing") else "LOADED (paused)"
                parts.append(
                    f"Deck {dk}: {state}, {d.get('remaining_seconds', 0):.0f}s remaining, "
                    f"{d.get('bpm', 0):.0f} BPM (file: {d.get('file_bpm', 0):.0f})"
                )
            else:
                parts.append(f"Deck {dk}: empty")

        xf = status.get("crossfader", 0)
        parts.append(f"Crossfader: {xf:.2f} ({'Deck 1' if xf < -0.3 else 'Deck 2' if xf > 0.3 else 'center'})")

        return "\n".join(parts)

    # ── Session persistence ───────────────────────────────────────────

    def _save_session(self):
        try:
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            PERSIST_FILE.write_text(json.dumps({
                "mood": self.mood,
                "tracks_played": self.tracks_played,
                "saved_at": time.time(),
            }, indent=2))
        except Exception:
            pass

    def _restore_session(self):
        try:
            if not PERSIST_FILE.exists():
                return
            data = json.loads(PERSIST_FILE.read_text())
            if time.time() - data.get("saved_at", 0) > 3600:
                return
            self.mood = data.get("mood", "")
            self.tracks_played = data.get("tracks_played", [])
            log.info(f"Restored: mood={self.mood}, tracks={len(self.tracks_played)}")
        except Exception:
            pass

    # ── State writer for TUI ──────────────────────────────────────────

    def _state_loop(self):
        save_counter = 0
        while self._running:
            self._write_state()
            save_counter += 1
            if save_counter % 5 == 0:
                self._save_session()
            time.sleep(2)

    def _write_state(self):
        try:
            status = _get_status(self.config.mixxx.url)
            current = {"title": "", "bpm": 0, "key": "", "remaining": 0}

            if status:
                for dk in [1, 2]:
                    d = status.get(f"deck{dk}", {})
                    if d.get("playing"):
                        try:
                            tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2).json()
                            if tinfo and not tinfo.get("error"):
                                current["title"] = tinfo.get("title", "")
                        except Exception:
                            pass
                        current["bpm"] = d.get("bpm", 0)
                        current["key"] = d.get("key", 0)
                        current["remaining"] = d.get("remaining_seconds", 0)
                        break

            # Read billing
            billing_str = ""
            try:
                bf = Path("/tmp/dj-treta-billing.json")
                if bf.exists():
                    b = json.loads(bf.read_text())
                    total_tok = b.get("total_input_tokens", 0) + b.get("total_output_tokens", 0)
                    cost = b.get("total_cost_usd", 0)
                    if total_tok > 1_000_000:
                        billing_str = f"{total_tok/1_000_000:.1f}M tokens ${cost:.3f}"
                    elif total_tok > 0:
                        billing_str = f"{total_tok//1000}K tokens ${cost:.4f}"
            except Exception:
                pass

            STATE_FILE.write_text(json.dumps({
                "phase": "playing" if (status and (status.get("deck1", {}).get("playing") or status.get("deck2", {}).get("playing"))) else "idle",
                "mood": self.mood,
                "tracks_played": len(self.tracks_played),
                "set_elapsed": 0,
                "set_remaining": "infinite",
                "current_track": current,
                "next_track": None,
                "planned_tracks": [],
                "last_command": self._last_command,
                "last_command_id": self._last_command_id,
                "last_command_result": self._last_result,
                "billing": billing_str,
                "consecutive_errors": 0,
            }, indent=2))
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser(description="DJ Treta Being")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    being = DJTretaBeing(config)
    being.start()
