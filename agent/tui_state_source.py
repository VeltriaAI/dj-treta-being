"""TUI state source abstraction — local files vs remote MCP SSE.

The DJ Treta TUI historically reads state from two local files:
    /tmp/dj-treta-state.json           (mixxx + set + current/next track)
    ~/beings/dj-treta/.beings/session.json   (mood profile, playlist, history)

To allow the TUI to connect to a remote daemon (e.g. the VM running live on
dj.treta.life), we abstract "where state comes from" behind StateSource.

Two implementations:
    LocalFileStateSource    — reads the files directly (default, zero overhead)
    MCPRemoteStateSource    — runs a background asyncio thread that opens an
                              MCP SSE session to a remote server and polls the
                              dj_status / dj_session_state / dj_playlist tools
                              into caches. The TUI then reads those caches.

The TUI does not need to know the difference past calling read_state() /
read_session() and looking at .label / .connected.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ─── Local file paths (kept in sync with tui.py) ────────────────────────

STATE_FILE = Path("/tmp/dj-treta-state.json")
SESSION_FILE = Path.home() / "beings" / "dj-treta" / ".beings" / "session.json"


# ─── Base class ─────────────────────────────────────────────────────────

class StateSource:
    """Abstract state source. Subclasses implement read_state/read_session
    (and, for remote, call_tool).

    Contract:
        read_state()   — returns dict shaped like /tmp/dj-treta-state.json, or None
        read_session() — returns dict shaped like .beings/session.json, or None
        label          — short human-readable "LOCAL" / "REMOTE host"
        connected      — True if the source is currently reachable
        status_detail  — short extra string ("" for local, host+state for remote)

    Write-side (remote only — local dispatch goes through the Being's
    command file as before):
        call_tool(name, args) — invoke a named MCP tool synchronously,
                                returning the parsed JSON payload. Raises
                                RuntimeError if not connected.
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

    def call_tool(self, name: str, args: dict | None = None, timeout: float = 30.0) -> dict:
        """Invoke an MCP tool. Local source cannot — remote source overrides."""
        raise RuntimeError(
            f"call_tool('{name}') not supported on {type(self).__name__}"
        )

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


# ─── Remote MCP SSE source ──────────────────────────────────────────────

class MCPRemoteStateSource(StateSource):
    """Polls a remote DJ Treta MCP server over SSE.

    Runs a background thread with its own asyncio loop. The thread opens an
    SSE session, initializes MCP, and every ``poll_interval`` seconds calls
    the dj_status, dj_session_state and dj_playlist tools. Results are parsed
    and cached. TUI reads from the caches — never blocks on network.

    Backoff on disconnect: 1s → 2s → 5s → 10s → 30s → 30s ...
    """

    def __init__(
        self,
        url: str,
        token: str,
        poll_interval: float = 1.5,
    ):
        self.url = url
        self.token = token
        self.poll_interval = poll_interval

        parsed = urlparse(url)
        self._host = parsed.hostname or url

        # caches — TUI reads these (thread-safe: only replaced, not mutated)
        self._status_cache: dict | None = None
        self._session_cache: dict | None = None
        self._playlist_cache: dict | None = None
        self._last_ok_ts: float = 0.0

        # thread control
        self._shutdown = False
        self._connected = False
        self._last_error: str = "connecting..."
        self._loop: asyncio.AbstractEventLoop | None = None
        # Active ClientSession is only valid on the background loop; we
        # keep a reference so call_tool can dispatch cross-thread.
        self._active_session = None
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
        """Return state.json-shaped dict synthesized from dj_status (+ playlist)."""
        if not self._status_cache:
            return None
        return _status_to_state(self._status_cache, self._playlist_cache)

    def read_session(self) -> dict | None:
        return self._session_cache

    def call_tool(self, name: str, args: dict | None = None, timeout: float = 30.0) -> dict:
        """Invoke an MCP tool synchronously from the TUI thread. Blocks until
        the background loop returns the parsed JSON payload. Raises if not
        connected or the call fails.
        """
        if not self._connected or self._active_session is None or self._loop is None:
            raise RuntimeError("remote not connected")
        args = args or {}

        async def _do_call():
            result = await self._active_session.call_tool(name, arguments=args)
            return _extract_tool_json(result) or {}

        fut = asyncio.run_coroutine_threadsafe(_do_call(), self._loop)
        return fut.result(timeout=timeout)

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
                self._last_error = str(exc)[:80]
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
        """One MCP SSE session — connect, init, poll in a loop until failure."""
        # Import inside to keep module import cheap for local-only users.
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        headers = {"Authorization": f"Bearer {self.token}"}

        async with sse_client(
            url=self.url,
            headers=headers,
            timeout=10.0,
            sse_read_timeout=60.0,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                self._active_session = session
                self._connected = True
                self._last_error = ""
                try:
                    while not self._shutdown:
                        await self._poll_once(session)
                        # Short sleep loop so shutdown reacts quickly.
                        slept = 0.0
                        while slept < self.poll_interval and not self._shutdown:
                            await asyncio.sleep(0.1)
                            slept += 0.1
                finally:
                    self._active_session = None

    async def _poll_once(self, session):
        """Call the read-only tools and update caches."""
        try:
            status_result = await session.call_tool("dj_status", arguments={})
            session_result = await session.call_tool("dj_session_state", arguments={})
            playlist_result = await session.call_tool("dj_playlist", arguments={})
        except Exception as exc:
            self._last_error = str(exc)[:80]
            raise

        status = _extract_tool_json(status_result)
        session_json = _extract_tool_json(session_result)
        playlist = _extract_tool_json(playlist_result)

        if status is not None:
            self._status_cache = status
        if session_json is not None:
            self._session_cache = session_json
        if playlist is not None:
            self._playlist_cache = playlist
        self._last_ok_ts = time.time()


# ─── helpers ────────────────────────────────────────────────────────────

def _extract_tool_json(tool_result) -> dict | None:
    """MCP tool results are wrapped. Extract the JSON payload (dict)."""
    if tool_result is None:
        return None
    # FastMCP returns CallToolResult with .content = [TextContent(text=...), ...]
    # and .structuredContent for structured JSON.
    structured = getattr(tool_result, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP wraps dict results under "result"
        if "result" in structured and isinstance(structured["result"], dict):
            return structured["result"]
        return structured

    content = getattr(tool_result, "content", None)
    if content:
        for item in content:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except Exception:
                    pass
    return None


def _status_to_state(status: dict, playlist: dict | None) -> dict:
    """Map dj_status output back to the /tmp/dj-treta-state.json schema the TUI
    already knows how to render.

    dj_status is nearly identical (phase, mood, current_track, next_track,
    set), we just fill a few derived / missing fields.
    """
    out: dict[str, Any] = {
        "phase": status.get("phase") or "?",
        "mood": status.get("mood") or "",
        "tracks_played": status.get("tracks_played") or 0,
        "current_track": status.get("current_track") or {},
        "next_track": status.get("next_track"),
        "set": status.get("set") or {},
        "last_command_id": status.get("last_command_id"),
        "last_command_result": status.get("last_result"),
        "user_intent": status.get("user_intent") or "",
        "planner_directive": status.get("planner_directive") or "",
        "dj_directive": status.get("dj_directive") or "",
    }
    # Playlist depth may be useful to the BrainWidget in the future; not
    # rendered today but parity is cheap.
    if playlist:
        out["playlist_depth"] = playlist.get("count")
    return out
