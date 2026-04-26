"""ADK runner mixin — async agent invocation, billing, and event processing."""

import asyncio
import json
import logging
import time
from pathlib import Path
from .runtime_paths import runtime_path

log = logging.getLogger("dj-treta")

THINKING_FILE = runtime_path("thinking.log")
BILLING_FILE = runtime_path("billing.json")


class _CorruptionDetector(logging.Handler):
    """Detects 'Missing tool results' warnings from ADK/LiteLLM.
    Sets a flag so the caller can rotate the corrupted session."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.corrupted = False

    def reset(self):
        self.corrupted = False

    def emit(self, record):
        msg = record.getMessage().lower()
        if "missing tool results" in msg or "tool_call_id" in msg:
            self.corrupted = True


# Attach to root logger (LiteLLM logs at root level)
_corruption_detector = _CorruptionDetector()
logging.getLogger().addHandler(_corruption_detector)


class ADKRunnerMixin:

    def _run_async(self, coro, timeout=120):
        """Run async coroutine on the persistent event loop. Thread-safe."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"ADK agent call timed out after {timeout}s")

    async def _invoke_agent_async(self, instruction: str, max_calls: int = 10, fresh_session: bool = False) -> str:
        """Invoke DJ agent via ADK runner. Processes events for billing + thinking log."""
        from google.genai import types
        from google.adk.runners import RunConfig

        # Fresh session resets ADK routing back to root agent (dj_treta)
        # Without this, sticky routing sends heartbeats to mixer sub-agent
        if fresh_session:
            self._dj_session = await self._session_service.create_session(
                app_name="dj_treta", user_id="dj"
            )

        message = types.Content(role="user", parts=[types.Part(text=instruction)])
        run_config = RunConfig(max_llm_calls=max_calls)
        result = ""
        async for event in self._dj_runner.run_async(
            session_id=self._dj_session.id, user_id="dj",
            new_message=message, run_config=run_config,
        ):
            self._process_event(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        result += part.text
        return result

    def _invoke_agent(self, instruction: str, timeout: int = 90, max_calls: int = 10, fresh_session: bool = False) -> str:
        """Invoke DJ agent. Sync wrapper with lock. Auto-recovers on tool errors."""
        with self._agent_lock:
            try:
                return self._run_async(self._invoke_agent_async(instruction, max_calls, fresh_session), timeout=timeout)
            except (ValueError, RuntimeError) as e:
                if "not found" in str(e).lower() or "tool" in str(e).lower():
                    log.warning(f"ADK tool error — recreating DJ session: {e}")
                    self._recreate_dj_session()
                    return self._run_async(self._invoke_agent_async(instruction, max_calls), timeout=timeout)
                raise

    def _recreate_dj_session(self):
        """Recreate DJ session when ADK loses tool registry."""
        try:
            async def _reinit():
                self._dj_session = await self._session_service.create_session(
                    app_name="dj_treta", user_id="dj"
                )
            self._run_async(_reinit(), timeout=10)
            log.info("DJ session recreated")
        except Exception as e:
            log.error(f"Session recreation failed: {e}")

    async def _invoke_being_async(self, instruction: str, max_calls: int = 15) -> str:
        """Invoke Being agent — the brain. Handles conversation + directives."""
        from google.genai import types
        from google.adk.runners import RunConfig

        message = types.Content(role="user", parts=[types.Part(text=instruction)])
        run_config = RunConfig(max_llm_calls=max_calls)
        result = ""
        async for event in self._being_runner.run_async(
            session_id=self._being_session.id, user_id="listener",
            new_message=message, run_config=run_config,
        ):
            self._process_event(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        result += part.text
        return result

    def _invoke_being(self, instruction: str, timeout: int = 120, max_calls: int = 15) -> str:
        """Invoke Being agent. Sync wrapper. Being has its own session — no lock needed with DJ."""
        return self._run_async(self._invoke_being_async(instruction, max_calls), timeout=timeout)

    async def _invoke_planner_async(self, instruction: str) -> str:
        """Invoke planner agent via ADK runner. Fresh session each time to avoid stale tool_call_ids."""
        from google.genai import types

        # Fresh session per invocation — prevents "Missing tool results" from compaction
        self._planner_session = await self._session_service.create_session(
            app_name="dj_treta_planner", user_id="planner"
        )

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

    async def _invoke_library_async(self, instruction: str) -> str:
        """Invoke library agent via ADK runner. Fresh session per invocation.

        Library runs as a peer thread (v8 Phase 5) — takes `session.library_need`
        signals and fulfils them via search_music + download_track."""
        from google.genai import types

        self._library_session = await self._session_service.create_session(
            app_name="dj_treta_library", user_id="library"
        )
        message = types.Content(role="user", parts=[types.Part(text=instruction)])
        result = ""
        async for event in self._library_runner.run_async(
            session_id=self._library_session.id, user_id="library", new_message=message
        ):
            self._process_event(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        result += part.text
        return result

    def _invoke_library(self, instruction: str) -> str:
        """Invoke library agent. Sync wrapper — download can take a while."""
        return self._run_async(self._invoke_library_async(instruction), timeout=600)

    async def _invoke_producer_async(self, instruction: str) -> str:
        """Invoke producer agent (v8 Phase 6 peer). Fresh session per call."""
        from google.genai import types

        self._producer_session = await self._session_service.create_session(
            app_name="dj_treta_producer", user_id="producer"
        )
        message = types.Content(role="user", parts=[types.Part(text=instruction)])
        result = ""
        async for event in self._producer_runner.run_async(
            session_id=self._producer_session.id, user_id="producer", new_message=message
        ):
            self._process_event(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        result += part.text
        return result

    def _invoke_producer(self, instruction: str) -> str:
        """Invoke producer agent. Sync wrapper — Lyria 3 generation is slow."""
        return self._run_async(self._invoke_producer_async(instruction), timeout=900)

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
                            # Broadcast thinking via WebSocket
                            if hasattr(self, '_ws_broadcast'):
                                self._ws_broadcast("thinking", {
                                    "agent": agent_name,
                                    "type": "think",
                                    "text": text[:500],
                                })

            # Tool calls
            func_calls = event.get_function_calls()
            if func_calls:
                for fc in func_calls:
                    args_str = str(fc.args)[:200] if fc.args else ""
                    with open(THINKING_FILE, "a") as f:
                        f.write(f"[CALL:{agent_name}] {fc.name}({args_str})\n")
                    # Broadcast tool call via WebSocket
                    if hasattr(self, '_ws_broadcast'):
                        self._ws_broadcast("thinking", {
                            "agent": agent_name,
                            "type": "call",
                            "tool": fc.name,
                            "args": args_str,
                        })

            # Billing — usage_metadata
            if event.usage_metadata:
                um = event.usage_metadata
                inp = um.prompt_token_count or 0
                out = um.candidates_token_count or 0
                if inp > 0 or out > 0:
                    self._update_billing(agent_name, inp, out)
                    # v8 Phase 8: record structured llm_calls row
                    try:
                        from .observability import record_llm_call
                        tool_calls = []
                        fcs = event.get_function_calls() if hasattr(event, "get_function_calls") else None
                        if fcs:
                            tool_calls = [{"name": fc.name, "args": str(fc.args)[:100]} for fc in fcs]
                        record_llm_call(
                            agent=agent_name,
                            input_tokens=inp,
                            output_tokens=out,
                            tool_calls=tool_calls,
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    def _update_billing(self, agent_name: str, inp: int, out: int):
        """Update billing JSON file with token counts.

        Also broadcasts a ``billing`` WS event so the TUI can render the cost
        line without polling the file. The file is still written for offline
        debugging + future observability tools.
        """
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
            # Broadcast updated billing snapshot to TUI clients.
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("billing", billing)
        except Exception:
            pass
