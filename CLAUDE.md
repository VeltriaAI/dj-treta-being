# CLAUDE.md — DJ Treta

## What This Is

DJ Treta is an autonomous AI DJ Being. It uses smolagents + LiteLLM (Gemini) as the brain, Mixxx (forked with HTTP API) as the audio engine, and the Beings Protocol for identity/memory/goals.

## Architecture

```
Brain (smolagents ToolCallingAgent + LiteLLM)  →  PLANS
Daemon (state machine at 2Hz)                  →  ORCHESTRATES
Executor (deterministic, 20fps)                →  EXECUTES
Mixxx (C++ DJ software, HTTP API on :7778)     →  PLAYS
```

## Key Files

- `agent/daemon.py` — Main loop, state machine
- `agent/brain.py` — smolagents agent with DJ tools
- `agent/tools.py` — @tool functions wrapping Mixxx API
- `agent/executor.py` — Deterministic transition execution (blend, bass_swap, filter_sweep, hard_cut)
- `agent/perception.py` — Polls Mixxx for real-time state
- `agent/selector.py` — Two-stage track selection (deterministic filter + LLM ranking)
- `agent/camelot.py` — Camelot wheel key compatibility
- `agent/config.py` — config.yaml loader
- `agent/state.py` — DJState, DJPhase, TrackState
- `config.yaml` — All configuration (Mixxx URL, LLM, library, transitions)

## Running

```bash
source .venv/bin/activate
python -m agent --mood techno-deep --duration 60
```

## Prerequisites

- Mixxx (VeltriaAI/mixxx fork) running with HTTP API on port 7778
- SSH tunnel to LiteLLM: `ssh -L 4000:localhost:4000 epadmin@20.235.125.250`
- Tracks in `~/Music/DJTreta/` organized by genre

## MCP Integration

DJ Treta is controlled via MCP tools in `~/beings/himani/skills/dj/mcp-server/`:
- `dj_agent_start` — spawn daemon
- `dj_agent_stop` — graceful shutdown
- `dj_agent_status` — read state from `/tmp/dj-treta-state.json`

## Brain Model

Uses `openai/gemini-3-flash` via LiteLLM proxy. To swap models, change `llm.model` in `config.yaml`:
- `openai/gemini-3-flash` (default, fast)
- `openai/gemini-3.1-pro` (better reasoning)
- `anthropic/claude-sonnet` (alternative)
