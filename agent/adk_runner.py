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


def _apply_billing(agent_name: str, inp: int, out: int, cost: float,
                   key_spend: float | None = None) -> dict:
    """Read-modify-write the shared billing.json accumulator. The single place
    the schema is defined, so ADK-path billing (_update_billing) and direct-call
    billing (bill_external) stay consistent. Returns the updated dict.

    Schema (consumed by TUI + ws_server + session — do not drop fields):
      total_input_tokens, total_output_tokens, total_cost_usd, calls,
      by_agent{name: {input, output, cost, calls}}, session_start,
      key_spend (optional: gateway's cumulative spend on the key).
    """
    billing = json.loads(BILLING_FILE.read_text()) if BILLING_FILE.exists() else {
        "total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0,
        "calls": 0, "by_agent": {}, "session_start": time.time()
    }
    billing["total_input_tokens"] += inp
    billing["total_output_tokens"] += out
    billing["calls"] += 1
    billing["total_cost_usd"] += cost
    a = billing["by_agent"].setdefault(agent_name, {"input": 0, "output": 0, "cost": 0.0, "calls": 0})
    a["input"] += inp
    a["output"] += out
    a["cost"] += cost
    a["calls"] += 1
    if key_spend is not None:
        # Gateway's authoritative cumulative spend on this key — a cross-check
        # anchor for the session total (see billing verification).
        billing["key_spend"] = key_spend
    BILLING_FILE.write_text(json.dumps(billing, indent=2))
    return billing


def bill_external(agent_name: str, inp: int, out: int, cost: float | None = None) -> None:
    """Bill an LLM call made OUTSIDE the ADK event loop (direct
    ``litellm.completion`` sites — canonicalize, mood_resolver, sets, tools/*).
    Uses the gateway's authoritative ``cost`` when the caller has it, else the
    per-model rate map. Mirrors into the observability llm_calls row."""
    try:
        if cost is None:
            from . import billing_rates
            cost = billing_rates.cost_for(billing_rates.alias_for_agent(agent_name), inp, out)
        _apply_billing(agent_name, inp, out, cost)
        try:
            from .observability import record_llm_call
            record_llm_call(agent=agent_name, input_tokens=inp, output_tokens=out, model_cost=cost)
        except Exception:
            pass
    except Exception:
        pass


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
        # FIX-C: track whether the DJ fired any tool call this invocation.
        # A tool-call-with-no-text is a SUCCESS, not an empty drop — the
        # heartbeat reads this flag to disambiguate true-empty (retry/defer)
        # from a silent-but-acting decision (keep). Reset per invoke.
        self._last_dj_made_tool_call = False
        async for event in self._dj_runner.run_async(
            session_id=self._dj_session.id, user_id="dj",
            new_message=message, run_config=run_config,
        ):
            self._process_event(event)
            try:
                if event.get_function_calls():
                    self._last_dj_made_tool_call = True
            except Exception:
                pass
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
                            # Auto-tap into the shared notebook (percept).
                            # A notebook fault must NEVER break thinking/billing/_ws_broadcast.
                            try:
                                from .notebook import get_notebook
                                nb = get_notebook()
                                if nb is not None:
                                    nb.append(
                                        author=agent_name,
                                        kind="percept",
                                        payload={"text": text[:300]},
                                        dedup_key=f"think:{agent_name}",
                                    )
                            except Exception:
                                pass

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
                    # Auto-tap into the shared notebook (decision/transition).
                    # A notebook fault must NEVER break thinking/billing/_ws_broadcast.
                    try:
                        from .notebook import get_notebook
                        nb = get_notebook()
                        if nb is not None:
                            _TRANSITION_TOOLS = {
                                "schedule_transition", "do_transition", "do_bass_swap",
                                "do_echo_out", "do_filter_sweep", "do_hard_cut",
                                "do_riser", "do_dissolve",
                            }
                            if fc.name in _TRANSITION_TOOLS:
                                nb.append(
                                    author=agent_name,
                                    kind="transition",
                                    payload={"tool": fc.name, "args": args_str},
                                    salience=0.9,
                                )
                            else:
                                nb.append(
                                    author=agent_name,
                                    kind="decision",
                                    payload={"tool": fc.name, "args": args_str},
                                )
                    except Exception:
                        pass

            # Billing — usage_metadata. The gateway's authoritative per-call
            # USD rides on event.custom_metadata['response_cost'] (set by the
            # conversion wrap in agents.py); use it when present, else a
            # per-model rate map. candidates_token_count already includes
            # reasoning tokens — do NOT add thoughts_token_count.
            if event.usage_metadata:
                um = event.usage_metadata
                inp = um.prompt_token_count or 0
                out = um.candidates_token_count or 0
                if inp > 0 or out > 0:
                    cm = getattr(event, "custom_metadata", None) or {}
                    auth_cost = cm.get("response_cost")
                    key_spend = cm.get("key_spend")
                    cost = self._update_billing(agent_name, inp, out, auth_cost, key_spend)
                    # v8 Phase 8: record structured llm_calls row (same cost)
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
                            model_cost=cost,
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    def _update_billing(self, agent_name: str, inp: int, out: int,
                        auth_cost: float | None = None, key_spend: float | None = None) -> float:
        """Accumulate a billed LLM call and broadcast the snapshot to the TUI.

        Cost priority: the gateway's authoritative ``auth_cost`` (response_cost)
        when present, else the per-model rate map keyed by the agent's model
        alias. Returns the cost charged so the caller can share it with the
        observability row (keeping both totals identical).
        """
        try:
            if auth_cost is not None:
                cost = float(auth_cost)
            else:
                from . import billing_rates
                cost = billing_rates.cost_for(billing_rates.alias_for_agent(agent_name), inp, out)
            billing = _apply_billing(agent_name, inp, out, cost, key_spend)
            # Broadcast updated billing snapshot to TUI clients.
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("billing", billing)
            return cost
        except Exception:
            return 0.0
