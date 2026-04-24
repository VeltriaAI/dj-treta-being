"""TUI state source abstraction — local files vs remote WebSocket.

The DJ Treta TUI historically reads state from two local files:
    /tmp/dj-treta-state.json           (mixxx + set + current/next track)
    ~/beings/dj-treta/.beings/session.json   (mood profile, playlist, history)

To allow the TUI (a human UI) to connect to a remote daemon (e.g. the VM
running live on dj.treta.life), we abstract "where state comes from" behind
StateSource.

Transport split:
    - AI agents (Himani, Claude Desktop) use MCP (mcp_server/*) — tools + SSE.
    - Human UIs (this TUI, the web listener) use WebSocket — real-time,
      public-readable state + authenticated command channel.

Two implementations:
    LocalFileStateSource        — reads the files directly (default, zero overhead)
    WebSocketRemoteStateSource  — runs a background asyncio thread that opens
                                  a persistent wss:// connection to the public
                                  state broadcaster and caches the latest frame.
                                  Writes go through a separate /ws/command
                                  endpoint (token-authenticated).

The TUI does not need to know the difference past calling read_state() /
read_session() and looking at .label / .connected.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


# ─── Local file paths (kept in sync with tui.py) ────────────────────────

STATE_FILE = Path("/tmp/dj-treta-state.json")
SESSION_FILE = Path.home() / "beings" / "dj-treta" / ".beings" / "session.json"


# ─── Base class ─────────────────────────────────────────────────────────

class StateSource:
    """Abstract state source. Subclasses implement read_state/read_session
    (and, for remote, send_command).

    Contract:
        read_state()   — returns dict shaped like /tmp/dj-treta-state.json, or None
        read_session() — returns dict shaped like .beings/session.json, or None
        label          — short human-readable "LOCAL" / "REMOTE host"
        connected      — True if the source is currently reachable
        status_detail  — short extra string ("" for local, host+state for remote)

    Write-side (remote only — local dispatch goes through the Being's
    command file as before):
        send_command(name, args) — dispatch a DJ command (talk, mood, skip,
                                   etc.) to the remote daemon. Returns a short
                                   result string. Raises if not connected.
    """

    label: str = "LOCAL"

    @property
    def connected(self) -> bool:
        return True

    @property
    def status_detail(self) -> str:
        return ""

    def read_state(self) -> dict | None:
        raise NotImplementedError

    def read_session(self) -> dict | None:
        raise NotImplementedError

    def send_command(self, name: str, args: dict | None = None, timeout: float = 10.0) -> str:
        """Dispatch a command to the daemon. Local source cannot — remote source overrides."""
        raise RuntimeError(
            f"send_command('{name}') not supported on {type(self).__name__}"
        )

    def call_tool(self, name: str, args: dict | None = None, timeout: float = 10.0) -> dict:
        """Compat shim: the TUI was originally written against an MCP SSE
        transport that exposed call_tool(). The current transport is WebSocket
        (see subclass) — so we delegate to send_command() and reshape the
        string response into the ``{"ok", "message"}`` dict the TUI expects.

        Subclasses that speak true JSON-RPC (MCP) can override this directly.
        """
        try:
            out = self.send_command(name, args, timeout=timeout)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        if isinstance(out, dict):
            return out
        return {"ok": True, "message": str(out) if out is not None else ""}

    def close(self) -> None:
        """Stop background work. Idempotent."""
        pass


# ─── Local file source (original behavior) ──────────────────────────────

class LocalFileStateSource(StateSource):
    """Reads state directly from /tmp/dj-treta-state.json and session.json.

    This is the original behavior — no network, no threads.
    """

    label = "LOCAL"

    @property
    def connected(self) -> bool:
        # Consider "connected" iff the state file exists (daemon is running).
        return STATE_FILE.exists()

    @property
    def status_detail(self) -> str:
        return "" if STATE_FILE.exists() else "daemon off"

    def read_state(self) -> dict | None:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
        return None

    def read_session(self) -> dict | None:
        try:
            if SESSION_FILE.exists():
                return json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
        return None


# ─── Remote WebSocket source ────────────────────────────────────────────

DEFAULT_REMOTE_WS_URL = "wss://dj.treta.life/ws/state"
DEFAULT_REMOTE_CMD_URL = "wss://dj.treta.life/ws/command"


class WebSocketRemoteStateSource(StateSource):
    """Streams live state from the public /ws/state broadcaster.

    Runs a background thread with its own asyncio loop. The thread opens a
    persistent WebSocket connection and listens for JSON frames pushed by the
    server at roughly 3 Hz. Each frame is stashed in ``_last_frame``; TUI reads
    from the cache so rendering never blocks on network.

    Writes are sent over a separate /ws/command WebSocket using a short-lived
    connection per call (token-authenticated via ?token=). This keeps the
    read socket simple and one-way.

    Backoff on disconnect: 1s → 2s → 5s → 10s → 30s → 30s ...
    """

    def __init__(
        self,
        url: str = DEFAULT_REMOTE_WS_URL,
        command_url: str | None = None,
        command_token: str | None = None,
        token: str | None = None,
    ):
        # ``token`` is an alias for ``command_token`` — kept for callers that
        # predate the split between the state socket (anonymous, read-only) and
        # the command socket (authenticated). tui.py passes token=.
        if command_token is None and token is not None:
            command_token = token
        self.url = url
        # Derive default command URL from the state URL (same host, /ws/command).
        if command_url is None:
            parsed = urlparse(url)
            cmd_parsed = parsed._replace(path="/ws/command", query="", fragment="")
            command_url = urlunparse(cmd_parsed)
        self.command_url = command_url
        self.command_token = command_token or os.environ.get("DJTRETA_COMMAND_TOKEN") or os.environ.get("DJTRETA_RELAY_TOKEN") or ""

        parsed = urlparse(url)
        self._host = parsed.hostname or url
        if parsed.port:
            self._host = f"{self._host}:{parsed.port}"

        # caches — TUI reads these (thread-safe: reference-replaced only)
        self._last_frame: dict | None = None
        self._last_ok_ts: float = 0.0

        # thread control
        self._shutdown = False
        self._connected = False
        self._last_error: str = "connecting..."
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    # ── public API (called on TUI thread) ──

    @property
    def label(self) -> str:
        return f"REMOTE {self._host}"

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status_detail(self) -> str:
        if self._connected:
            age = time.time() - self._last_ok_ts if self._last_ok_ts else 999
            if age > 10:
                return f"stale {int(age)}s"
            return ""
        return self._last_error or "disconnected"

    def read_state(self) -> dict | None:
        """Return state.json-shaped dict synthesized from the last WS frame."""
        if not self._last_frame:
            return None
        return _ws_frame_to_state(self._last_frame)

    def read_session(self) -> dict | None:
        """WS /ws/state doesn't push session.json — return a minimal synthesised
        version so session-driven widgets don't blank out. Full planner history
        isn't available in remote mode (would need /ws/session, future work).
        """
        if not self._last_frame:
            return None
        return _ws_frame_to_session(self._last_frame)

    def send_command(self, name: str, args: dict | None = None, timeout: float = 10.0) -> str:
        """Dispatch a DJ command over /ws/command. Short-lived WS: open,
        auth via ?token=, send one JSON payload, await one response, close.
        """
        if self._loop is None:
            raise RuntimeError("remote not connected (loop missing)")
        if not self.command_token:
            raise RuntimeError(
                "No command token configured. Set DJTRETA_COMMAND_TOKEN "
                "(or DJTRETA_RELAY_TOKEN) env var."
            )

        payload = {"command": name, "args": args or {}}

        async def _do_send():
            # Local import — only the remote thread path needs websockets.
            import websockets

            sep = "&" if "?" in self.command_url else "?"
            url = f"{self.command_url}{sep}token={self.command_token}"
            async with websockets.connect(url, open_timeout=timeout, close_timeout=2) as ws:
                await ws.send(json.dumps(payload))
                # Server responds with {ok: bool, result: str} or {error: ...}
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                try:
                    resp = json.loads(raw)
                except Exception:
                    return raw
                if isinstance(resp, dict):
                    if resp.get("ok") is False:
                        return f"[error] {resp.get('error') or resp.get('result') or 'unknown error'}"
                    return resp.get("result") or resp.get("message") or "ok"
                return str(resp)

        fut = asyncio.run_coroutine_threadsafe(_do_send(), self._loop)
        try:
            return fut.result(timeout=timeout + 2)
        except asyncio.TimeoutError:
            raise RuntimeError(f"send_command('{name}') timed out")

    def close(self) -> None:
        self._shutdown = True
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

    # ── background thread ──

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:
            self._last_error = f"fatal: {exc}"
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main_loop(self):
        backoffs = [1, 2, 5, 10, 30]
        attempt = 0
        while not self._shutdown:
            try:
                await self._session_run()
                attempt = 0  # reset after a clean session
            except Exception as exc:
                self._last_error = str(exc)[:80] or type(exc).__name__
            self._connected = False
            if self._shutdown:
                break
            delay = backoffs[min(attempt, len(backoffs) - 1)]
            attempt += 1
            for _ in range(int(delay * 10)):
                if self._shutdown:
                    return
                await asyncio.sleep(0.1)

    async def _session_run(self):
        """One WebSocket session — connect, read frames until failure."""
        # Local import — only remote users need websockets on the import path.
        import websockets

        async with websockets.connect(
            self.url,
            open_timeout=10.0,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=2,
        ) as ws:
            self._connected = True
            self._last_error = ""
            while not self._shutdown:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if isinstance(frame, dict):
                    self._last_frame = frame
                    self._last_ok_ts = time.time()


# ─── helpers ────────────────────────────────────────────────────────────

def _ws_frame_to_state(frame: dict) -> dict:
    """Map /ws/state (camelCase, flat) back to the /tmp/dj-treta-state.json
    schema the TUI already knows how to render.

    Public WS frame shape (observed 2026-04-23):
      { phase, activeDeck, currentTrack: {title, artist, bpm, key, energy,
        duration, elapsed, remaining}, nextTrack, mood, decks: {deck1, deck2},
        crossfader, vu, brain, history, perception, harmonicMap, finishedSet, ... }

    Local state.json shape (snake_case):
      { phase, mood, tracks_played, current_track: {title, artist, bpm, key,
        energy, duration, position, remaining, deck, file_path}, next_track,
        set: {...}, user_intent, planner_directive, dj_directive, ... }
    """
    def _track(src: dict | None, deck_num: int | None) -> dict | None:
        if not src:
            return None
        pos = src.get("elapsed")
        dur = src.get("duration") or 0
        out = {
            "title": src.get("title") or "",
            "artist": src.get("artist") or "",
            "bpm": src.get("bpm") or 0,
            "file_bpm": src.get("bpm") or 0,
            "key": src.get("key") or "",
            "energy": src.get("energy") or 0,
            "duration": dur,
            "position": pos if pos is not None else 0,
            "remaining": src.get("remaining") if src.get("remaining") is not None else max(0, dur - (pos or 0)),
        }
        if deck_num is not None:
            out["deck"] = deck_num
        return out

    active = frame.get("activeDeck")
    current = _track(frame.get("currentTrack"), active)
    nxt_deck = None
    if active is not None:
        nxt_deck = 2 if active == 1 else 1
    next_track = _track(frame.get("nextTrack"), nxt_deck)

    brain = frame.get("brain") or {}
    set_info = frame.get("set") or {}
    history = frame.get("history") or []

    state: dict[str, Any] = {
        "phase": frame.get("phase") or "?",
        "mood": frame.get("mood") or "",
        "tracks_played": len(history),
        "current_track": current or {},
        "next_track": next_track,
        "set": {
            "mood": frame.get("mood") or "",
            "tracks_played": len(history),
            **set_info,
        },
        "user_intent": brain.get("currentIntent") or "",
        "planner_directive": brain.get("transitionAnalysis") or "",
        "dj_directive": brain.get("lastDecision") or "",
        # Surfaced raw frame for widgets that want camelCase (decks, vu).
        "_ws_frame": frame,
    }

    # history mapping — local schema uses tracks_played under "set"
    if history:
        state["set"]["history"] = [
            {
                "title": h.get("title", ""),
                "artist": h.get("artist", ""),
                "played_at": h.get("playedAt", ""),
                "energy": h.get("energy", 0),
            }
            for h in history
        ]

    return state


def _ws_frame_to_session(frame: dict) -> dict:
    """Minimal session.json synthesis. The remote server doesn't expose the
    daemon's full session (planner playlist, directives, feedback history)
    over /ws/state. We populate just enough so the TUI doesn't crash on
    missing keys.
    """
    history = frame.get("history") or []
    current = frame.get("currentTrack") or {}
    return {
        "mood": frame.get("mood") or "",
        "phase": frame.get("phase") or "?",
        "current_track": current,
        "tracks_played": [
            {
                "title": h.get("title", ""),
                "artist": h.get("artist", ""),
                "played_at": h.get("playedAt", ""),
            }
            for h in history
        ],
        "planner": {},
        "directives": [],
        "feedback_history": [],
    }


# ─── Backwards-compat alias ─────────────────────────────────────────────
# tui.py imports MCPRemoteStateSource under that name. Keep it pointed at
# the WebSocket impl so any stale import paths still resolve to the correct
# transport. (Deprecated — will be removed once TUI is fully migrated.)
MCPRemoteStateSource = WebSocketRemoteStateSource
