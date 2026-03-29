"""DJ Treta v3.0 — DJClaw

The Being starts, stays alive, and decides everything.
No watchdog. No state machine. No deterministic DJ logic.
Just an agent with a heartbeat — she sees reality and acts.

Usage:
    python -m agent              # Start Being
    djtreta start                # Same, via CLI
    djclaw start                 # Same, via DJClaw CLI
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

from .config import load_config, Config
from .agents import create_dj_agent, create_planner_agent

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

        # Reset billing + playlist for fresh session
        Path("/tmp/dj-treta-billing.json").unlink(missing_ok=True)
        PLAYLIST_FILE.unlink(missing_ok=True)

        # Init SQLite DB + scan library
        from .db import init_db, scan_library
        init_db()
        scan_library(self.config.library.music_path)

        _ensure_litellm(self.config)
        _ensure_mixxx(self.config)
        self._restore_session()

        log.info("Creating DJ agent...")
        self.agent = create_dj_agent(self.config)
        log.info("Creating planner agent...")
        self.planner_agent = create_planner_agent(self.config)

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

        # Get mix_out + timeline from DB for active track
        mix_out = None
        in_safe_section = True  # assume safe unless timeline says otherwise
        if playing:
            try:
                from .db import get_track_by_path
                import json as _json
                tinfo = httpx.get(
                    f"{self.config.mixxx.url}/api/deck/{active_deck}/track_info", timeout=2
                ).json()
                meta = get_track_by_path(tinfo.get("file_path", ""))
                if meta:
                    mix_out = float(meta.get("mix_out_seconds") or 0)
                    # Check timeline: what section are we in RIGHT NOW?
                    timeline_str = meta.get("timeline", "")
                    if timeline_str:
                        try:
                            timeline = _json.loads(timeline_str) if isinstance(timeline_str, str) else timeline_str
                            for section in timeline:
                                start = float(section.get("start", 0))
                                end = float(section.get("end", 0))
                                if start <= position <= end:
                                    name = section.get("section", "").lower()
                                    # Safe to transition: breakdown, outro, intro
                                    # NOT safe: drop, buildup, main theme
                                    if any(w in name for w in ["drop", "build", "main", "peak"]):
                                        in_safe_section = False
                                    break
                        except Exception:
                            pass
            except Exception:
                pass

        idle_ready = idle_loaded and idle_remaining > 60

        # How long has this track been playing on this deck? (wall clock)
        time_on_deck = time.time() - self._deck_start_time.get(active_deck, 0)
        duration = float(d_active.get("duration", 0) or 0)

        # === PRIORITY 2: Smart transition at mix_out (from DB timeline) ===
        # Only transition during safe sections (breakdown, outro) — NEVER during drops
        if idle_ready and mix_out and mix_out > 0 and position >= (mix_out - 55) and in_safe_section:
            log.info(f"Smart transition at mix_out! pos={position:.0f}s, mix_out={mix_out:.0f}s, on_deck={time_on_deck:.0f}s, safe_section=True")
            self._execute_transition(idle_deck, 45)
            return

        # === PRIORITY 3: Fallback transition (no DB data — use 60% guard) ===
        min_play = max(
            self.config.planner.min_play_time_seconds,
            duration * 0.6 if duration > 0 else 90
        )
        if idle_ready and remaining < 120 and time_on_deck > min_play:
            log.info(f"Fallback transition. remain={remaining:.0f}s, on_deck={time_on_deck:.0f}s")
            self._execute_transition(idle_deck, 45)
            return

        # === PRIORITY 4: Backup load — planner didn't load idle deck ===
        if not idle_loaded and position > 120 and playing:
            self._next_sleep = 10
            log.warning("Backup: loading idle deck (planner missed it)")
            self._load_next_on_idle(status)
            return

        # === Everything fine — dynamic sleep ===
        if mix_out and position < (mix_out - 55):
            time_until = mix_out - 55 - position
            self._next_sleep = min(15, max(5, time_until / 3))
        elif remaining > 120:
            self._next_sleep = min(15, max(5, remaining / 10))
        else:
            self._next_sleep = 5

        self._record_playing_tracks()
        self._check_set_duration()

    def _execute_transition(self, to_deck, duration):
        """Execute transition. Only called by heartbeat. Uses _agent_busy to prevent re-entry."""
        self._agent_busy = True

        def _run():
            try:
                from .tools import do_transition
                result = do_transition(to_deck, duration)
                log.info(f"Transition result: {str(result)[:100]}")
                self._record_playing_tracks()
            except Exception as e:
                log.error(f"Transition error: {e}")
            finally:
                self._agent_busy = False
        threading.Thread(target=_run, daemon=True).start()

    def _emergency_play(self):
        """Silence! Direct API play first (fast + reliable), agent fallback for empty library."""
        try:
            url = self.config.mixxx.url

            # Try direct API first — pick any track from library, load, play
            import glob
            tracks = glob.glob(str(self.config.library.music_path / "**/*.mp3"), recursive=True)
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

            # Empty library — agent searches + downloads
            context = self._build_context(_get_status(url))
            result = self.agent.run(
                f"{context}\n\n"
                f"SILENCE! Empty library. Search YouTube, download a melodic techno track, "
                f"load on deck 1, play it, set crossfader to 0.0."
            )
            log.info(f"Emergency play (agent): {str(result)[:200]}")
            self._record_playing_tracks()
        except Exception as e:
            log.error(f"Emergency play error: {e}")
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

        if not candidates:
            # Fallback: get ANY analyzed track not on either deck
            from .db import get_all_analyzed_tracks
            all_tracks = get_all_analyzed_tracks()
            candidates = [t for t in all_tracks
                          if t.get("path") not in exclude_paths
                          and t.get("title") not in played_titles]

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

                # Reset rate to original BPM + save duration
                try:
                    url = self.config.mixxx.url
                    time.sleep(1)
                    # Reset rate — play at original BPM, no pitch drift
                    httpx.post(f"{url}/api/control", json={
                        "group": f"[Channel{idle_deck}]", "key": "rate", "value": 0
                    }, timeout=3)
                    # Save duration from Mixxx (Gemini analysis often misses it)
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

        set_id = f"set-{time.strftime('%Y%m%d-%H%M')}"
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
            log.info("Broadcast stopped")
        except Exception as e:
            log.warning(f"Broadcast stop failed: {e}")

    # ── Self-evolution ─────────────────────────────────────────────────

    def _agent_reflect(self):
        """Periodic self-evolution — reflect on recent tracks."""
        try:
            recent = [t.get("title", "?") for t in self.tracks_played[-5:]]
            self.agent.run(
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
                log.warning(f"Planner loop error: {e}")
            time.sleep(30)

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
            current_info = (
                f"{current_track} | BPM:{current_meta.get('bpm',0):.0f} "
                f"Key:{current_meta.get('key_musical','?')} "
                f"Energy:{current_meta.get('energy_peak','?')}"
            )

        log.info(f"Planner running — current: {current_track or 'nothing'}, {len(candidates)} candidates in DB")
        result = str(self.planner_agent.run(
            f"Currently playing: {current_info}\n"
            f"Already played (DO NOT repeat): {played_list}\n\n"
            f"Tracks already in library:\n{candidate_text or '  (none)'}\n\n"
            f"Current mood/genre: {self.mood or 'melodic-techno'}. Search for THIS genre specifically.\n"
            f"ALWAYS search YouTube and download {self.config.planner.download_new_tracks} NEW '{self.mood or 'melodic-techno'}' tracks.\n"
            f"Search for different artists each time. Don't download what's already in library.\n"
            f"After downloading, analyze each new track.\n"
            f"Then pick the best next 3 tracks (mix of library + new downloads).\n"
            f"For each: title, full path, BPM, key, energy, why it fits."
        ))
        log.info(f"Planner done: {str(result)[:200]}")

        self._write_playlist(result, current_track)

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

    def _get_analysis_cache(self) -> str:
        """Read cached track analyses for planner context."""
        cache_dir = self.config.library.music_path / ".analysis"
        if not cache_dir.exists():
            return "(no analyses yet)"
        lines = []
        for f in sorted(cache_dir.glob("*.txt"))[:20]:
            content = f.read_text()[:200]
            lines.append(f"  {f.stem}: {content}")
        return "\n".join(lines) if lines else "(no analyses yet)"

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
        """One agent, one personality. Always."""
        try:
            context = self._build_context(_get_status(self.config.mixxx.url))
            history = self._format_history()

            with self._talk_lock:
                result = str(self.agent.run(
                    f"{context}\n\n{history}\n\n"
                    f'The listener says: "{message}"\n\n'
                    f"Respond naturally. Use tools only if they asked you to DO something."
                ))

            # Update conversation memory
            self._chat_history.append((message, result))
            if len(self._chat_history) > 10:
                self._chat_history = self._chat_history[-10:]

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
            status = _get_status(self.config.mixxx.url)
            context = self._build_context(status)
            active, idle = _active_idle_decks(status) if status else (1, 2)
            result = self.agent.run(
                f"{context}\n\n"
                f"ACTIVE deck: {active}, IDLE deck: {idle}. "
                f"SKIP NOW. Find a new track, load it on deck {idle}, "
                f"and do_transition quickly (20s). Go."
            )
            self._last_result = f"Skipped: {str(result)[:150]}"
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
            lines.append(f"DJ Treta: {response[:200]}")
        return "\n".join(lines)

    # ── Context from reality ──────────────────────────────────────────

    def _build_context(self, status):
        if not status:
            return "Mixxx not responding."

        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        parts = [f"Mood: {self.mood or 'not set'}"]
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
