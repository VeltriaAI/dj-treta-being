"""TUI state source abstraction — WebSocket-only.

The DJ Treta TUI used to read state from local disk files
(/tmp/dj-treta-state.json, billing.json, thinking.log, …) when running
against the local daemon, and from a public WebSocket relay when running
against the VM. That split caused two code paths, two shapes, and a
translator between them.

Now: the TUI ALWAYS speaks WebSocket. Default URL is
``ws://localhost:7779/ws/state`` (same machine — points at the daemon's
own ws_server). With ``--remote`` the URL becomes
``wss://dj.treta.life/ws/state`` (or whatever ``relay.server_url``
resolves to). Same code path, only the URL differs.

Local daemon still writes ``state.json`` etc. to disk for offline
debugging and other tools, but this module no longer reads them.

Frame envelopes accepted (so the same source works against both
``ws_server.py`` and the public relay):

  1. ``ws_server.py`` (local daemon):
       {"type": "event", "event": "state"|"billing"|"thinking"|"log"|
                                   "transition_scheduled"|"talk_response",
        "data": {…}}
     Data for ``state`` is the snake_case ``state.json`` shape
     (so no remapping needed when consumers want state.json keys).

  2. Public relay (``wss://dj.treta.life/ws/state``):
       Raw, no envelope, camelCase frame from ``agent/relay.py``.
     The source detects this shape (no ``type`` field, but ``phase`` /
     ``activeDeck`` keys present) and caches a translated state.

Transport split (unchanged):
    - AI agents (Himani, Claude Desktop) use MCP (mcp_server/*) — tools + SSE.
    - Human UIs (this TUI, the web listener) use WebSocket — public-readable
      state + token-authenticated command channel.

The TUI calls read_state(), read_billing(), iter_new_thinking(), etc.,
plus send_command() for writes.
"""
from __future__ import annotations

import asyncio
import json
import uuid
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from .runtime_paths import runtime_path

# ─── Local file paths (kept for legacy reference — TUI no longer reads) ─

STATE_FILE = runtime_path("state.json")
SESSION_FILE = Path.home() / "beings" / "dj-treta" / ".beings" / "session.json"


# ─── Base class ─────────────────────────────────────────────────────────

class StateSource:
    """Abstract state source. The only concrete implementation is
    ``WebSocketRemoteStateSource`` — the TUI always speaks WebSocket.

    Contract:
        read_state()        — returns dict in /tmp/dj-treta-state.json shape, or None
        read_session()      — returns dict in .beings/session.json shape, or None
                              (best-effort — full planner state isn't on /ws/state)
        read_billing()      — returns billing.json-shaped dict, or None
        read_scheduled_transition() — current scheduled transition or None
        drain_thinking()    — pop accumulated [THINK]/[CALL] events as a list
        drain_logs()        — pop accumulated log lines as a list of strings
        label               — short human-readable "LOCAL ws://…" / "REMOTE host"
        connected           — True if the source is currently reachable
        status_detail       — short extra string

    Write-side:
        send_command(name, args) — dispatch a DJ command (talk, mood, skip,
                                   etc.) to the daemon. Returns a short result
                                   string. Raises if not connected.
    """

    label: str = "WS"

    @property
    def connected(self) -> bool:
        return False

    @property
    def status_detail(self) -> str:
        return ""

    def read_state(self) -> dict | None:
        raise NotImplementedError

    def read_session(self) -> dict | None:
        raise NotImplementedError

    def read_billing(self) -> dict | None:
        return None

    def read_scheduled_transition(self) -> dict | None:
        return None

    def drain_thinking(self) -> list[dict]:
        """Return and clear accumulated thinking events ([THINK]/[CALL])."""
        return []

    def drain_logs(self) -> list[str]:
        """Return and clear accumulated log lines."""
        return []

    def send_command(self, name: str, args: dict | None = None, timeout: float = 10.0) -> str:
        raise RuntimeError(
            f"send_command('{name}') not supported on {type(self).__name__}"
        )

    def call_tool(self, name: str, args: dict | None = None, timeout: float = 10.0) -> dict:
        """Compat shim: TUI/legacy code expects ``{"ok", "message"}``."""
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


# ─── Remote WebSocket source ────────────────────────────────────────────


def _read_token_file() -> str:
    """Read ~/.config/dj-treta/token if present (chmod 600 expected)."""
    try:
        p = Path.home() / ".config" / "dj-treta" / "token"
        if p.exists():
            return p.read_text().strip()
    except Exception:
        pass
    return ""


