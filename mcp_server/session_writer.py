"""Safe writers for DJ Treta session state.

The Session singleton is owned by the agent daemon. We NEVER overwrite
session.json directly — that would race with the daemon's debounced flush.

Two safe channels exist:

1.  Command file at /tmp/dj-treta-command.json. The agent's heartbeat
    polls this file, executes the command, and deletes it. This is the
    same channel the TUI uses, so the semantics are proven.

2.  Narrow signal fields on the Session object (user_skip, library_need,
    deck_ownership). The agent polls these every tick and clears them
    after consuming. Direct writes to these fields in session.json are
    NOT safe either — the Session class debounces to disk, so our write
    can be clobbered. We therefore funnel signal writes through the
    command file too, using a dedicated "mcp_signal" command that
    lets the daemon set the field via its own Session handle.

Reads are always safe — session.json is atomic-renamed on flush, and
stale data (up to ~2s old) is acceptable for a monitoring/control surface.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from agent.runtime_paths import runtime_path

COMMAND_FILE = runtime_path("command.json")
# Resolve to the real repo's session.json (repo root = mcp_server/..), with an
# env override for VM/other deployments. The old hardcoded /mnt/data path made
# dj_session_state return empty on any non-VM host (e.g. the dev Mac).
SESSION_JSON = Path(
    os.environ.get("DJTRETA_SESSION_JSON")
    or (Path(__file__).resolve().parent.parent / ".beings" / "session.json")
)
STATE_JSON = runtime_path("state.json")


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def write_command(cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
    """Write a command file the agent picks up on its next heartbeat.

    Returns the command id, which callers can later match against
    state.last_command_id in /tmp/dj-treta-state.json to see the result.

    If a prior command file is still sitting unprocessed, we overwrite
    it — this matches TUI behaviour (latest command wins).
    """
    cmd_id = f"mcp-{uuid.uuid4().hex[:10]}-{int(time.time())}"
    payload = {"command": cmd, "args": args or {}, "id": cmd_id, "source": "mcp"}
    _atomic_write(COMMAND_FILE, payload)
    return cmd_id


def read_session_json() -> Dict[str, Any]:
    """Read the full session snapshot from disk. Returns {} if unavailable."""
    try:
        return json.loads(SESSION_JSON.read_text())
    except Exception:
        return {}


def read_state_json() -> Dict[str, Any]:
    """Read the agent's ephemeral /tmp state (current track, deck info)."""
    try:
        return json.loads(STATE_JSON.read_text())
    except Exception:
        return {}


def wait_for_command_result(cmd_id: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Poll /tmp/dj-treta-state.json until last_command_id matches cmd_id.

    Returns a dict with keys `processed` (bool), `result` (str), `elapsed`
    (float seconds). We don't block forever — the MCP client should stay
    responsive. A timeout does NOT mean failure; most commands are fire-
    and-forget and succeed asynchronously.
    """
    start = time.time()
    while (time.time() - start) < timeout:
        state = read_state_json()
        if state.get("last_command_id") == cmd_id:
            return {
                "processed": True,
                "result": state.get("last_result", ""),
                "elapsed": time.time() - start,
            }
        time.sleep(0.2)
    return {"processed": False, "result": "", "elapsed": time.time() - start}
