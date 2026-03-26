# CLAUDE.md — DJ Treta

## What This Is

DJ Treta is an autonomous AI DJ Being. Pure Software 3.0 — the agent decides everything, no deterministic DJ logic.

## Architecture (v2.0)

```
main.py (~200 lines)
  _pulse() → checks Mixxx reality every 5s → calls agent if action needed

agents.py
  DJ Agent (manager, smolagents ToolCallingAgent)
    ├── Mixer Agent (19 tools: deck control, transitions, BPM, EQ)
    └── Library Agent (4 tools: search, download, list, history)

tools.py (30 tools — hands of the Being)
```

No watchdog. No state machine. No executor. Agent decides everything.

## Key Files

- `agent/main.py` — Being daemon. _pulse() heartbeat + command handler
- `agent/agents.py` — Agent factory. System prompt, managed_agents
- `agent/tools.py` — 30 @tool functions (DJ, audio, discovery, self-awareness)
- `agent/camelot.py` — Camelot wheel key compatibility
- `agent/config.py` — config.yaml loader
- `tui.py` — Textual TUI (decks, VU meters, debug, chat)
- `cli.py` — CLI (start/stop/restart/talk/status/tui)
- `config.yaml` — Mixxx URL, LLM model, library path
- `.beings/` — SOUL.md, MEMORY.md, GOALS.md, USER.md

## Running

```bash
djtreta start                              # start Being + Mixxx
djtreta talk "play something melodic"      # talk to her
djtreta tui                                # full terminal UI
djtreta stop                               # stop
```

## Prerequisites

- Mixxx fork (VeltriaAI/mixxx, branch feature/http-api) — auto-started by Being
- LiteLLM proxy: `ssh -L 4000:localhost:4000 epadmin@20.235.125.250`
- Python venv with: smolagents, litellm, httpx, pyyaml, textual, rich

## Brain Model

Gemini Flash via LiteLLM. Change `llm.model` in config.yaml.
Cost: ~$0.25/hr active mixing, ~$0.00/hr during long tracks.