DEFAULT_LOCAL_WS_URL = "ws://localhost:7779/ws/state"
# Remote points at the daemon's own WS server via nginx proxy
# (/ws/agent/* → 127.0.0.1:7779 on the VM). Token-gated at the nginx
# layer; same event protocol as local — full state + thinking + log +
# transition + billing + talk_response. The legacy /ws/state on the
# relay container is state-only and stays public for the web listener
# page.
DEFAULT_REMOTE_WS_URL = "wss://dj.treta.life/ws/agent/state"
DEFAULT_REMOTE_CMD_URL = "wss://dj.treta.life/ws/agent/command"


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
        url: str = DEFAULT_LOCAL_WS_URL,
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
        # Derive default command URL from the state URL by swapping the last
        # path segment from /state → /command. Preserves any prefix —
        # critical for the /ws/agent/* path where the old hardcoded
        # /ws/command would land on the relay container (different auth
        # token) and return 403 to a TUI carrying the daemon-proxy token.
        if command_url is None:
            parsed = urlparse(url)
            state_path = parsed.path
            if state_path.endswith("/state"):
                cmd_path = state_path[: -len("/state")] + "/command"
            else:
                cmd_path = "/ws/command"  # legacy / non-standard URLs
            cmd_parsed = parsed._replace(path=cmd_path, query="", fragment="")
            command_url = urlunparse(cmd_parsed)
        self.command_url = command_url
        # Token resolution order:
        #   1. Explicit constructor arg (CLI --token / direct caller)
        #   2. DJTRETA_REMOTE_TOKEN env (new, canonical name for /ws/agent/*)
        #   3. ~/.config/dj-treta/token  (chmod-600 file)
        #   4. Legacy DJTRETA_COMMAND_TOKEN / DJTRETA_RELAY_TOKEN env vars
        #      (kept for back-compat with the old relay's /ws/command)
        self.command_token = (
            command_token
            or os.environ.get("DJTRETA_REMOTE_TOKEN")
            or _read_token_file()
            or os.environ.get("DJTRETA_COMMAND_TOKEN")
            or os.environ.get("DJTRETA_RELAY_TOKEN")
            or ""
        )

        parsed = urlparse(url)
        self._host = parsed.hostname or url
        if parsed.port:
            self._host = f"{self._host}:{parsed.port}"
        # Local mode = ws:// to localhost — distinguish in the badge so the
        # operator instantly knows which daemon they're driving.
        scheme = (parsed.scheme or "").lower()
        host_lower = (parsed.hostname or "").lower()
        self._is_local = scheme == "ws" and host_lower in ("localhost", "127.0.0.1", "::1")

        # caches — TUI reads these (thread-safe: reference-replaced only,
        # never mutated in-place from the WS thread).
        self._last_state: dict | None = None
        self._last_session: dict | None = None
        self._last_billing: dict | None = None
        self._last_scheduled_transition: dict | None = None
        # camelCase relay frame retained for widgets (decks/vu/harmonicMap)
        # that prefer the rich shape from agent/relay.py.
        self._last_relay_frame: dict | None = None
        self._last_ok_ts: float = 0.0

        # Streaming buffers — drained periodically by the TUI poll loops.
        # Bounded to prevent unbounded growth if the TUI ever stops draining.
        self._pending_thinking: deque = deque(maxlen=500)
        self._pending_logs: deque = deque(maxlen=500)
        self._pending_lock = threading.Lock()
        # Mixxx HTTP proxy caches (filled by mixxx_status / mixxx_live events).
        self._last_mixxx_status: dict | None = None
        self._last_mixxx_live: dict | None = None

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
        if self._is_local:
            return f"LOCAL {self._host}"
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
        """Return current state.json-shaped dict, or None if no frames yet."""
        return self._last_state

    def read_session(self) -> dict | None:
        """Return a session.json-ish dict if we've got any history.

        Local daemon doesn't push session.json over WS yet — we synthesise a
        minimal one from state so session-driven widgets don't blank out.
        """
        if self._last_session is not None:
            return self._last_session
        if self._last_state:
            return _state_to_session(self._last_state)
        if self._last_relay_frame:
            return _ws_frame_to_session(self._last_relay_frame)
        return None

    def read_billing(self) -> dict | None:
        return self._last_billing

    def read_mixxx_status(self) -> dict | None:
        """Return the latest /api/status snapshot proxied via WS, or None."""
        return self._last_mixxx_status

    def read_mixxx_live(self) -> dict | None:
        """Return the latest /api/live snapshot proxied via WS, or None."""
        return self._last_mixxx_live

    def mixxx_proxy(self, path: str, method: str = "GET", data: dict | None = None,
                    timeout: float = 5.0) -> dict | None:
        """Synchronous Mixxx HTTP call routed through /ws/command.

        Replaces direct httpx.get/post(MIXXX_URL/...) calls in the TUI so
        nothing communicates with Mixxx outside the daemon. Returns the
        Mixxx response JSON, or {"error": ...}.
        """
        try:
            raw = self.send_command(
                "mixxx_proxy",
                {"path": path, "method": method, "data": data},
                timeout=timeout,
            )
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        # send_command returns the result string OR a dict (we send dict via mixxx_proxy)
        # The server wraps the dict in {"result": <dict>}, send_command extracts result.
        if isinstance(raw, dict):
            return raw
        # send_command may stringify dict → parse back if possible
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("{"):
                try:
                    return json.loads(raw)
                except Exception:
                    return {"text": raw}
            return {"text": raw}
        return None

    def read_scheduled_transition(self) -> dict | None:
        # Prefer the dedicated cache; fall back to the field embedded in state.
        if self._last_scheduled_transition is not None:
            return self._last_scheduled_transition
        if self._last_state:
            return self._last_state.get("scheduled_transition")
        return None

    def drain_thinking(self) -> list[dict]:
        with self._pending_lock:
            out = list(self._pending_thinking)
            self._pending_thinking.clear()
        return out

    def drain_logs(self) -> list[str]:
        with self._pending_lock:
            out = list(self._pending_logs)
            self._pending_logs.clear()
        return out

    def send_command(self, name: str, args: dict | None = None, timeout: float = 10.0):
        """Dispatch a DJ command over /ws/command. Short-lived WS: open,
        auth via ?token=, send one JSON payload, await the matching response,
        close.

        Returns the parsed result — typically a dict (for mixxx_proxy and
        similar) or a string (for talk and other text commands).
        """
        if self._loop is None:
            raise RuntimeError("remote not connected (loop missing)")
        if not self.command_token:
            raise RuntimeError(
                "No command token configured. Set DJTRETA_COMMAND_TOKEN "
                "(or DJTRETA_RELAY_TOKEN) env var."
            )

        cmd_id = uuid.uuid4().hex
        payload = {"type": "command", "id": cmd_id, "command": name, "args": args or {}}

        async def _do_send():
            # Local import — only the remote thread path needs websockets.
            import websockets

            sep = "&" if "?" in self.command_url else "?"
            url = f"{self.command_url}{sep}token={self.command_token}"
            async with websockets.connect(url, open_timeout=timeout, close_timeout=2) as ws:
                await ws.send(json.dumps(payload))
                # The /ws/command socket also receives push events (state,
                # billing, etc.) on connect. Filter for our matching response
                # and ignore the rest.
                deadline = asyncio.get_event_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        raise RuntimeError(f"send_command('{name}') timed out waiting for response")
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    try:
                        resp = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(resp, dict):
                        continue
                    msg_type = resp.get("type")
                    # Skip push events sent on this socket.
                    if msg_type == "event":
                        continue
                    # Match our command id when present.
                    if resp.get("id") and resp.get("id") != cmd_id:
                        continue
                    if msg_type == "error":
                        return f"[error] {resp.get('error') or 'unknown error'}"
                    if resp.get("ok") is False:
                        return f"[error] {resp.get('error') or resp.get('result') or 'unknown error'}"
                    # Plain truthiness `or` chain breaks dicts/zero — be explicit.
                    if "result" in resp:
                        return resp["result"]
                    if "message" in resp:
                        return resp["message"]
                    return "ok"

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

    def _state_url_with_auth(self) -> str:
        """Return ``self.url`` with ``?token=`` appended when a token is set.

        The relay's public ``/ws/state`` ignores the token (read is unauthed
        there), and the local daemon doesn't check it either, so it's safe
        to always append. The new ``/ws/agent/*`` path REQUIRES it (nginx
        gate), so this is what makes the remote remote-full mode work.
        """
        if not self.command_token:
            return self.url
        parsed = urlparse(self.url)
        sep = "&" if parsed.query else "?"
        return f"{self.url}{sep}token={self.command_token}"

    async def _session_run(self):
        """One WebSocket session — connect, read frames until failure."""
        # Local import — only remote users need websockets on the import path.
        import websockets

        async with websockets.connect(
            self._state_url_with_auth(),
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
                    self._ingest_frame(frame)
                    self._last_ok_ts = time.time()

    def _ingest_frame(self, frame: dict) -> None:
        """Apply one incoming WS frame to the appropriate cache.

        Two envelope styles are supported:
          1. ``{"type":"event","event":"NAME","data":{…}}`` — the
             ws_server.py envelope (local daemon). Data shape matches the
             corresponding *.json file (snake_case) or, for thinking, the
             {agent,type,text|tool,args} dict from adk_runner.
          2. Raw camelCase frame from ``agent/relay.py`` (public relay) —
             no ``type`` field but ``phase`` / ``activeDeck`` / ``decks``
             present. Translated via ``_ws_frame_to_state``.
        """
        msg_type = frame.get("type")

        if msg_type == "event":
            evt = frame.get("event") or ""
            data = frame.get("data") or {}
            if evt == "state":
                self._last_state = data if isinstance(data, dict) else None
                # If the state payload carried a scheduled_transition, mirror it.
                if isinstance(data, dict):
                    sched = data.get("scheduled_transition")
                    if sched is not None:
                        self._last_scheduled_transition = sched
                    elif "scheduled_transition" in data:
                        # Explicit null clears it.
                        self._last_scheduled_transition = None
            elif evt == "billing":
                self._last_billing = data if isinstance(data, dict) else None
            elif evt == "transition_scheduled":
                self._last_scheduled_transition = data if isinstance(data, dict) else None
            elif evt == "mixxx_status":
                self._last_mixxx_status = data if isinstance(data, dict) else None
            elif evt == "mixxx_live":
                self._last_mixxx_live = data if isinstance(data, dict) else None
            elif evt == "thinking":
                if isinstance(data, dict):
                    # Carry the replay flag through so the TUI can render
                    # history entries differently from live events.
                    if frame.get("replay"):
                        data = {**data, "_replay": True}
                    with self._pending_lock:
                        self._pending_thinking.append(data)
            elif evt == "log":
                text = ""
                if isinstance(data, dict):
                    text = data.get("text") or ""
                elif isinstance(data, str):
                    text = data
                if text:
                    if frame.get("replay"):
                        text = f"[history] {text}"
                    with self._pending_lock:
                        self._pending_logs.append(text)
            elif evt == "talk_response":
                # Surface as a log line so the TUI's existing log handlers
                # display the chatter without a dedicated channel.
                if isinstance(data, dict):
                    msg = data.get("text") or data.get("result") or ""
                    if msg:
                        with self._pending_lock:
                            self._pending_logs.append(f"DJ Treta said: {msg[:300]}")
            return

        # Raw relay frame — produced by agent/relay.py against the public
        # endpoint (wss://dj.treta.life). Translate to local-shape state.
        if msg_type is None and ("phase" in frame or "activeDeck" in frame):
            self._last_relay_frame = frame
            self._last_state = _ws_frame_to_state(frame)
            self._last_session = _ws_frame_to_session(frame)
            # Public relay embeds scheduled transition under transition.scheduled
            try:
                tr = frame.get("transition") or {}
                sched = tr.get("scheduled")
                if sched is not None:
                    self._last_scheduled_transition = sched
            except Exception:
                pass


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
    """Minimal session.json synthesis from a relay (camelCase) frame."""
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


def _state_to_session(state: dict) -> dict:
    """Synthesise a session.json-ish dict from a snake_case state.json
    payload — local daemon doesn't push session.json directly."""
    history = (state.get("set") or {}).get("history") or []
    return {
        "mood": state.get("mood") or "",
        "phase": state.get("phase") or "?",
        "current_track": state.get("current_track") or {},
        "tracks_played": history,
        "planner": {},
        "directives": [],
        "feedback_history": [],
    }


# ─── Backwards-compat alias ─────────────────────────────────────────────
# Older imports (mcp_server, web listener) referenced this name; keep
# it pointed at the unified WebSocket source so external callers keep
# working without touching their imports. The previous LocalFile-based
# source has been removed — file mode no longer exists. If you see a
# NameError on the old name, switch to
#     WebSocketRemoteStateSource(url="ws://localhost:7779/ws/state")
# for local subscriptions.
MCPRemoteStateSource = WebSocketRemoteStateSource
