"""WebSocket server — real-time command/response + state channel for the TUI.

Single port (7779) speaks two URL paths so it mirrors the public relay
(``wss://dj.treta.life/ws/state`` + ``/ws/command``) one-to-one. The TUI
connects with the same code regardless of local vs remote — only the URL
differs.

Endpoints:
  ``/ws/state``    — read-only push channel. Server emits events:
                     ``state``, ``billing``, ``thinking``, ``log``,
                     ``transition_scheduled``, ``talk_response``, …
  ``/ws/command``  — bidirectional. Client sends commands, server replies.
  ``/`` (bare)     — legacy combined channel (kept for back-compat with
                     existing TUI builds and the web listener that opens a
                     bare ``ws://localhost:7779``). Both reads and writes.

Wire format (any endpoint):
  Client → {"type": "command", "id": "ID", "command": "talk", "args": {…}}
  Server → {"type": "response", "id": "ID", "result": "…"}
  Server → {"type": "event", "event": "state", "data": {…}}   (push)
"""

import asyncio
import json
import logging
import time
import threading
from pathlib import Path

import websockets
from websockets.asyncio.server import serve

log = logging.getLogger("dj-treta")

WS_PORT = 7779


class WSServerMixin:
    """WebSocket server for real-time client communication."""

    def _start_ws_server(self):
        """Start WebSocket server in its own thread with its own event loop."""
        # Two client sets keyed by endpoint role:
        #   _ws_clients           — receives push events (state/billing/…).
        #   _ws_command_clients   — kept separate for clarity but currently
        #                           also receives push events so a single
        #                           /ws/command socket can both write commands
        #                           and observe immediate state echoes.
        self._ws_clients: set = set()
        # Ring buffers for replay-on-connect. A fresh TUI subscriber gets
        # the last N thinking/log events so the user sees recent context
        # instead of an empty pane after restart. Cleared by a fresh
        # daemon launch (deque starts empty) or by the `clear_history`
        # WS command. State/billing/scheduled-transition/mixxx don't need
        # rings — those are last-value snapshots and we already replay the
        # latest snapshot on connect.
        from collections import deque as _deque
        self._thinking_history: _deque = _deque(maxlen=100)
        self._log_history: _deque = _deque(maxlen=100)
        self._ws_thread = threading.Thread(target=self._ws_run_thread, daemon=True)
        self._ws_thread.start()
        log.info(f"WebSocket server starting on ws://localhost:{WS_PORT}")
        # WS is the canonical protocol for daemon ↔ TUI. Start the Mixxx
        # proxy loop so the TUI never has to hit Mixxx HTTP directly.
        self._start_mixxx_proxy_loop()

    def _ws_run_thread(self):
        """Run WS server in a dedicated thread + event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_serve(loop))
        except Exception as e:
            log.error(f"WS server died: {e}")

    async def _http_process_request(self, connection, request):
        """Serve plain HTTP for the in-Mixxx QML Sarathi panel.

        The QML panel can't open a WebSocket (QtWebSockets isn't linked) but
        QML's XMLHttpRequest does HTTP GET fine. We answer two GET routes here
        (before the WS upgrade); everything else falls through to the normal
        WebSocket handshake by returning None.

          GET /http/state                       → current state.json
          GET /http/command?cmd=...&reason=...   → run a daemon command

        Commands are GET (not POST) on purpose: websockets' opening-handshake
        hook can read the request line + query but not a POST body. The
        command set here is tiny + side-effect-guarded, so query params are
        adequate. CORS is wide-open since this only ever binds to localhost.
        """
        try:
            from websockets.http11 import Response
            from websockets.datastructures import Headers
            from urllib.parse import urlsplit, parse_qs

            raw_path = getattr(request, "path", "/") or "/"
            parts = urlsplit(raw_path)
            route = parts.path.rstrip("/")

            def _json_response(status, payload):
                body = json.dumps(payload).encode("utf-8")
                headers = Headers([
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    ("Access-Control-Allow-Origin", "*"),
                    ("Cache-Control", "no-store"),
                ])
                return Response(status, "OK" if status == 200 else "ERR", headers, body)

            if route == "/http/state":
                from .runtime_paths import runtime_path
                sf = runtime_path("state.json")
                data = json.loads(sf.read_text()) if sf.exists() else {}
                return _json_response(200, data)

            if route == "/http/playlist":
                # The planner's live queue (session.playlist) — feeds the
                # "Planned" node of the DJ Treta library feature in Mixxx.
                from .session_state import get_session
                sess = get_session()
                pl = getattr(sess, "playlist", None) if sess else None
                tracks = []
                for t in (pl or {}).get("tracks", []):
                    p = t.get("path", "")
                    if not p:
                        continue
                    tracks.append({
                        "path": p,
                        "title": t.get("title", ""),
                        "bpm": t.get("bpm"),
                        "key_camelot": t.get("key_camelot", ""),
                    })
                return _json_response(200, {
                    "mood": (pl or {}).get("mood_snapshot", ""),
                    "reasoning": (pl or {}).get("reasoning_summary", ""),
                    "updated_at": getattr(sess, "playlist_updated_at", 0.0) if sess else 0.0,
                    "tracks": tracks,
                })

            if route == "/http/activity":
                # Treta's recent thinking + tool calls, for the chat's
                # visibility feed. From the in-memory ring (ts-stamped above).
                try:
                    n = int((parse_qs(parts.query).get("n", ["60"])[0]) or 60)
                    hist = list(getattr(self, "_thinking_history", []) or [])[-n:]
                except Exception:
                    hist = []
                return _json_response(200, {"activity": hist})

            if route == "/http/log":
                # Recent daemon log lines for the cockpit tabs (Activity / DJ /
                # Planner / Library / Issues — client filters by keyword/tag).
                out = []
                for e in list(getattr(self, "_log_history", []) or [])[-int((parse_qs(parts.query).get("n", ["120"])[0]) or 120):]:
                    if isinstance(e, dict):
                        out.append({"ts": e.get("ts"), "text": e.get("text", "")})
                    else:
                        out.append({"ts": None, "text": str(e)})
                return _json_response(200, {"log": out})

            if route == "/http/billing":
                from .runtime_paths import runtime_path
                bf = runtime_path("billing.json")
                data = {}
                try:
                    if bf.exists():
                        data = json.loads(bf.read_text())
                except Exception:
                    data = {}
                return _json_response(200, data)

            if route == "/http/reflections":
                from .session_state import get_session
                sess = get_session()
                refl = list(getattr(sess, "reflections", []) or []) if sess else []
                return _json_response(200, {"reflections": refl[-20:]})

            if route == "/http/tracklist":
                # Played tracks of the current live set (for the cockpit set
                # view): title + transition + 👍/👎. Energy lives in the tracks
                # table — omitted in v1.
                tracks = []
                set_title = ""
                try:
                    from .db import get_current_set, get_set_tracks, get_db
                    cs = get_current_set()
                    if cs:
                        set_title = cs.get("title", "")
                        sid = cs.get("id")
                        fb = {}
                        try:
                            _db = get_db()
                            for r in _db.execute(
                                    "SELECT track_title, feedback FROM feedback WHERE set_id=?",
                                    (sid,)).fetchall():
                                fb[r["track_title"]] = r["feedback"]
                            _db.close()
                        except Exception:
                            pass
                        for r in get_set_tracks(sid):
                            t = r.get("title", "")
                            tracks.append({
                                "title": t,
                                "transition": r.get("transition_type", ""),
                                "feedback": fb.get(t, ""),
                            })
                except Exception:
                    tracks = []
                return _json_response(200, {"set": set_title, "tracks": tracks})

            if route == "/http/chat":
                # Recent chat turns for the in-Mixxx chat window. JSONL-backed,
                # oldest→newest.
                try:
                    from .chat_persistence import load_recent_turns
                    n = int((parse_qs(parts.query).get("n", ["40"])[0]) or 40)
                    turns = [
                        {"ts": e.get("ts"), "role": e.get("role"), "content": e.get("content", "")}
                        for e in load_recent_turns(n=n, max_age_hours=72.0)
                    ]
                except Exception:
                    turns = []
                return _json_response(200, {"turns": turns})

            if route == "/http/talk":
                # Send a message to Treta from the Mixxx chat window. Threaded
                # (the talk command returns immediately); the reply lands in the
                # JSONL and surfaces on the next /http/chat poll.
                msg = (parse_qs(parts.query).get("msg", [""])[0] or "").strip()
                if not msg:
                    return _json_response(400, {"ok": False, "message": "missing msg"})
                try:
                    result = self._handle_command("talk", {"message": msg}, cmd_id="mixxx-chat")
                except Exception as exc:
                    return _json_response(500, {"ok": False, "message": str(exc)})
                return _json_response(200, {"ok": True, "status": str(result)})

            if route == "/http/command":
                qs = parse_qs(parts.query)
                cmd = (qs.get("cmd", [""])[0] or "").strip()
                if not cmd:
                    return _json_response(400, {"ok": False, "message": "missing cmd"})
                args = {}
                if "reason" in qs:
                    args["reason"] = qs["reason"][0]
                if "mode" in qs:
                    args["mode"] = qs["mode"][0]
                if "mood" in qs:
                    args["mood"] = qs["mood"][0]
                if "type" in qs:
                    args["type"] = qs["type"][0]
                if "suggestion_id" in qs:
                    args["suggestion_id"] = qs["suggestion_id"][0]
                try:
                    result = self._handle_command(cmd, args, cmd_id="qml")
                except Exception as exc:
                    return _json_response(500, {"ok": False, "message": str(exc)})
                return _json_response(200, {"ok": True, "result": str(result)})

            # Not an HTTP route we handle → proceed to WebSocket handshake.
            return None
        except Exception as exc:
            log.debug(f"http process_request failed (non-fatal): {exc}")
            return None

    async def _ws_serve(self, loop=None):
        """Run the WebSocket server."""
        try:
            async with serve(
                self._ws_handler, "localhost", WS_PORT,
                process_request=self._http_process_request,
            ):
                log.info(
                    f"WebSocket server listening on ws://localhost:{WS_PORT} "
                    f"(paths: /ws/state, /ws/command, /)"
                )
                await asyncio.Future()  # run forever
        except OSError as e:
            log.warning(f"WS bind failed (port {WS_PORT} in use?): {e}")

    async def _ws_handler(self, websocket):
        """Handle a single WebSocket connection.

        Routes by request path:
          /ws/state    → read-only push subscriber (no command processing)
          /ws/command  → command-driven (also receives push events)
          anything else (incl. bare /) → legacy combined behaviour
        """
        # ``websocket.request.path`` exposes the URL path (websockets ≥ 14).
        # Falls back gracefully on older versions / odd transports.
        try:
            path = getattr(websocket.request, "path", "/") or "/"
        except Exception:
            path = "/"
        # Strip query string for routing.
        path_no_query = path.split("?", 1)[0]

        read_only = path_no_query.rstrip("/") == "/ws/state"

        self._ws_clients.add(websocket)
        remote = websocket.remote_address
        log.info(f"WS client connected: {remote} path={path_no_query} readonly={read_only}")

        # On connect, push the latest state snapshot so a fresh subscriber
        # doesn't have to wait up-to-2s for the next periodic _write_state.
        # Also replay the thinking + log ring buffers so a TUI restart
        # doesn't lose recent context (cleared on `clear_history` command
        # or when the daemon itself restarts).
        try:
            from .runtime_paths import runtime_path
            sf = runtime_path("state.json")
            if sf.exists():
                snapshot = json.loads(sf.read_text())
                await websocket.send(json.dumps({
                    "type": "event", "event": "state", "data": snapshot
                }))
                bf = runtime_path("billing.json")
                if bf.exists():
                    await websocket.send(json.dumps({
                        "type": "event", "event": "billing",
                        "data": json.loads(bf.read_text()),
                    }))
                stf = runtime_path("scheduled-transition.json")
                if stf.exists():
                    await websocket.send(json.dumps({
                        "type": "event", "event": "transition_scheduled",
                        "data": json.loads(stf.read_text()),
                    }))
            # Replay thinking + log history as ordered events with replay=True.
            for entry in list(getattr(self, "_thinking_history", []) or []):
                await websocket.send(json.dumps({
                    "type": "event", "event": "thinking",
                    "data": entry, "replay": True,
                }))
            for entry in list(getattr(self, "_log_history", []) or []):
                await websocket.send(json.dumps({
                    "type": "event", "event": "log",
                    "data": entry, "replay": True,
                }))
        except Exception:
            pass

        try:
            async for raw in websocket:
                if read_only:
                    # /ws/state is push-only; ignore any client frames except ping.
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") == "ping":
                            await websocket.send(json.dumps({"type": "pong"}))
                    except Exception:
                        pass
                    continue
                try:
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")
                    msg_id = msg.get("id", "")

                    if msg_type == "command":
                        cmd = msg.get("command", "")
                        args = msg.get("args", {})
                        # Handle async commands (talk, etc) in a thread
                        if cmd == "talk":
                            asyncio.run_coroutine_threadsafe(
                                self._ws_handle_talk(websocket, msg_id, args),
                                self._loop,
                            )
                        elif cmd == "mixxx_proxy":
                            # Synchronous Mixxx HTTP proxy — keeps the TUI
                            # from talking to Mixxx HTTP directly.
                            try:
                                result = self._ws_handle_mixxx_proxy(args)
                                await websocket.send(json.dumps({
                                    "type": "response", "id": msg_id, "result": result
                                }))
                            except Exception as e:
                                await websocket.send(json.dumps({
                                    "type": "error", "id": msg_id, "error": str(e)
                                }))
                        elif cmd == "clear_history":
                            # User-driven flush of the thinking/log replay
                            # buffers. Doesn't affect connected TUIs' on-
                            # screen content; that's a client-side concern.
                            cleared = 0
                            if hasattr(self, "_thinking_history"):
                                cleared += len(self._thinking_history)
                                self._thinking_history.clear()
                            if hasattr(self, "_log_history"):
                                cleared += len(self._log_history)
                                self._log_history.clear()
                            await websocket.send(json.dumps({
                                "type": "response", "id": msg_id,
                                "result": {"ok": True, "cleared": cleared},
                            }))
                        else:
                            # Sync commands — run directly
                            try:
                                result = self._handle_command(cmd, args, msg_id)
                                # For async commands that return "processing..."
                                # we need to wait for the result
                                if result == "processing...":
                                    result = await self._ws_wait_result(msg_id, timeout=120)
                                await websocket.send(json.dumps({
                                    "type": "response", "id": msg_id, "result": result
                                }))
                            except Exception as e:
                                await websocket.send(json.dumps({
                                    "type": "error", "id": msg_id, "error": str(e)
                                }))

                    elif msg_type == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))

                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error", "error": "Invalid JSON"
                    }))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._ws_clients.discard(websocket)
            log.info(f"WS client disconnected: {remote}")

    async def _ws_handle_talk(self, websocket, msg_id: str, args: dict):
        """Handle talk command — runs Being agent, sends response when done."""
        message = args.get("message", "")
        readonly = args.get("readonly", False)
        if not message:
            await websocket.send(json.dumps({
                "type": "response", "id": msg_id, "result": "No message"
            }))
            return

        if not hasattr(self, 'being_agent') or not self.being_agent:
            await websocket.send(json.dumps({
                "type": "response", "id": msg_id, "result": "Brain not ready"
            }))
            return

        try:
            from .main import _get_status
            context = self._build_context(_get_status(self.config.mixxx.url))
            history = self._format_history()

            readonly_tag = ""
            if readonly:
                readonly_tag = (
                    "\n\nMODE: READONLY — this is a live web listener. "
                    "You can ONLY respond conversationally. Do NOT call set_dj_directive, "
                    "set_planner_directive, set_mood, or any control tools. "
                    "Just chat, share your thoughts on the music, describe the vibe.\n"
                )

            # Run Being agent (this blocks until response)
            result = await self._invoke_being_async(
                f"{context}\n\n{history}\n{readonly_tag}\n"
                f'The listener says: "{message}"\n\n'
                f"Respond naturally. Set directives only if they asked you to DO something.",
                max_calls=20 if not readonly else 5,
            )

            # Update conversation memory
            self._chat_history.append((message, result))
            if len(self._chat_history) > 10:
                self._chat_history = self._chat_history[-10:]

            await websocket.send(json.dumps({
                "type": "response", "id": msg_id, "result": result
            }))
            log.info(f"WS talk {'(readonly)' if readonly else ''}: {result[:200]}")

        except Exception as e:
            await websocket.send(json.dumps({
                "type": "error", "id": msg_id, "error": str(e)
            }))

    async def _ws_wait_result(self, cmd_id: str, timeout: int = 120) -> str:
        """Wait for an async command to complete by polling state."""
        start = time.time()
        while time.time() - start < timeout:
            if self._last_command_id == cmd_id and self._last_result != "processing...":
                return self._last_result
            await asyncio.sleep(0.5)
        return "Timeout waiting for response"

    def _ws_broadcast(self, event: str, data: dict):
        """Broadcast an event to all connected WebSocket clients.

        Side effect: thinking + log events are also appended to ring buffers
        so a fresh TUI subscriber gets recent history on connect (replayed
        with ``replay: True`` so the client can flag them visually).
        """
        # Capture history BEFORE the early-return for the no-clients case —
        # otherwise a daemon running with no live TUI for a while accumulates
        # nothing, and the next client to attach gets a blank pane.
        if event == "thinking" and hasattr(self, "_thinking_history"):
            # Stamp a timestamp so the in-Mixxx chat can interleave activity
            # (thinking + tool calls) with chat turns by time.
            if isinstance(data, dict) and "ts" not in data:
                data["ts"] = time.time()
            self._thinking_history.append(data)
        elif event == "log" and hasattr(self, "_log_history"):
            if isinstance(data, dict) and "ts" not in data:
                data["ts"] = time.time()
            self._log_history.append(data)

        if not hasattr(self, '_ws_clients') or not self._ws_clients:
            return
        msg = json.dumps({"type": "event", "event": event, "data": data})
        for ws in list(self._ws_clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send(msg), self._loop)
            except Exception:
                self._ws_clients.discard(ws)

    # ── Mixxx HTTP → WS proxy ─────────────────────────────────────────────
    # The TUI now consumes Mixxx state via WS only. The daemon polls Mixxx
    # at 5 Hz and broadcasts the response shape unchanged. Commands flow
    # the other direction via the `mixxx_proxy` /ws/command handler below.

    def _start_mixxx_proxy_loop(self):
        """Start a background thread that polls Mixxx and broadcasts /api/status
        + /api/live to WS subscribers at 5 Hz. Does nothing if no clients are
        connected (lazy fan-out)."""
        if getattr(self, "_mixxx_proxy_running", False):
            return
        self._mixxx_proxy_running = True
        threading.Thread(target=self._mixxx_proxy_loop, daemon=True).start()

    def _mixxx_proxy_loop(self):
        import httpx
        url = self.config.mixxx.url
        while not getattr(self, "_shutdown", False):
            try:
                # Skip the network round-trip if no one is subscribed.
                if hasattr(self, "_ws_clients") and self._ws_clients:
                    try:
                        s = httpx.get(f"{url}/api/status", timeout=1.0)
                        if s.status_code == 200:
                            self._ws_broadcast("mixxx_status", s.json())
                    except Exception:
                        pass
                    try:
                        l = httpx.get(f"{url}/api/live", timeout=1.0)
                        if l.status_code == 200:
                            self._ws_broadcast("mixxx_live", l.json())
                    except Exception:
                        pass
            except Exception as exc:
                log.debug(f"mixxx proxy loop tick error: {exc}")
            time.sleep(0.2)  # 5 Hz

    def _ws_handle_mixxx_proxy(self, args: dict) -> dict:
        """Synchronous proxy: forward an arbitrary Mixxx HTTP call.

        Args shape: {"path": "/api/load", "method": "POST", "data": {...}}
        Returns the parsed JSON response (or {error: ...}).
        """
        import httpx
        path = args.get("path") or ""
        method = (args.get("method") or "GET").upper()
        data = args.get("data") or None
        if not path.startswith("/"):
            return {"error": "path must start with /"}
        url = f"{self.config.mixxx.url}{path}"
        try:
            if method == "POST":
                r = httpx.post(url, json=data, timeout=3.0)
            else:
                r = httpx.get(url, timeout=3.0)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return {"text": r.text}
            return {"error": f"HTTP {r.status_code}", "text": r.text[:200]}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
