"""Subagent spawning — create temporary ADK agents for focused tasks.

The Being can spawn short-lived agents with restricted tool sets.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("dj-treta")

# Registry of completed spawn results
_spawn_results: dict[str, dict] = {}
_spawn_lock = threading.Lock()

# Module-level event loop for spawned agents (separate from Being's loop)
_spawn_loop = asyncio.new_event_loop()
_spawn_thread = threading.Thread(target=_spawn_loop.run_forever, daemon=True)
_spawn_thread.start()

# Tool sets — map name to list of tool function names
_TOOL_SETS = {
    "research": ["search_music", "read_file", "recall_learnings", "list_library_tracks"],
    "analysis": ["analyze_track", "read_file", "list_library_tracks", "recall_learnings", "get_set_history"],
    "production": ["generate_track", "analyze_track", "list_library_tracks"],
    "introspection": ["read_file", "write_file", "save_learning", "recall_learnings"],
}


def _resolve_tools(tool_set: str) -> list:
    """Resolve tool set name to actual FunctionTool instances."""
    from google.adk.tools import FunctionTool
    from . import (
        search_music, read_file, recall_learnings, list_library_tracks,
        analyze_track, get_set_history, generate_track, write_file, save_learning,
    )

    tool_map = {
        "search_music": search_music,
        "read_file": read_file,
        "write_file": write_file,
        "recall_learnings": recall_learnings,
        "save_learning": save_learning,
        "list_library_tracks": list_library_tracks,
        "analyze_track": analyze_track,
        "get_set_history": get_set_history,
        "generate_track": generate_track,
    }

    names = _TOOL_SETS.get(tool_set, [])
    return [FunctionTool(func=tool_map[n]) for n in names if n in tool_map]


async def _run_spawn(task: str, tool_set: str, spawn_id: str):
    """Run a spawned agent asynchronously."""
    from google.adk.agents import LlmAgent
    from google.adk.apps.app import App
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from ..config import load_config

    config = load_config()
    model = LiteLlm(
        model=config.llm.model,
        api_key=config.llm.api_key,
        api_base=config.llm.api_base,
    )

    tools = _resolve_tools(tool_set)
    agent = LlmAgent(
        name=f"spawn_{spawn_id}",
        model=model,
        instruction=f"You are a temporary agent spawned by DJ Treta for a specific task. Complete the task and return results concisely.\n\nAvailable tool set: {tool_set}",
        tools=tools,
        description=f"Temporary {tool_set} agent",
    )

    app_name = f"spawn_{spawn_id}"
    app = App(name=app_name, root_agent=agent)
    session_service = InMemorySessionService()
    runner = Runner(app=app, session_service=session_service)
    session = await session_service.create_session(app_name=app_name, user_id="spawn")

    message = types.Content(role="user", parts=[types.Part(text=task)])
    result = ""
    async for event in runner.run_async(
        session_id=session.id, user_id="spawn", new_message=message
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    result += part.text

    return result


def spawn_agent(task: str, tool_set: str = "research", timeout_seconds: int = 120) -> str:
    """Spawn a temporary agent to work on a specific task.

    The agent gets a restricted set of tools and runs independently.

    Args:
        task: What the spawned agent should do. Be specific.
        tool_set: Which tools to give it. One of: research, analysis, production, introspection.
        timeout_seconds: Max time before giving up (default 120s).

    Returns:
        The spawned agent's response.
    """
    if tool_set not in _TOOL_SETS:
        return f"ERROR: Invalid tool_set '{tool_set}'. Must be one of: {list(_TOOL_SETS.keys())}"

    # Limit concurrent spawns
    with _spawn_lock:
        active = sum(1 for v in _spawn_results.values() if v.get("status") == "running")
        if active >= 3:
            return "ERROR: Too many active spawns (max 3). Wait for one to finish."

    spawn_id = f"{int(time.time())}"[-6:]
    log.info(f"Spawning {tool_set} agent [{spawn_id}]: {task[:80]}")

    with _spawn_lock:
        _spawn_results[spawn_id] = {"status": "running", "task": task, "started": time.time()}

    try:
        future = asyncio.run_coroutine_threadsafe(
            _run_spawn(task, tool_set, spawn_id), _spawn_loop
        )
        result = future.result(timeout=timeout_seconds)

        with _spawn_lock:
            _spawn_results[spawn_id] = {"status": "done", "result": result, "finished": time.time()}

        log.info(f"Spawn [{spawn_id}] complete: {result[:200]}")

        # Prune old results (keep last 10)
        with _spawn_lock:
            if len(_spawn_results) > 10:
                oldest = sorted(_spawn_results.keys())[:-10]
                for k in oldest:
                    del _spawn_results[k]

        return result

    except TimeoutError:
        with _spawn_lock:
            _spawn_results[spawn_id] = {"status": "timeout", "task": task}
        return f"Spawn timed out after {timeout_seconds}s. ID: {spawn_id}"
    except Exception as e:
        with _spawn_lock:
            _spawn_results[spawn_id] = {"status": "error", "error": str(e)}
        log.error(f"Spawn [{spawn_id}] error: {e}")
        return f"Spawn error: {type(e).__name__}: {e}"


def get_spawn_result(spawn_id: str) -> str:
    """Get the result of a previously spawned agent.

    Args:
        spawn_id: The ID from a previous spawn_agent() call.

    Returns:
        The result if complete, or status if still running.
    """
    with _spawn_lock:
        entry = _spawn_results.get(spawn_id)
    if not entry:
        return f"No spawn found with ID: {spawn_id}"
    if entry["status"] == "running":
        elapsed = time.time() - entry.get("started", 0)
        return f"Still running ({elapsed:.0f}s elapsed). Task: {entry.get('task', '?')[:80]}"
    if entry["status"] == "done":
        return entry.get("result", "No result")
    return f"Status: {entry['status']}. {entry.get('error', '')}"
