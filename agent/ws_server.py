"""WebSocket server — real-time command/response channel for MCP and other clients.

Replaces file-based IPC with bidirectional WebSocket on localhost:7779.

Protocol:
  Client sends: {"type": "command", "id": "unique-id", "command": "talk", "args": {"message": "hello"}}
  Server responds: {"type": "response", "id": "unique-id", "result": "..."}
  Server pushes: {"type": "event", "event": "track_change", "data": {...}}
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
        """Start WebSocket server on the Being's event loop."""
        self._ws_clients: set = set()
        asyncio.run_coroutine_threadsafe(self._ws_serve(), self._loop)
        log.info(f"WebSocket server starting on ws://localhost:{WS_PORT}")

    async def _ws_serve(self):
        """Run the WebSocket server."""
        async with serve(self._ws_handler, "localhost", WS_PORT):
            await asyncio.Future()  # run forever

    async def _ws_handler(self, websocket):
        """Handle a single WebSocket connection."""
        self._ws_clients.add(websocket)
        remote = websocket.remote_address
        log.info(f"WS client connected: {remote}")
        try:
            async for raw in websocket:
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
