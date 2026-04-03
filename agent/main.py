"""DJ Treta v6.0 — DJClaw (Being as Brain)

The Being starts, stays alive, and decides everything.
No watchdog. No state machine. No deterministic DJ logic.
Just an agent with a heartbeat — she sees reality and acts.

Usage:
    python -m agent              # Start Being
    djtreta start                # Same, via CLI
    djclaw start                 # Same, via DJClaw CLI
"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .config import load_config, Config
from .agents import create_agents
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from .heartbeat import HeartbeatMixin
from .transitions import TransitionMixin
from .planner_loop import PlannerMixin
from .sets import SetsMixin
from .commands import CommandsMixin
from .adk_runner import ADKRunnerMixin
from .session import SessionMixin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dj-treta")

STATE_FILE = Path("/tmp/dj-treta-state.json")
COMMAND_FILE = Path("/tmp/dj-treta-command.json")
PID_FILE = Path("/tmp/dj-treta.pid")
PLAYLIST_FILE = Path("/tmp/dj-treta-playlist.json")
PERSIST_FILE = Path(__file__).parent.parent / ".beings" / "session.json"
THINKING_FILE = Path("/tmp/dj-treta-thinking.log")
BILLING_FILE = Path("/tmp/dj-treta-billing.json")


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


def _litellm_reachable(base_url: str, api_key: str = "") -> bool:
    """Check if LiteLLM is reachable (200 or 401 both mean it's running)."""
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{base_url}/health", timeout=2, headers=headers)
        return r.status_code in (200, 401)  # 401 = running but needs key
    except Exception:
        return False


def _ensure_litellm(config):
    """Start local LiteLLM if not running."""
    if _litellm_reachable(config.llm.api_base, config.llm.api_key):
        return  # already running

    log.info("LiteLLM not running — starting locally")
    config_file = Path(__file__).parent.parent / "litellm_config.yaml"
    if not config_file.exists():
        log.warning("No litellm_config.yaml found — skipping LiteLLM auto-start")
        return

    venv_python = Path(__file__).parent.parent / ".venv" / "bin" / "litellm"
    subprocess.Popen(
        [str(venv_python), "--config", str(config_file), "--port", "4000"],
        stdout=open("/tmp/litellm-local.log", "w"),
        stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).parent.parent),
    )
    for i in range(20):
        time.sleep(0.5)
        if _litellm_reachable(config.llm.api_base, config.llm.api_key):
            log.info(f"LiteLLM up after {(i+1)*0.5:.1f}s")
            return
    log.warning("LiteLLM failed to start")


