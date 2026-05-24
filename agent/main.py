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

from .audio_files import is_audio_file
from .config import load_config, Config
from .agents import create_agents
from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from .heartbeat import HeartbeatMixin
from .transitions import TransitionMixin
from .planner_loop import PlannerMixin
from .library_loop import LibraryMixin
from .producer_loop import ProducerMixin
from .sets import SetsMixin
from .commands import CommandsMixin
from .adk_runner import ADKRunnerMixin
from .session import SessionMixin
from .session_state import Session, register_session
from .evolution import EvolutionMixin
from .ws_server import WSServerMixin
from .being_heartbeat import BeingHeartbeatMixin
from .runtime_paths import runtime_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dj-treta")

STATE_FILE = runtime_path("state.json")
COMMAND_FILE = runtime_path("command.json")
PID_FILE = runtime_path("dj-treta.pid")
PLAYLIST_FILE = runtime_path("playlist.json")
PERSIST_FILE = Path(__file__).parent.parent / ".beings" / "session.json"
THINKING_FILE = runtime_path("thinking.log")
BILLING_FILE = runtime_path("billing.json")


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


def _resolve_litellm_config() -> Path | None:
    """Find the LiteLLM proxy config in priority order.

      1. $DJCLAW_LITELLM_CONFIG    — explicit override
      2. ~/.config/djclaw/litellm.yaml — XDG (installer-managed)
      3. <repo>/litellm_config.yaml    — dev fallback (legacy name)
    """
    env = os.environ.get("DJCLAW_LITELLM_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = Path("~/.config/djclaw/litellm.yaml").expanduser()
    if xdg.exists():
        return xdg
    repo = Path(__file__).parent.parent / "litellm_config.yaml"
    if repo.exists():
        return repo
    return None


def _resolve_litellm_binary() -> Path | None:
    """Find the litellm proxy binary — repo .venv first (dev), then the
    XDG installer venv. Returns None if neither has it.
    """
    repo_venv = Path(__file__).parent.parent / ".venv" / "bin" / "litellm"
    if repo_venv.exists():
        return repo_venv
    xdg_venv = Path("~/.local/share/djclaw/venv/bin/litellm").expanduser()
    if xdg_venv.exists():
        return xdg_venv
    return None


def _ensure_litellm(config):
    """Start local LiteLLM if not running."""
    if _litellm_reachable(config.llm.api_base, config.llm.api_key):
        return  # already running

    log.info("LiteLLM not running — starting locally")
    config_file = _resolve_litellm_config()
    if config_file is None or not config_file.exists():
        log.warning(
            "No litellm config found at $DJCLAW_LITELLM_CONFIG, "
            "~/.config/djclaw/litellm.yaml, or repo litellm_config.yaml — "
            "skipping LiteLLM auto-start. Run `djclaw setup` to generate one."
        )
        return

    venv_python = _resolve_litellm_binary()
    if venv_python is None:
        log.warning("litellm binary not found in any venv; skipping auto-start")
        return

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


_mixxx_launch_lock = threading.Lock()


def _mixxx_process_running(mixxx_bin: Path) -> bool:
    """True if a Mixxx process already exists (even if its API isn't up yet).

    Used to avoid spawning a second instance while the first is still booting —
    multiple instances fight over the settings DB + port 7778 and none bind,
    causing an unbounded spawn cascade.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", str(mixxx_bin)],
            capture_output=True, text=True, timeout=2,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def _ensure_mixxx(config: Config):
    """Start Mixxx if not running (when mixxx.auto_start is true)."""
    url = config.mixxx.url
    try:
        httpx.get(f"{url}/api/status", timeout=2)
        return  # already running + API up
    except Exception:
        pass

    if not config.mixxx.auto_start:
        log.warning("Mixxx not reachable — auto_start is false; start Mixxx manually")
        return

    # Serialize launch attempts. Concurrent heartbeat ticks must NOT each spawn
    # their own Mixxx — that's the multi-instance cascade. Non-blocking acquire:
    # if a launch is already in flight, bail immediately.
    if not _mixxx_launch_lock.acquire(blocking=False):
        log.debug("Mixxx launch already in progress — skipping duplicate spawn")
        return
    try:
        default_bin = Path.home() / "workspace" / "mixxx-treta" / "build" / "mixxx"
        default_res = Path.home() / "workspace" / "mixxx-treta" / "res"
        default_settings = Path.home() / "Library" / "Application Support" / "Mixxx"

        mixxx_bin = Path(config.mixxx.binary).expanduser() if config.mixxx.binary.strip() else default_bin
        resource = Path(config.mixxx.resource_path).expanduser() if config.mixxx.resource_path.strip() else default_res
        settings = Path(config.mixxx.settings_path).expanduser() if config.mixxx.settings_path.strip() else default_settings

        if not mixxx_bin.exists():
            log.error(
                f"Mixxx not found: {mixxx_bin} — rebuild with: "
                f"cd ~/workspace/mixxx-treta && source tools/macos_buildenv.sh setup && "
                f"cmake -B build -DHTTPAPI=ON && cmake --build build -j8"
            )
            return

        # If a Mixxx process already exists (booting, API not up yet), DO NOT
        # spawn another — just wait for its API to come online.
        if _mixxx_process_running(mixxx_bin):
            log.info("Mixxx process already running (booting) — waiting for API, not spawning another")
        else:
            launch_cmd = [str(mixxx_bin), "--resourcePath", str(resource), "--settingsPath", str(settings)]
            if getattr(config.mixxx, "qml_ui", False):
                # QML-UI mode renders the in-booth Sarathi panel (res/qml).
                launch_cmd.append("--qml")
            log.info(f"Mixxx not running — starting it ({'QML UI' if '--qml' in launch_cmd else 'QWidget skin'})")
            subprocess.Popen(
                launch_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        for i in range(60):  # up to 30s for API to come online
            time.sleep(0.5)
            try:
                httpx.get(f"{url}/api/status", timeout=2)
                log.info(f"Mixxx API up after {(i+1)*0.5:.1f}s — waiting for audio engine...")
                time.sleep(15)  # audio engine needs time after HTTP API is ready
                return
            except Exception:
                pass
        log.error("Mixxx failed to start (API never came up within 30s)")
    finally:
        _mixxx_launch_lock.release()


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
            n += sum(1 for f in d.iterdir() if is_audio_file(f))
    return n


# ── The Being ─────────────────────────────────────────────────────────

class DJTretaBeing(
    HeartbeatMixin,
    TransitionMixin,
    PlannerMixin,
    LibraryMixin,
    ProducerMixin,
    SetsMixin,
    CommandsMixin,
    ADKRunnerMixin,
    SessionMixin,
    EvolutionMixin,
    WSServerMixin,
    BeingHeartbeatMixin,
):

    def __init__(self, config: Config):
        self.config = config
        self.agent = None
        self.planner_agent = None
        self._running = True
        self._agent_busy = False
        self._planner_busy = False
        self._talk_lock = threading.Lock()

        # ── Single source of truth: Session class ────────────────────
        # All live state the user/TUI/agents care about lives in Session.
        # Auto-persisted to .beings/session.json. See agent/session_state.py.
        session_path = Path(__file__).parent.parent / ".beings" / "session.json"
        self.session = Session.load(session_path)
        register_session(self.session)
        # Back-reference so session-scoped tools (e.g. Sarathi suggest/confirm)
        # can reach _ws_broadcast. Underscore-prefixed → bypasses the session
        # observer and is never serialized to session.json.
        object.__setattr__(self.session, "_being_ref", self)

        # Command bookkeeping — internal, not user-facing
        self._last_command = ""
        self._last_command_id = ""
        self._last_result = ""

        # Track when each deck's current track started (for minimum play time)
        self._deck_start_time = {1: 0.0, 2: 0.0}  # wall clock when track started on deck
        self._deck_track = {1: "", 2: ""}  # track path on each deck

        # Subsystem state — internal Python machinery, not user state
        self._recording_active = False
        self._broadcast_active = False

        # Scheduled transition — Python executes, agent is free
        self._transition_pending = False

        # Phase A2: timestamp when idle_needs_load last flipped to True.
        # Watchdog P2 uses this to detect a stuck signal (DJ agent hung)
        # and fall back to Python rank-1 load before silence hits.
        self._idle_needs_load_set_at = 0.0

        # Serialize all DJ agent invocations (talk, heartbeat, skip, reflect)
        self._agent_lock = threading.Lock()

        # Generation status for TUI
        self._generation_status = {}

        # ADK v5.0 — single persistent event loop (avoids LiteLLM Queue binding errors)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()
        self._session_service = InMemorySessionService()
        self._being_runner = None
        self._dj_runner = None
        self._planner_runner = None
        self._library_runner = None           # v8 Phase 5
        self._producer_runner = None          # v8 Phase 6
        self._being_session = None
        # Boot-replay flag: True until first Treta invocation after fresh
        # daemon start. When True, _being_talk reads the last K turns from
        # today's chat JSONL and prepends them as a context block. This
        # gives Treta conversation continuity across daemon restarts
        # without polluting every prompt — replay only happens once per
        # boot, then the ADK session accumulates new turns normally.
        # Set evolution plan Tier 2-stretch.
        self._chat_replay_pending = True
        self._dj_session = None
        self._planner_session = None
        self._library_session = None          # v8 Phase 5
        self._producer_session = None         # v8 Phase 6

    # ── Session property delegates ───────────────────────────────────
    # These let every existing mixin keep reading/writing self.mood,
    # self.tracks_played, etc. unchanged — but state actually lives in
    # self.session and is auto-persisted. Critical fields (mood,
    # tracks_played, current_set, directives) sync-write to disk on each
    # mutation; transients debounce 500ms.

    @property
    def mood(self) -> str:
        return self.session.mood

    @mood.setter
    def mood(self, value: str):
        self.session.mood = value or ""

    @property
    def tracks_played(self) -> list:
        return self.session.tracks_played

    @tracks_played.setter
    def tracks_played(self, value: list):
        self.session.tracks_played = list(value or [])

    @property
    def current_set(self):
        return self.session.current_set

    @current_set.setter
    def current_set(self, value):
        self.session.current_set = value

    @property
    def user_intent(self) -> str:
        return self.session.user_intent

    @user_intent.setter
    def user_intent(self, value: str):
        self.session.user_intent = value or ""

    @property
    def dj_directive(self) -> str:
        return self.session.dj_directive

    @dj_directive.setter
    def dj_directive(self, value: str):
        self.session.dj_directive = value or ""

    @property
    def planner_directive(self) -> str:
        return self.session.planner_directive

    @planner_directive.setter
    def planner_directive(self, value: str):
        self.session.planner_directive = value or ""

    @property
    def _emergency_count(self) -> int:
        return self.session.emergency_count

    @_emergency_count.setter
    def _emergency_count(self, value: int):
        self.session.emergency_count = int(value or 0)

    @property
    def _last_reflect_count(self) -> int:
        return self.session.last_reflect_count

    @_last_reflect_count.setter
    def _last_reflect_count(self, value: int):
        self.session.last_reflect_count = int(value or 0)

    @property
    def _chat_history(self):
        # Returns the ObservedList so callers' `.append((user, resp))` triggers
        # Session dirty flag + flush. Tuples are JSON-serialized as 2-item
        # lists; consumers iterate with `for u, r in self._chat_history:`
        # which handles both shapes.
        return self.session.chat_history

    @_chat_history.setter
    def _chat_history(self, value):
        self.session.chat_history = list(value or [])

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
        runtime_path("scheduled-transition.json").unlink(missing_ok=True)
        runtime_path("transition-pending.lock").unlink(missing_ok=True)
        runtime_path("directives.json").unlink(missing_ok=True)
        runtime_path("mood-change.json").unlink(missing_ok=True)

        # Init SQLite DB + scan library
        from .db import init_db, scan_library
        init_db()
        scan_library(self.config.library.music_path)

        _ensure_litellm(self.config)
        _ensure_mixxx(self.config)

        # Reset rate on both decks — clears any leftover offsets from previous session
        try:
            url = self.config.mixxx.url
            for deck in [1, 2]:
                httpx.post(f"{url}/api/control",
                           json={"group": f"[Channel{deck}]", "key": "rate_ratio", "value": 1.0}, timeout=2)
                httpx.post(f"{url}/api/control",
                           json={"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0}, timeout=2)
            log.info("Reset rate on both decks (startup)")
        except Exception as e:
            log.warning(f"Startup rate reset failed: {e}")

        self._restore_session()

        log.info("Creating ADK agents (v8)...")
        being_agent, dj_agent, planner_agent, library_agent, producer_agent = create_agents(self.config)
        self.being_agent = being_agent
        self.agent = dj_agent
        self.planner_agent = planner_agent
        self.library_agent = library_agent
        self.producer_agent = producer_agent

        # No events_compaction: ADK compaction can drop tool results while assistant
        # messages still reference tool_call_ids → "Missing tool results" API errors.
        being_app = App(name="treta_being", root_agent=being_agent)
        dj_app = App(name="dj_treta", root_agent=dj_agent)
        planner_app = App(name="dj_treta_planner", root_agent=planner_agent)
        library_app = App(name="dj_treta_library", root_agent=library_agent)
        producer_app = App(name="dj_treta_producer", root_agent=producer_agent)

        self._being_runner = Runner(app=being_app, session_service=self._session_service)
        self._dj_runner = Runner(app=dj_app, session_service=self._session_service)
        self._planner_runner = Runner(app=planner_app, session_service=self._session_service)
        self._library_runner = Runner(app=library_app, session_service=self._session_service)
        self._producer_runner = Runner(app=producer_app, session_service=self._session_service)

        async def _init_sessions():
            self._being_session = await self._session_service.create_session(app_name="treta_being", user_id="listener")
            self._dj_session = await self._session_service.create_session(app_name="dj_treta", user_id="dj")
            self._planner_session = await self._session_service.create_session(app_name="dj_treta_planner", user_id="planner")
            self._library_session = await self._session_service.create_session(app_name="dj_treta_library", user_id="library")
            self._producer_session = await self._session_service.create_session(app_name="dj_treta_producer", user_id="producer")
        self._run_async(_init_sessions())

        # State writer for TUI (infrastructure)
        threading.Thread(target=self._state_loop, daemon=True).start()

        # Planner loop — background track planning
        threading.Thread(target=self._planner_loop, daemon=True).start()

        # Library loop — fills library on session.library_need signals (v8 Phase 5)
        threading.Thread(target=self._library_loop, daemon=True).start()

        # Producer loop — generates originals on session.producer_need (v8 Phase 6)
        threading.Thread(target=self._producer_loop, daemon=True).start()

        # ── Evolution: multi-loop consciousness ────────────────────
        # Three new loops layered on the existing heartbeat-driven cycle.
        # Each runs in its own daemon thread and reads session/memory.
        # Reflection: 15 min cadence — synthesizes recent activity.
        # Journal: 6 hr OR 5 min idle — daily journal entry (was previously
        #   called "dream"; renamed for honesty — current impl is linear
        #   daily synthesis, not free-associative recombination).
        # Intention: weekly (Sun 23:00) — meta intentions for next week.
        # See evolution plan Tier 2.4/2.5/2.6.
        try:
            from .reflection_loop import ReflectionLoop
            self._reflection_loop = ReflectionLoop(self)
            self._reflection_loop.start()
        except Exception as exc:
            log.warning(f"reflection loop failed to start (non-fatal): {exc}")
        try:
            from .journal_loop import JournalLoop
            self._journal_loop = JournalLoop(self)
            self._journal_loop.start()
        except Exception as exc:
            log.warning(f"journal loop failed to start (non-fatal): {exc}")
        try:
            from .intention_loop import IntentionLoop
            self._intention_loop = IntentionLoop(self)
            self._intention_loop.start()
        except Exception as exc:
            log.warning(f"intention loop failed to start (non-fatal): {exc}")

        # Session callback: when mood changes, do three things.
        # 1. Update the current set's mood/genre fields (for DB + relay).
        # 2. Force planner replan on next tick.
        # 3. Kick off async LLM mood resolution → write session.mood_profile.
        #
        # Replaces the old file-IPC polling in CommandsMixin._pick_up_directives.
        def _on_mood_change(name, old, new):
            if not new or new == old:
                return
            if self.current_set:
                # Mutating the dict in place would bypass Session dirty
                # detection; reassign to trigger flush.
                cs = dict(self.current_set)
                cs["mood"] = new
                cs["genre"] = new
                self.current_set = cs
            if hasattr(self.config, "planner"):
                self._tracks_since_plan = self.config.planner.replan_every_n_tracks
            log.info(f"Mood changed via Session callback: {new}")

            # Async LLM mood resolver — runs in a thread so the callback
            # returns immediately and the LLM call doesn't block the writer.
            #
            # Race guard: if session.mood changes again before this thread's
            # LLM call returns (e.g. boot-time default "techno-deep" → user
            # set_mood "melodic-techno"), the slower resolver must NOT clobber
            # the newer profile. We re-check session.mood against `new` after
            # the resolve and bail if a fresher write has landed.
            mood_target = new
            def _resolve():
                try:
                    from .mood_resolver import resolve_mood
                    profile = resolve_mood(mood_target)
                    if self.session.mood != mood_target:
                        log.info(
                            f"Mood resolver: discarding stale profile for "
                            f"{mood_target!r} (current mood={self.session.mood!r})"
                        )
                        return
                    self.session.mood_profile = profile.to_dict()
                    log.info(
                        f"Mood profile resolved: {profile.canonical_slug} "
                        f"(BPM {profile.bpm_range}, conf {profile.confidence:.2f})"
                    )
                except Exception as exc:
                    log.warning(f"Mood resolver thread error: {exc}")
            threading.Thread(target=_resolve, daemon=True).start()

        self.session.register_callback("mood", _on_mood_change)

        # Default mood fallback — apply config.set.default_mood only after the
        # mood callback is registered so mood_profile resolution fires.
        # Without this, post-hard_reset session.mood='' leaves the planner
        # flying blind and DJ picks incompatible tracks from the library.
        if not self.session.mood and self.config.set.default_mood:
            self.session.mood = self.config.set.default_mood
        # If mood is set but profile hasn't resolved (e.g., mood was applied
        # before callback was registered on a prior run), kick resolver now.
        elif self.session.mood and not (self.session.mood_profile or {}).get("canonical_slug"):
            _on_mood_change("mood", "", self.session.mood)

        # Phase A2: track when idle_needs_load flips True so the watchdog
        # can tell if DJ has hung on the signal for longer than N seconds.
        def _on_idle_needs_load(name, old, new):
            if new and not old:
                self._idle_needs_load_set_at = time.time()

        self.session.register_callback("idle_needs_load", _on_idle_needs_load)

        # Read startup mood if provided via CLI
        mood_file = runtime_path("mood.txt")
        if mood_file.exists():
            self.mood = mood_file.read_text().strip()
            mood_file.unlink()
            log.info(f"Startup mood: {self.mood}")

        # WebSocket server for MCP and other clients
        self._start_ws_server()

        # Being heartbeat — consciousness LoopAgent (self-reflection, goals, memory)
        self._start_being_heartbeat()

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

            # Responsive sleep: keep the heartbeat's chosen cadence
            # (self._next_sleep, up to 30s) but poll for commands every 0.5s so
            # /skip and friends don't sit unprocessed for the whole interval.
            # Break early ONLY for a user-initiated skip — that needs a
            # sub-second response. Background signals (idle_needs_load /
            # replan_requested) deliberately do NOT trigger an early re-tick:
            # they can be set+unsatisfied for a while (e.g. idle_needs_load
            # stays set while a transition is pending), and breaking on them
            # would busy-spin the heartbeat every 0.5s. They're handled on the
            # normal heartbeat cadence, which is fine for background work.
            _deadline = time.time() + max(1.0, self._next_sleep)
            while self._running and time.time() < _deadline:
                time.sleep(0.5)
                try:
                    self._check_commands()
                except Exception as e:
                    log.warning(f"Command poll error: {e}")
                # Break for a pending skip, but at most once per 2s so an
                # unsatisfiable skip (e.g. empty idle deck) can't busy-spin the
                # heartbeat — it re-ticks every 2s instead of every 0.5s.
                if getattr(self.session, "user_skip", None):
                    _now = time.time()
                    if _now - getattr(self, "_last_skip_break", 0.0) >= 2.0:
                        self._last_skip_break = _now
                        break

        log.info("DJ Treta Being shutting down")

    def stop(self):
        # End set + recording + broadcast gracefully
        if self.current_set:
            from .db import update_set
            # Reassign dict to trigger Session flush on critical current_set field
            cs = dict(self.current_set)
            cs["status"] = "finished"
            cs["ended_at"] = time.time()
            cs["track_count"] = len(self.tracks_played)
            self.current_set = cs
            self._stop_recording()
            update_set(cs)
            log.info(f"Set ended on shutdown: {cs['id']}")
        self._stop_broadcast()
        # Final Session flush before exit. Session's atexit handler will also
        # fire but this makes shutdown ordering explicit.
        self.session.flush()
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
    parser.add_argument("--mood", default=None, help="Set mood/genre (e.g. BollyAfro, melodic-techno)")
    parser.add_argument("--duration", type=int, default=None, help="Set duration in minutes")
    args = parser.parse_args()

    config = load_config(args.config)
    being = DJTretaBeing(config)
    if args.mood:
        being.mood = args.mood
    if args.duration:
        being.config.sets.default_duration_minutes = args.duration
    being.start()
