"""DJ Treta v5.0 — DJClaw (ADK)

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

from .config import load_config, Config
from .agents import create_agents
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

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

class DJTretaBeing:

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

        # Generation status for TUI
        self._generation_status = {}

        # ADK v5.0 — single persistent event loop (avoids LiteLLM Queue binding errors)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()
        self._session_service = InMemorySessionService()
        self._dj_runner = None
        self._planner_runner = None
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

        # Reset billing + thinking log + playlist for fresh session
        BILLING_FILE.unlink(missing_ok=True)
        THINKING_FILE.write_text("")
        PLAYLIST_FILE.unlink(missing_ok=True)

        # Init SQLite DB + scan library
        from .db import init_db, scan_library
        init_db()
        scan_library(self.config.library.music_path)

        _ensure_litellm(self.config)
        _ensure_mixxx(self.config)
        self._restore_session()

        log.info("Creating ADK agents (v5.0)...")
        dj_agent, planner_agent = create_agents(self.config)
        self.agent = dj_agent
        self.planner_agent = planner_agent

        # Context compaction — summarize older messages periodically
        compaction = EventsCompactionConfig(
            compaction_interval=10,  # compact every 10 invocations (~5 min)
            overlap_size=2,          # keep last 2 exchanges verbatim
        )
        dj_app = App(name="dj_treta", root_agent=dj_agent, events_compaction_config=compaction)
        planner_app = App(name="dj_treta_planner", root_agent=planner_agent, events_compaction_config=compaction)

        self._dj_runner = Runner(app=dj_app, session_service=self._session_service)
        self._planner_runner = Runner(app=planner_app, session_service=self._session_service)

        async def _init_sessions():
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

                if not self._agent_busy:
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

    # ── Heartbeat — monitors + transitions (no loading, no agent calls) ──

    def _heartbeat(self):
        """Pure Python heartbeat. Reads mix_out from DB. No flags, no timers."""
        status = _get_status(self.config.mixxx.url)
        if not status:
            _ensure_mixxx(self.config)
            return

        active_deck, idle_deck = _active_idle_decks(status)
        d_active = status.get(f"deck{active_deck}", {})
        d_idle = status.get(f"deck{idle_deck}", {})
        position = float(d_active.get("position_seconds", 0) or 0)
        remaining = float(d_active.get("remaining_seconds", 0) or 0)
        playing = d_active.get("playing", False)
        idle_loaded = d_idle.get("track_loaded", False)
        idle_remaining = float(d_idle.get("remaining_seconds", 0) or 0)

        nothing_playing = (not status.get("deck1", {}).get("playing")
                           and not status.get("deck2", {}).get("playing"))

        # === PRIORITY 1: SILENCE — emergency recovery ===
        if nothing_playing:
            self._next_sleep = 5
            if not self._agent_busy:
                self._agent_busy = True
                threading.Thread(target=self._emergency_play, daemon=True).start()
            return

        idle_ready = idle_loaded and idle_remaining > 60
        duration = float(d_active.get("duration", 0) or 0)

        # === PRIORITY 2: Execute scheduled transition (Python handles timing) ===
        if not self._transition_pending:
            sched_file = Path("/tmp/dj-treta-scheduled-transition.json")
            if sched_file.exists():
                try:
                    sched = json.loads(sched_file.read_text())
                    self._transition_pending = True
                    threading.Thread(
                        target=self._execute_scheduled_transition,
                        args=(sched,), daemon=True
                    ).start()
                except Exception as e:
                    log.warning(f"Bad scheduled transition file: {e}")
                    sched_file.unlink(missing_ok=True)

        # === PRIORITY 3: Agent decides transition (Software 3.0) ===
        # Only ask after 50% played (saves tokens) and when idle deck ready
        # Don't ask if transition is already pending
        if (idle_ready and duration > 0 and position > (duration * 0.5)
                and not self._agent_busy and not self._transition_pending):
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
            idle_bpm = d_idle.get("bpm", 0)
            active_key = active_meta.get("key_musical", "?") if active_meta else "?"
            idle_key = idle_meta.get("key_musical", "?") if idle_meta else "?"

            instruction = (
                f"ACTIVE: '{active_track[:40]}' at {position:.0f}s/{duration:.0f}s "
                f"({remaining:.0f}s left, BPM:{active_bpm:.0f}, Key:{active_key})\n"
                f"  NOW IN: {active_section}\n"
                f"  TIMELINE: {active_timeline}\n\n"
                f"NEXT: '{idle_track[:40]}' on deck {idle_deck} "
                f"(BPM:{idle_bpm:.0f}, Key:{idle_key})\n"
                f"  TIMELINE: {idle_timeline}\n\n"
                f"You are the DJ. Look at the timelines.\n"
                f"If you want to transition, call schedule_transition with the right track position.\n"
                f"If not ready yet, just explain why you're waiting."
            )

            self._agent_busy = True

            def _run():
                try:
                    result = self._invoke_agent(instruction)
                    log.info(f"DJ decision: {result[:500]}")
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

        # === PRIORITY 4: Backup load — planner didn't load idle deck ===
        # Trigger earlier for short tracks (generated tracks ~150s)
        load_threshold = min(120, duration * 0.4) if duration > 0 else 60
        if not idle_loaded and position > load_threshold and playing:
            self._next_sleep = 10
            log.warning("Backup: loading idle deck (planner missed it)")
            self._load_next_on_idle(status)
            return

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

    def _execute_scheduled_transition(self, sched: dict):
        """Python-side transition executor. Waits for track position, then executes.
        Runs in its own thread. Agent is FREE during this entire time."""
        from .tools import do_transition, do_bass_swap, do_filter_sweep, do_hard_cut, do_echo_out

        to_deck = sched["toDeck"]
        at_position = sched["atPosition"]
        technique = sched.get("technique", "crossfade")
        duration = sched.get("duration", 45)
        active_deck = sched.get("activeDeck", 1 if to_deck == 2 else 2)

        log.info(f"Transition scheduled: {technique} to deck {to_deck} at {at_position}s (waiting...)")

        try:
            # Poll until position reached or track ends
            # Adaptive sleep: far away = 5s, close = 0.3s for precision
            while True:
                status = _get_status(self.config.mixxx.url)
                if not status:
                    log.warning("Scheduled transition: Mixxx not responding, aborting")
                    break

                d = status.get(f"deck{active_deck}", {})
                if not d.get("playing"):
                    log.warning(f"Scheduled transition: Deck {active_deck} stopped, aborting")
                    break

                current_pos = float(d.get("position_seconds", 0) or 0)
                gap = at_position - current_pos

                if gap <= 0:
                    # Time to execute — right on the mark
                    log.info(f"Executing {technique} to deck {to_deck} at {current_pos:.1f}s (target: {at_position}s)")
                    if technique == "bass_swap":
                        result = do_bass_swap(to_deck, duration)
                    elif technique == "filter_sweep":
                        result = do_filter_sweep(to_deck, duration)
                    elif technique == "hard_cut":
                        result = do_hard_cut(to_deck)
                    elif technique == "echo_out":
                        result = do_echo_out(to_deck, duration)
                    else:
                        result = do_transition(to_deck, duration)
                    log.info(f"Transition result: {str(result)[:200]}")
                    # Mark transition event in energy arc
                    if self.current_set and isinstance(self.current_set.get("energy_arc"), list):
                        self.current_set["energy_arc"].append({
                            "t": round(time.time() - self.current_set["started_at"]),
                            "event": "transition",
                            "technique": technique,
                            "to_deck": to_deck,
                        })
                    self._record_playing_tracks()
                    self._check_set_duration()
                    break

                # Adaptive sleep: tight when close, relaxed when far
                if gap > 30:
                    time.sleep(5)
                elif gap > 10:
                    time.sleep(2)
                elif gap > 3:
                    time.sleep(0.5)
                else:
                    time.sleep(0.2)
        except Exception as e:
            log.error(f"Scheduled transition error: {e}")
        finally:
            self._transition_pending = False
            Path("/tmp/dj-treta-scheduled-transition.json").unlink(missing_ok=True)

    def _emergency_play(self):
        """Silence! Direct API play first (fast + reliable), agent fallback for empty library."""
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

                httpx.post(f"{url}/api/play", json={"deck": 1}, timeout=3)
                httpx.post(f"{url}/api/crossfade", json={"position": 0.0}, timeout=3)
                time.sleep(2)
                log.info(f"Emergency play: {Path(track).stem[:50]}")
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
                    httpx.post(f"{url}/api/play", json={"deck": 1}, timeout=3)
                    httpx.post(f"{url}/api/crossfade", json={"position": 0.0}, timeout=3)
                    log.info(f"Emergency play: {Path(filepath).stem[:50]}")
                    self._record_playing_tracks()
            elif self.config.sources.youtube:
                result = self._invoke_agent(
                    f"{self._build_context(_get_status(url))}\n\n"
                    f"SILENCE! Empty library. Search YouTube, download a {self.mood or 'melodic-techno'} track, "
                    f"load on deck 1, play it, set crossfader to 0.0."
                )
                log.info(f"Emergency play (agent): {result[:200]}")
                self._record_playing_tracks()
            self._record_playing_tracks()
        except Exception as e:
            import traceback
            log.error(f"Emergency play error: {type(e).__name__}: {e}")
            log.error(traceback.format_exc()[:500])
        finally:
            self._agent_busy = False

    def _load_next_on_idle(self, status):
        """Load next compatible track on idle deck — direct Mixxx API, no agent."""
        from .db import find_compatible_tracks, get_track_by_path

        active_deck, idle_deck = _active_idle_decks(status)
        d_idle = status.get(f"deck{idle_deck}", {})

        # Skip if idle already has a fresh track
        if d_idle.get("track_loaded") and float(d_idle.get("remaining_seconds", 0) or 0) > 60:
            return

        # Get BOTH deck file paths — never load what's on either deck
        exclude_paths = set()
        for dk in [1, 2]:
            try:
                tinfo = httpx.get(
                    f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2
                ).json()
                p = tinfo.get("file_path", "")
                if p:
                    exclude_paths.add(p)
            except Exception:
                pass

        active_path = ""
        try:
            tinfo = httpx.get(
                f"{self.config.mixxx.url}/api/deck/{active_deck}/track_info", timeout=2
            ).json()
            active_path = tinfo.get("file_path", "")
        except Exception:
            pass

        # Find compatible tracks from DB
        played_titles = [t.get("title", "") for t in self.tracks_played]
        current_meta = get_track_by_path(active_path) if active_path else None

        candidates = []
        if current_meta and current_meta.get("bpm"):
            candidates = find_compatible_tracks(
                bpm=current_meta["bpm"],
                key_camelot=current_meta.get("key_camelot", ""),
                energy=current_meta.get("energy_peak", 5),
                played_titles=played_titles,
            )
        # Filter out tracks on EITHER deck
        candidates = [c for c in candidates if c.get("path") not in exclude_paths]

        # When youtube source is off, prefer Treta originals
        if not self.config.sources.youtube and self.config.sources.treta_originals:
            originals = [c for c in candidates
                         if c.get("artist") == "DJ Treta" or "DJ Treta" in c.get("title", "") or c.get("title", "").startswith("DJ Treta")]
            if originals:
                candidates = originals

        if not candidates:
            # Fallback: get ANY track from DB (analyzed or not) — Mixxx can play anything
            from .db import get_db
            db = get_db()
            try:
                all_tracks = [dict(r) for r in db.execute(
                    "SELECT path, title FROM tracks ORDER BY RANDOM() LIMIT 20"
                ).fetchall()]
                candidates = [t for t in all_tracks
                              if t.get("path") not in exclude_paths
                              and t.get("title") not in played_titles]
            finally:
                db.close()

        if not candidates:
            log.warning("No tracks available to load on idle deck")
            return

        next_track = candidates[0]
        track_path = next_track["path"]

        # Load via Mixxx API
        try:
            result = httpx.post(
                f"{self.config.mixxx.url}/api/load",
                json={"deck": idle_deck, "track": track_path},
                timeout=5,
            ).json()

            if result.get("ok"):
                log.info(f"Loaded deck {idle_deck}: {next_track.get('title', '?')[:50]}")

                # Save duration from Mixxx (Gemini analysis often misses it)
                try:
                    url = self.config.mixxx.url
                    time.sleep(1)
                    st = _get_status(url)
                    if st:
                        dur = float(st.get(f"deck{idle_deck}", {}).get("duration", 0) or 0)
                        if dur > 0:
                            from .db import upsert_track
                            upsert_track(path=track_path, duration_seconds=dur)
                except Exception:
                    pass

            else:
                log.warning(f"Load failed: {result}")
        except Exception as e:
            log.warning(f"Load error: {e}")

    def _record_playing_tracks(self):
        """Track what's playing for set history + deck start times."""
        try:
            status = _get_status(self.config.mixxx.url)
            if not status:
                return
            for dk in [1, 2]:
                if status.get(f"deck{dk}", {}).get("playing"):
                    tinfo = httpx.get(
                        f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=3
                    ).json()
                    if tinfo and not tinfo.get("error"):
                        title = tinfo.get("title", "")
                        path = tinfo.get("file_path", "")
                        # Track deck start time — reset when track changes
                        if path and path != self._deck_track.get(dk, ""):
                            self._deck_track[dk] = path
                            self._deck_start_time[dk] = time.time()
                        if title and not any(t.get("title") == title for t in self.tracks_played):
                            self.tracks_played.append({"title": title, "time": time.time()})
                            # Record in DB set_history
                            if self.current_set:
                                from .db import add_track_to_set
                                add_track_to_set(self.current_set["id"], title, dk, "")
        except Exception:
            pass

    # ── Relay ──────────────────────────────────────────────────────────

    def _relay_loop(self):
        """Run relay WebSocket push in asyncio event loop."""
        import asyncio
        try:
            asyncio.run(self.relay.run())
        except Exception as e:
            log.error(f"Relay loop crashed: {e}")

    # ── Sets + Recording + Broadcast ─────────────────────────────────

    def _start_set(self, mood=None, genre=None, duration=None, title=None):
        """Start a new DJ set. Auto-decides mood/duration/name if not provided."""
        from .db import insert_set, get_next_set_number
        import random

        set_id = f"set-{time.strftime('%Y%m%d-%H%M%S')}"
        set_number = get_next_set_number()
        set_mood = mood or self.mood or "melodic-techno"

        # Let the AI name the set
        if not title:
            try:
                from litellm import completion
                cfg = load_config()
                resp = completion(
                    model=cfg.llm.model,
                    messages=[{"role": "user", "content":
                        f"Reply with ONLY a creative 2-4 word name for a {set_mood} DJ set. "
                        f"Examples: Midnight Signal, Dark Matter, Velvet Underground, Neural Drift. "
                        f"No explanation. No quotes. Just the name."}],
                    api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
                    temperature=0.9, timeout=10,
                )
                title = resp.choices[0].message.content.strip()[:50]
            except Exception:
                title = f"Set #{set_number}"

        self.current_set = {
            "id": set_id,
            "set_number": set_number,
            "title": title,
            "started_at": time.time(),
            "mood": set_mood,
            "genre": genre or set_mood or "melodic-techno",
            "target_duration": duration or self.config.sets.default_duration_minutes,
            "tracks": [],
            "energy_arc": [],
            "peak_energy": 0,
            "status": "live",
        }
        insert_set(self.current_set)
        self._start_recording()
        log.info(f"Set started: '{title}' ({set_mood}, {self.current_set['target_duration']}m)")

    def _end_set(self):
        """End current set, stop recording, auto-start new one."""
        if not self.current_set:
            return
        from .db import update_set
        self.current_set["status"] = "finished"
        self.current_set["ended_at"] = time.time()
        self.current_set["track_count"] = len(self.tracks_played)
        self._stop_recording()
        update_set(self.current_set)
        log.info(f"Set ended: {self.current_set['id']} ({len(self.tracks_played)} tracks)")
        # Store finished set for relay to pick up (one final push)
        self.last_finished_set = dict(self.current_set)
        # Auto-start new set
        self._start_set()

    def _check_set_duration(self):
        """Check if current set has reached target duration."""
        if not self.current_set:
            return
        elapsed = (time.time() - self.current_set["started_at"]) / 60
        if elapsed >= self.current_set["target_duration"]:
            self._end_set()

    def _start_recording(self):
        """Start Mixxx recording (if local_recording enabled)."""
        if not self.config.sets.local_recording:
            return
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Recording]", "key": "toggle_recording", "value": 1
            }, timeout=3)
            self._recording_active = True
            log.info("Recording started")
        except Exception as e:
            log.warning(f"Recording start failed: {e}")

    def _stop_recording(self):
        """Stop Mixxx recording (if local_recording enabled)."""
        if not self.config.sets.local_recording:
            return
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Recording]", "key": "toggle_recording", "value": 0
            }, timeout=3)
            self._recording_active = False
            log.info("Recording stopped")
        except Exception as e:
            log.warning(f"Recording stop failed: {e}")

    def _start_broadcast(self):
        """Enable Mixxx Shoutcast broadcast."""
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Shoutcast]", "key": "enabled", "value": 1
            }, timeout=3)
            self._broadcast_active = True
            log.info("Broadcast started")
        except Exception as e:
            log.warning(f"Broadcast start failed: {e}")

    def _stop_broadcast(self):
        """Disable Mixxx Shoutcast broadcast."""
        try:
            url = self.config.mixxx.url
            httpx.post(f"{url}/api/control", json={
                "group": "[Shoutcast]", "key": "enabled", "value": 0
            }, timeout=3)
            self._broadcast_active = False
            log.info("Broadcast stopped")
        except Exception as e:
            log.warning(f"Broadcast stop failed: {e}")

    # ── Self-evolution ─────────────────────────────────────────────────

    def _agent_reflect(self):
        """Periodic self-evolution — reflect on recent tracks."""
        if self._agent_busy:
            return  # skip if agent is already working
        try:
            recent = [t.get("title", "?") for t in self.tracks_played[-5:]]
            self._invoke_agent(
                f"REFLECTION: Last 5 tracks were: {recent}\n"
                f"Use save_learning() to note what worked and what didn't.\n"
                f"Then respond with a brief summary of your learnings."
            )
            log.info("Self-reflection complete")
        except Exception as e:
            log.warning(f"Reflection error: {e}")

    # ── Planner — background track planning ──────────────────────────

    def _planner_loop(self):
        """Background: plan 6 tracks, load idle deck, re-plan every 4 tracks."""
        self._tracks_since_plan = 0
        last_track = ""
        time.sleep(5)  # let heartbeat boot first
        while self._running:
            try:
                status = _get_status(self.config.mixxx.url)
                if not status:
                    time.sleep(10)
                    continue

                current_track = self._get_current_track_title(status)

                # Detect track change (transition happened)
                if current_track and current_track != last_track:
                    last_track = current_track
                    self._tracks_since_plan += 1
                    # Immediately load next track on idle deck
                    self._load_next_on_idle(status)
                    # Self-evolution check
                    if (len(self.tracks_played) >= 5
                            and len(self.tracks_played) - self._last_reflect_count >= 5):
                        self._last_reflect_count = len(self.tracks_played)
                        threading.Thread(target=self._agent_reflect, daemon=True).start()

                playlist = self._read_playlist()
                needs_plan = (
                    not playlist
                    or not playlist.get("planner_output")
                    or self._tracks_since_plan >= self.config.planner.replan_every_n_tracks
                )

                if needs_plan and not self._planner_busy:
                    self._planner_busy = True
                    self._tracks_since_plan = 0
                    try:
                        self._run_planner(status, current_track)
                        # Load after planning
                        status = _get_status(self.config.mixxx.url)
                        if status:
                            self._load_next_on_idle(status)
                    finally:
                        self._planner_busy = False

            except Exception as e:
                import traceback
                log.warning(f"Planner loop error: {type(e).__name__}: {e}")
                log.warning(traceback.format_exc()[:500])
            time.sleep(15)  # 15s — fast enough for short generated tracks (~150s)

    def _run_planner(self, status, current_track):
        """Run planner agent with DB-powered track selection."""
        from .db import get_track_by_path, find_compatible_tracks, get_all_analyzed_tracks

        played_list = [t.get("title", "?") for t in self.tracks_played]

        # Get current track's REAL metadata from DB
        current_meta = None
        for dk in [1, 2]:
            if status.get(f"deck{dk}", {}).get("playing"):
                try:
                    tinfo = httpx.get(
                        f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2
                    ).json()
                    file_path = tinfo.get("file_path", "")
                    if file_path:
                        current_meta = get_track_by_path(file_path)
                except Exception:
                    pass

        # SQL query for compatible tracks
        candidates = []
        if current_meta and current_meta.get("bpm"):
            candidates = find_compatible_tracks(
                bpm=current_meta.get("bpm", 125),
                key_camelot=current_meta.get("key_camelot", ""),
                energy=current_meta.get("energy_peak", 5),
                played_titles=played_list,
            )

        # Build compact candidate list for planner
        candidate_text = ""
        if candidates:
            for c in candidates:
                # Compact timeline summary
                timeline_summary = ""
                tl = c.get("timeline", "")
                if tl:
                    try:
                        import json as _json
                        sections = _json.loads(tl) if isinstance(tl, str) else tl
                        parts = [f"{s['section']}({s['energy']})" for s in sections]
                        timeline_summary = f" | Structure: {' → '.join(parts)}"
                    except Exception:
                        pass

                candidate_text += (
                    f"  - {c['title']} | path: {c['path']} | "
                    f"BPM:{c.get('bpm',0):.0f} Key:{c.get('key_musical','?')} "
                    f"Energy:{c.get('energy_peak','?')} "
                    f"Mix-in:{c.get('mix_in_seconds',0) or 0:.0f}s "
                    f"Mix-out:{c.get('mix_out_seconds',0) or 0:.0f}s"
                    f"{timeline_summary}\n"
                )

        # Current track info
        current_info = "NOTHING — silence!"
        if current_meta:
            bpm = current_meta.get('bpm') or 0
            key = current_meta.get('key_musical') or '?'
            energy = current_meta.get('energy_peak') or '?'
            current_info = f"{current_track} | BPM:{bpm:.0f} Key:{key} Energy:{energy}"

        log.info(f"Planner running — current: {current_track or 'nothing'}, {len(candidates)} candidates in DB")
        result = self._invoke_planner(
            f"Currently playing: {current_info}\n"
            f"Already played (DO NOT repeat): {played_list}\n\n"
            f"Tracks already in library:\n{candidate_text or '  (none)'}\n\n"
            f"Current mood/genre: {self.mood or 'melodic-techno'}.\n"
            + self._build_source_instructions() +
            f"After creating/finding new tracks, analyze each one.\n"
            f"Then pick the best next 3 tracks from what's available.\n"
            f"For each: title, full path, BPM, key, energy, why it fits."
        )
        log.info(f"Planner done: {str(result)[:500]}")

        self._write_playlist(result, current_track)

    def _build_source_instructions(self) -> str:
        """Build planner instructions based on enabled music sources."""
        mood = self.mood or 'melodic-techno'
        parts = []
        if self.config.sources.youtube:
            parts.append(
                f"Search YouTube and download {self.config.planner.download_new_tracks} NEW "
                f"'{mood}' tracks. Search for different artists each time. "
                f"Don't download what's already in library.\n"
            )
        else:
            parts.append("YouTube is DISABLED. Do NOT search YouTube, do NOT download. You cannot.\n")
        if self.config.sources.treta_originals:
            gen_count = self.config.planner.generate_new_tracks
            if not self.config.sources.youtube:
                # Originals only — generate more to compensate
                gen_count = self.config.planner.download_new_tracks + self.config.planner.generate_new_tracks
            parts.append(
                f"Delegate to your 'producer' sub-agent to generate {gen_count} "
                f"original track(s). Tell the producer the BPM, key, genre='{mood}', and describe the mood/instruments.\n"
                f"Example: producer(\"Generate a {mood} track, 125 BPM, A minor, with warm pads and driving bass, genre {mood}\")\n"
                f"This is YOUR music — be creative with the description. Each track should sound DIFFERENT.\n"
            )
        if not self.config.sources.youtube and not self.config.sources.treta_originals:
            parts.append("Only use tracks already in the library.\n")
        return "".join(parts)

    def _auto_load_track(self, filepath):
        """Load a freshly generated track on the idle deck."""
        status = _get_status(self.config.mixxx.url)
        if not status:
            return
        _, idle_deck = _active_idle_decks(status)
        try:
            result = httpx.post(
                f"{self.config.mixxx.url}/api/load",
                json={"deck": idle_deck, "track": filepath}, timeout=5
            ).json()
            if result.get("ok"):
                log.info(f"Auto-loaded generated track on deck {idle_deck}: {Path(filepath).stem[:50]}")
        except Exception as e:
            log.warning(f"Auto-load failed: {e}")

    def _get_current_track_title(self, status) -> str:
        """Get the title of the currently playing track."""
        for dk in [1, 2]:
            if status.get(f"deck{dk}", {}).get("playing"):
                try:
                    tinfo = httpx.get(
                        f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2
                    ).json()
                    return tinfo.get("title", "")
                except Exception:
                    pass
        return ""

    def _write_playlist(self, planner_output, current_track):
        """Write planner output to playlist file."""
        playlist = {
            "current": {"title": current_track or ""},
            "planner_output": planner_output[:2000],
            "played": [t.get("title", "?") for t in self.tracks_played],
            "updated_at": time.time(),
        }
        PLAYLIST_FILE.write_text(json.dumps(playlist, indent=2))

    def _read_playlist(self) -> dict | None:
        try:
            if PLAYLIST_FILE.exists():
                return json.loads(PLAYLIST_FILE.read_text())
        except Exception:
            pass
        return None

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

            # Extract mood from play requests
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
            threading.Thread(target=self._agent_skip, daemon=True).start()
            return "processing..."

        elif cmd == "stop":
            if self.agent and not self._agent_busy:
                threading.Thread(
                    target=lambda: self._invoke_agent("Fade out the current track gracefully over 30 seconds."),
                    daemon=True
                ).start()
                return "Fading out..."
            return "Agent busy — try again in a moment"

        elif cmd == "change_mood":
            self.mood = args.get("mood", self.mood)
            return f"Mood changed to {self.mood}"

        elif cmd == "change_sources":
            source = args.get("source", "")
            enabled = args.get("enabled", True)
            if source == "youtube":
                self.config.sources.youtube = enabled
            elif source in ("treta_originals", "originals"):
                self.config.sources.treta_originals = enabled
            # Recreate agents with new tool access
            log.info(f"Source changed: {source} → {'on' if enabled else 'off'} — rebuilding agents")
            dj_agent, planner_agent = create_agents(self.config)
            self.agent = dj_agent
            self.planner_agent = planner_agent
            compaction = EventsCompactionConfig(compaction_interval=10, overlap_size=2)
            dj_app = App(name="dj_treta", root_agent=dj_agent, events_compaction_config=compaction)
            planner_app = App(name="dj_treta_planner", root_agent=planner_agent, events_compaction_config=compaction)
            self._dj_runner = Runner(app=dj_app, session_service=self._session_service)
            self._planner_runner = Runner(app=planner_app, session_service=self._session_service)
            async def _reinit():
                self._dj_session = await self._session_service.create_session(app_name="dj_treta", user_id="dj")
                self._planner_session = await self._session_service.create_session(app_name="dj_treta_planner", user_id="planner")
            self._run_async(_reinit())
            return f"Source {source} → {'on' if enabled else 'off'} (agents rebuilt)"

        else:
            return f"Unknown: {cmd}"

    def _run_async(self, coro, timeout=120):
        """Run async coroutine on the persistent event loop. Thread-safe."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"ADK agent call timed out after {timeout}s")

    async def _invoke_agent_async(self, instruction: str) -> str:
        """Invoke DJ agent via ADK runner. Processes events for billing + thinking log."""
        message = types.Content(role="user", parts=[types.Part(text=instruction)])
        result = ""
        async for event in self._dj_runner.run_async(
            session_id=self._dj_session.id, user_id="dj", new_message=message
        ):
            self._process_event(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        result += part.text
        return result

    def _invoke_agent(self, instruction: str, timeout: int = 60) -> str:
        """Invoke DJ agent. Sync wrapper with lock to prevent concurrent session access."""
        with self._agent_lock:
            return self._run_async(self._invoke_agent_async(instruction), timeout=timeout)

    async def _invoke_planner_async(self, instruction: str) -> str:
        """Invoke planner agent via ADK runner. Processes events for billing + thinking log."""
        message = types.Content(role="user", parts=[types.Part(text=instruction)])
        result = ""
        async for event in self._planner_runner.run_async(
            session_id=self._planner_session.id, user_id="planner", new_message=message
        ):
            self._process_event(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        result += part.text
        return result

    def _invoke_planner(self, instruction: str) -> str:
        """Invoke planner. Sync wrapper — longer timeout for generation."""
        return self._run_async(self._invoke_planner_async(instruction), timeout=600)

    def _process_event(self, event):
        """Extract billing + thinking from ADK events → files for TUI."""
        try:
            agent_name = event.author or "agent"

            # Thinking — text content from agent
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text and len(part.text.strip()) > 5:
                        text = part.text.strip()
                        if not text.startswith('{') and not text.startswith('['):
                            with open(THINKING_FILE, "a") as f:
                                f.write(f"[THINK:{agent_name}] {text[:500]}\n")

            # Tool calls
            func_calls = event.get_function_calls()
            if func_calls:
                for fc in func_calls:
                    args_str = str(fc.args)[:200] if fc.args else ""
                    with open(THINKING_FILE, "a") as f:
                        f.write(f"[CALL:{agent_name}] {fc.name}({args_str})\n")

            # Billing — usage_metadata
            if event.usage_metadata:
                um = event.usage_metadata
                inp = um.prompt_token_count or 0
                out = um.candidates_token_count or 0
                if inp > 0 or out > 0:
                    self._update_billing(agent_name, inp, out)
        except Exception:
            pass

    def _update_billing(self, agent_name: str, inp: int, out: int):
        """Update billing JSON file with token counts."""
        try:
            billing = json.loads(BILLING_FILE.read_text()) if BILLING_FILE.exists() else {
                "total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0,
                "calls": 0, "by_agent": {}, "session_start": time.time()
            }
            billing["total_input_tokens"] += inp
            billing["total_output_tokens"] += out
            billing["calls"] += 1
            cost = (inp / 1_000_000 * 0.10) + (out / 1_000_000 * 0.40)
            billing["total_cost_usd"] += cost
            if agent_name not in billing["by_agent"]:
                billing["by_agent"][agent_name] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0}
            billing["by_agent"][agent_name]["input"] += inp
            billing["by_agent"][agent_name]["output"] += out
            billing["by_agent"][agent_name]["cost"] += cost
            billing["by_agent"][agent_name]["calls"] += 1
            BILLING_FILE.write_text(json.dumps(billing, indent=2))
        except Exception:
            pass

    def _agent_talk(self, message, cmd_id):
        """One agent, one personality. Always."""
        try:
            context = self._build_context(_get_status(self.config.mixxx.url))
            history = self._format_history()

            with self._talk_lock:
                result = self._invoke_agent(
                    f"{context}\n\n{history}\n\n"
                    f'The listener says: "{message}"\n\n'
                    f"Respond naturally. Use tools only if they asked you to DO something."
                )

            # Update conversation memory
            self._chat_history.append((message, result))
            if len(self._chat_history) > 10:
                self._chat_history = self._chat_history[-10:]

            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Talk done: {result[:500]}")
        except Exception as e:
            self._last_command_id = cmd_id
            self._last_result = f"Error: {e}"
            self._write_state()

    def _agent_skip(self):
        """Agent decides skip — she picks track and technique."""
        try:
            status = _get_status(self.config.mixxx.url)
            context = self._build_context(status)
            active, idle = _active_idle_decks(status) if status else (1, 2)
            result = self._invoke_agent(
                f"{context}\n\n"
                f"ACTIVE deck: {active}, IDLE deck: {idle}. "
                f"SKIP NOW. Find a new track, load it on deck {idle}, "
                f"and do_transition quickly (20s). Go."
            )
            self._last_result = f"Skipped: {result[:150]}"
            self._write_state()
        except Exception as e:
            self._last_result = f"Skip error: {e}"
            self._write_state()

    # ── Conversation memory ────────────────────────────────────────────

    def _format_history(self) -> str:
        """Format recent conversation for agent context."""
        if not self._chat_history:
            return ""
        lines = ["Recent conversation:"]
        for user_msg, response in self._chat_history[-5:]:
            lines.append(f"Listener: {user_msg}")
            lines.append(f"DJ Treta: {response[:500]}")
        return "\n".join(lines)

    # ── Context from reality ──────────────────────────────────────────

    def _build_context(self, status):
        if not status:
            return "Mixxx not responding."

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        src_parts = []
        if self.config.sources.youtube:
            src_parts.append("youtube")
        if self.config.sources.treta_originals:
            src_parts.append("treta_originals")
        parts = [f"Mood: {self.mood or 'not set'}  Sources: {', '.join(src_parts) or 'none'}"]
        parts.append(f"Tracks played: {len(self.tracks_played)}")

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

        # Compact library listing — saves ~5K tokens vs agent calling list_library_tracks
        parts.append(f"\nLibrary ({_count_tracks(self.config.library.music_path)} tracks):")
        parts.append(self._get_library_summary())

        return "\n".join(parts)

    def _get_library_summary(self) -> str:
        """Compact library: genre/: track1, track2, ..."""
        music_dir = self.config.library.music_path
        if not music_dir.exists():
            return "  (empty)"
        lines = []
        for genre_dir in sorted(music_dir.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            tracks = [f.stem[:40] for f in sorted(genre_dir.iterdir())
                      if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a')]
            if tracks:
                lines.append(f"  {genre_dir.name}/: {', '.join(tracks)}")
        return "\n".join(lines) if lines else "  (empty)"

    # ── Session persistence ───────────────────────────────────────────

    def _save_session(self):
        try:
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            PERSIST_FILE.write_text(json.dumps({
                "mood": self.mood,
                "tracks_played": self.tracks_played,
                "chat_history": [
                    {"user": u, "response": r} for u, r in self._chat_history
                ],
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
            # Restore conversation memory
            for entry in data.get("chat_history", []):
                if isinstance(entry, dict):
                    self._chat_history.append((entry.get("user", ""), entry.get("response", "")))
            log.info(f"Restored: mood={self.mood}, tracks={len(self.tracks_played)}, chat={len(self._chat_history)}")
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
            current = {"title": "", "bpm": 0, "key": "", "remaining": 0, "file_path": ""}

            if status:
                for dk in [1, 2]:
                    d = status.get(f"deck{dk}", {})
                    if d.get("playing"):
                        try:
                            tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2).json()
                            if tinfo and not tinfo.get("error"):
                                current["title"] = tinfo.get("title", "")
                                current["file_path"] = tinfo.get("file_path", "")
                        except Exception:
                            pass
                        current["bpm"] = d.get("bpm", 0)
                        current["key"] = d.get("key", 0)
                        current["remaining"] = d.get("remaining_seconds", 0)
                        current["position"] = d.get("position_seconds", 0)
                        current["duration"] = d.get("duration", 0)
                        current["file_bpm"] = d.get("file_bpm", 0)
                        current["deck"] = dk
                        break

            # Next track (idle deck)
            next_track = None
            if status:
                _, idle_dk = _active_idle_decks(status)
                d_idle = status.get(f"deck{idle_dk}", {})
                if d_idle.get("track_loaded"):
                    try:
                        tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{idle_dk}/track_info", timeout=2).json()
                        if tinfo and not tinfo.get("error"):
                            next_track = {"title": tinfo.get("title", ""), "deck": idle_dk,
                                          "file_path": tinfo.get("file_path", "")}
                    except Exception:
                        pass

            # Set info
            set_data = {}
            if self.current_set:
                s = self.current_set
                elapsed_secs = time.time() - s["started_at"]
                target_secs = s["target_duration"] * 60
                set_data = {
                    "id": s["id"],
                    "number": s.get("set_number", 0),
                    "title": s.get("title", ""),
                    "mood": s.get("mood", ""),
                    "genre": s.get("genre", ""),
                    "elapsed": elapsed_secs,
                    "remaining": max(0, target_secs - elapsed_secs),
                    "target_minutes": s["target_duration"],
                }

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

            phase = "idle"
            if status and (status.get("deck1", {}).get("playing") or status.get("deck2", {}).get("playing")):
                phase = "playing"

            STATE_FILE.write_text(json.dumps({
                "phase": phase,
                "mood": self.mood,
                "tracks_played": len(self.tracks_played),
                "current_track": current,
                "next_track": next_track,
                "set": set_data,
                "planner_status": "busy" if self._planner_busy else "idle",
                "planner_tracks_since": getattr(self, '_tracks_since_plan', 0),
                "agent_busy": self._agent_busy,
                "relay_enabled": self.config.relay.enabled,
                "relay_connected": hasattr(self, 'relay'),
                "recording": self._recording_active,
                "broadcasting": self._broadcast_active,
                "emergency_count": self._emergency_count,
                "last_command": self._last_command,
                "last_command_id": self._last_command_id,
                "last_command_result": self._last_result,
                "billing": billing_str,
                "sources": {
                    "youtube": self.config.sources.youtube,
                    "treta_originals": self.config.sources.treta_originals,
                },
                "producing": self._generation_status,
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
