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
        self._ws_thread = threading.Thread(target=self._ws_run_thread, daemon=True)
        self._ws_thread.start()
        log.info(f"WebSocket server starting on ws://localhost:{WS_PORT}")

    def _ws_run_thread(self):
        """Run WS server in a dedicated thread + event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_serve(loop))
        except Exception as e:
            log.error(f"WS server died: {e}")

    async def _ws_serve(self, loop=None):
        """Run the WebSocket server."""
        try:
            async with serve(self._ws_handler, "localhost", WS_PORT):
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
        """Broadcast an event to all connected WebSocket clients."""
        if not hasattr(self, '_ws_clients') or not self._ws_clients:
            return
        msg = json.dumps({"type": "event", "event": event, "data": data})
        for ws in list(self._ws_clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send(msg), self._loop)
            except Exception:
                self._ws_clients.discard(ws)
