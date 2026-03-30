# DJ Treta — ADK Migration Architecture

## Why

smolagents has no async/background agents. Every sub-agent call blocks the parent.
Google ADK has `LongRunningFunctionTool` — exactly what we need.
ADK supports LiteLLM — no vendor lock-in.

## Agent Hierarchy

```
DJTretaAgent (LlmAgent, root)
  instruction: InstructionProvider (dynamic — SOUL.md + DJ_KNOWLEDGE.md + live state)
  model: LiteLlm("openai/gemini-3-flash") via LiteLLM proxy
  tools: [
    get_dj_status, get_live_data,
    hear_music, analyze_track, preview_track,
    schedule_transition (LongRunningFunctionTool),
    generate_track_async (LongRunningFunctionTool),
    save_learning, recall_learnings, read_file, write_file
  ]
  sub_agents: [MixerAgent, LibraryAgent, ProducerAgent]

PlannerAgent (separate root, own Runner + Session)
  sub_agents: [ProducerAgentForPlanner]
  tools: [analyze_track, preview_track, list_library_tracks, recall_learnings, read_file]
  + conditionally: [search_music, download_track]
```

Note: ADK doesn't allow sharing agent instances between parents.
Two ProducerAgent instances with identical config — one for DJ, one for Planner.

## Key Mappings

| smolagents | ADK |
|---|---|
| `ToolCallingAgent` | `LlmAgent` |
| `LiteLLMModel` | `LiteLlm` (google.adk.models.lite_llm) |
| `managed_agents=[]` | `sub_agents=[]` |
| `@tool def foo()` | Plain `def foo()` in `tools=[]` |
| `agent.run(instruction)` | `runner.run_async(session_id, user_id, message)` |
| `step_callbacks` | `after_model_callback`, `after_tool_callback` |

## Tool Migration

Remove `@tool` decorator from all 33 functions. Bodies unchanged.
Wrap two tools in `LongRunningFunctionTool`:
- `generate_track_async` → returns immediately, background generation
- `schedule_transition` → returns immediately, background execution

## LiteLLM (No Vendor Lock-in)

```python
from google.adk.models.lite_llm import LiteLlm
model = LiteLlm(model="openai/gemini-3-flash")
# Uses existing LiteLLM proxy at localhost:4000
# Can swap to any model: Claude, Llama, Mistral, etc.
```

## Heartbeat Pattern

ADK has no heartbeat. DJTretaBeing remains the orchestrator:
```python
while self._running:
    self._check_commands()
    if not self._agent_busy:
        await self._heartbeat()
    await asyncio.sleep(self._next_sleep)
```

Agent invocation changes from `agent.run()` to `runner.run_async()`.

## What Changes

| File | Change |
|---|---|
| `agent/agents.py` | Rewrite — LlmAgent + LiteLlm + LongRunningFunctionTool |
| `agent/tools.py` | Minor — remove @tool decorators |
| `agent/main.py` | Moderate — async bridge, Runner init |
| `pyproject.toml` | Replace smolagents with google-adk |

## What Does NOT Change

- `agent/config.py` — identical
- `agent/db.py` — identical
- `agent/relay.py` — identical
- `agent/audio_analysis.py` — identical
- `agent/camelot.py` — identical
- `config.yaml` — identical
- `.beings/*` — identical
- `tui.py` — identical (file-based IPC unchanged)
- MCP server — identical

## Migration Phases

1. Foundation — install ADK, create basic LlmAgent, test one tool
2. Full tools — register all 33 tools, create sub-agents
3. Heartbeat — wire into DJTretaBeing, async bridge
4. Callbacks — thinking log, billing, state management
5. LongRunningFunctionTool — async generation + transitions
6. Cleanup — remove smolagents, update docs
</content>
