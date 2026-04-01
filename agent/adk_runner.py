"""ADK runner mixin — async agent invocation, billing, and event processing."""

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("dj-treta")

THINKING_FILE = Path("/tmp/dj-treta-thinking.log")
BILLING_FILE = Path("/tmp/dj-treta-billing.json")


class ADKRunnerMixin:

    def _run_async(self, coro, timeout=120):
        """Run async coroutine on the persistent event loop. Thread-safe."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"ADK agent call timed out after {timeout}s")

    async def _invoke_agent_async(self, instruction: str, max_calls: int = 10) -> str:
        """Invoke DJ agent via ADK runner. Processes events for billing + thinking log."""
        from google.genai import types
        from google.adk.runners import RunConfig

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

    def _invoke_agent(self, instruction: str, timeout: int = 90, max_calls: int = 10) -> str:
        """Invoke DJ agent. Sync wrapper with lock to prevent concurrent session access."""
        with self._agent_lock:
            return self._run_async(self._invoke_agent_async(instruction, max_calls), timeout=timeout)

    async def _invoke_planner_async(self, instruction: str) -> str:
        """Invoke planner agent via ADK runner. Processes events for billing + thinking log."""
        from google.genai import types

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

            # Tool calls
            func_calls = event.get_function_calls()
            if func_calls:
                for fc in func_calls:
                    args_str = str(fc.args)[:200] if fc.args else ""
                    with open(THINKING_FILE, "a") as f:
                        f.write(f"[CALL:{agent_name}] {fc.name}({args_str})\n")

            # Billing — usage_metadata
            if event.usage_metadata:
                um = event.usage_metadata
                inp = um.prompt_token_count or 0
                out = um.candidates_token_count or 0
                if inp > 0 or out > 0:
                    self._update_billing(agent_name, inp, out)
        except Exception:
            pass

    def _update_billing(self, agent_name: str, inp: int, out: int):
        """Update billing JSON file with token counts."""
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
        except Exception:
            pass