def _ensure_mixxx(config: Config):
    """Start Mixxx if not running (when mixxx.auto_start is true)."""
    url = config.mixxx.url
    try:
        httpx.get(f"{url}/api/status", timeout=2)
        return  # already running
    except Exception:
        pass

    if not config.mixxx.auto_start:
        log.warning("Mixxx not reachable — auto_start is false; start Mixxx manually")
        return

    log.info("Mixxx not running — starting it")
    default_bin = Path.home() / "workspace" / "mixxx-treta" / "build" / "mixxx"
    default_res = Path.home() / "workspace" / "mixxx-treta" / "res"
    default_settings = Path.home() / "Library" / "Application Support" / "Mixxx"

    mixxx_bin = Path(config.mixxx.binary).expanduser() if config.mixxx.binary.strip() else default_bin
    resource = Path(config.mixxx.resource_path).expanduser() if config.mixxx.resource_path.strip() else default_res
    settings = Path(config.mixxx.settings_path).expanduser() if config.mixxx.settings_path.strip() else default_settings

    if not mixxx_bin.exists():
        log.error(f"Mixxx not found: {mixxx_bin}")
        return

    subprocess.Popen(
        [str(mixxx_bin), "--resourcePath", str(resource), "--settingsPath", str(settings)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for i in range(30):
        time.sleep(0.5)
        try:
            httpx.get(f"{url}/api/status", timeout=2)
            log.info(f"Mixxx up after {(i+1)*0.5:.1f}s — waiting for audio engine...")
            time.sleep(15)  # audio engine needs time after HTTP API is ready
            return
        except Exception:
            pass
    log.error("Mixxx failed to start")


def _get_status(url: str) -> dict | None:
    try:
        return httpx.get(f"{url}/api/status", timeout=2).json()
    except Exception:
        return None


def _active_idle_decks(status: dict) -> tuple[int, int]:
    """Which deck is primary on-air vs free to load the next track."""
    d1 = status.get("deck1", {})
    d2 = status.get("deck2", {})
    xf = float(status.get("crossfader", 0))
    p1, p2 = d1.get("playing"), d2.get("playing")
    r1 = float(d1.get("remaining_seconds", 0) or 0)
    r2 = float(d2.get("remaining_seconds", 0) or 0)
    if p1 and not p2:
        return 1, 2
    if p2 and not p1:
        return 2, 1
    if p1 and p2:
        if xf < -0.2:
            return 1, 2
        if xf > 0.2:
            return 2, 1
        return (1, 2) if r1 >= r2 else (2, 1)
    return 1, 2


def _count_tracks(music_dir: Path) -> int:
    n = 0
    if not music_dir.exists():
        return 0
    for d in music_dir.iterdir():
        if d.is_dir() and not d.name.startswith('.'):
            n += sum(1 for f in d.iterdir() if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'))
    return n


# ── The Being ─────────────────────────────────────────────────────────

class DJTretaBeing(
    HeartbeatMixin,
    TransitionMixin,
    PlannerMixin,
    SetsMixin,
    CommandsMixin,
    ADKRunnerMixin,
    SessionMixin,
):

    def __init__(self, config: Config):
        self.config = config
        self.agent = None
        self.planner_agent = None
        self._running = True
        self._agent_busy = False
        self._planner_busy = False
        self._talk_lock = threading.Lock()

        # State (shared with TUI via state file)
        self.mood = ""
        self.tracks_played: list[dict] = []
        self._last_command = ""
        self._last_command_id = ""
        self._last_result = ""

        # Conversation memory
        self._chat_history: list[tuple[str, str]] = []

        # Self-evolution tracking
        self._last_reflect_count = 0

        # Current set
        self.current_set = None

        # Track when each deck's current track started (for minimum play time)
        self._deck_start_time = {1: 0.0, 2: 0.0}  # wall clock when track started on deck
        self._deck_track = {1: "", 2: ""}  # track path on each deck

        # State tracking for TUI
        self._emergency_count = 0
        self._recording_active = False
        self._broadcast_active = False

        # Scheduled transition — Python executes, agent is free
        self._transition_pending = False

        # Serialize all DJ agent invocations (talk, heartbeat, skip, reflect)
        self._agent_lock = threading.Lock()

        # DJ → Planner communication: user intent from conversation
        self.user_intent = ""  # e.g. "play some bhojpuri mix"

        # Generation status for TUI
        self._generation_status = {}

        # v6.0 Directive system — Being sets, agents read
        self.dj_directive = ""
        self.planner_directive = ""

        # ADK v5.0 — single persistent event loop (avoids LiteLLM Queue binding errors)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()
        self._session_service = InMemorySessionService()
        self._being_runner = None
        self._dj_runner = None
        self._planner_runner = None
        self._being_session = None
        self._dj_session = None
        self._planner_session = None

    def start(self):
        _check_single_instance()
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())
        signal.signal(signal.SIGINT, lambda s, f: self.stop())

        log.info("DJ Treta Being alive")

        if not (self.config.llm.api_key or "").strip():
            log.warning(
                "LLM api_key is empty — set llm.api_key in config.yaml or export "
                "DJTRETA_LLM_API_KEY / LLM_API_KEY"
            )

        # Reset billing + thinking log + playlist + stale transitions for fresh session
        BILLING_FILE.unlink(missing_ok=True)
        THINKING_FILE.write_text("")
        PLAYLIST_FILE.unlink(missing_ok=True)
        Path("/tmp/dj-treta-scheduled-transition.json").unlink(missing_ok=True)
        Path("/tmp/dj-treta-transition-pending.lock").unlink(missing_ok=True)
        Path("/tmp/dj-treta-directives.json").unlink(missing_ok=True)
        Path("/tmp/dj-treta-mood-change.json").unlink(missing_ok=True)

        # Init SQLite DB + scan library
        from .db import init_db, scan_library
        init_db()
        scan_library(self.config.library.music_path)

        _ensure_litellm(self.config)
        _ensure_mixxx(self.config)
        self._restore_session()

        log.info("Creating ADK agents (v6.0)...")
        being_agent, dj_agent, planner_agent = create_agents(self.config)
        self.being_agent = being_agent
        self.agent = dj_agent
        self.planner_agent = planner_agent

        # Context compaction — summarize older messages periodically
        compaction = EventsCompactionConfig(
            compaction_interval=10,  # compact every 10 invocations (~5 min)
            overlap_size=2,          # keep last 2 exchanges verbatim
        )
        being_app = App(name="treta_being", root_agent=being_agent, events_compaction_config=compaction)
        dj_app = App(name="dj_treta", root_agent=dj_agent, events_compaction_config=compaction)
        planner_app = App(name="dj_treta_planner", root_agent=planner_agent, events_compaction_config=compaction)

        self._being_runner = Runner(app=being_app, session_service=self._session_service)
        self._dj_runner = Runner(app=dj_app, session_service=self._session_service)
        self._planner_runner = Runner(app=planner_app, session_service=self._session_service)

        async def _init_sessions():
            self._being_session = await self._session_service.create_session(app_name="treta_being", user_id="listener")
            self._dj_session = await self._session_service.create_session(app_name="dj_treta", user_id="dj")
            self._planner_session = await self._session_service.create_session(app_name="dj_treta_planner", user_id="planner")
        self._run_async(_init_sessions())

        # State writer for TUI (infrastructure)
        threading.Thread(target=self._state_loop, daemon=True).start()

        # Planner loop — background track planning
        threading.Thread(target=self._planner_loop, daemon=True).start()

        # Read startup mood if provided via CLI
        mood_file = Path("/tmp/dj-treta-mood.txt")
        if mood_file.exists():
            self.mood = mood_file.read_text().strip()
            mood_file.unlink()
            log.info(f"Startup mood: {self.mood}")

        # Start broadcast + recording + set
        self._start_broadcast()
        self._start_set(mood=self.mood if self.mood else None)

        # Start relay (pushes state to dj.treta.life)
        if self.config.relay.enabled:
            from .relay import RelayEngine
            self.relay = RelayEngine(self.config, self)
            threading.Thread(target=self._relay_loop, daemon=True).start()

        # Main loop — the Being's heartbeat
        self._next_sleep = 30
        log.info("Ready. Listening.")
        while self._running:
            try:
                self._check_commands()
                self._pick_up_directives()
                self._heartbeat()

            except Exception as e:
                log.warning(f"Loop error: {e}")

            time.sleep(max(1.0, self._next_sleep))

        log.info("DJ Treta Being shutting down")

    def stop(self):
        # End set + recording + broadcast gracefully
        if self.current_set:
            from .db import update_set
            self.current_set["status"] = "finished"
            self.current_set["ended_at"] = time.time()
            self.current_set["track_count"] = len(self.tracks_played)
            self._stop_recording()
            update_set(self.current_set)
            log.info(f"Set ended on shutdown: {self.current_set['id']}")
        self._stop_broadcast()
        self._save_session()
        self._running = False
        PID_FILE.unlink(missing_ok=True)

    # ── Relay ──────────────────────────────────────────────────────────

    def _relay_loop(self):
        """Run relay WebSocket push in asyncio event loop."""
        import asyncio
        try:
            asyncio.run(self.relay.run())
        except Exception as e:
            log.error(f"Relay loop crashed: {e}")


# ── Entry point ───────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser(description="DJ Treta Being")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    being = DJTretaBeing(config)
    being.start()
