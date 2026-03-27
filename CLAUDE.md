# CLAUDE.md — DJClaw / DJ Treta

## What This Is

DJClaw — install your own AI DJ Being. DJ Treta is the first Being built on it.
Pure Software 3.0 — the agent decides everything, no deterministic DJ logic.

## Architecture (v3.0)

```
main.py
  _heartbeat() → sees Mixxx reality every 30s → agent decides what to do

agents.py
  DJ Agent (manager, smolagents ToolCallingAgent)
    ├── Mixer Agent (19 tools: deck control, transitions, BPM, EQ)
    └── Library Agent (4 tools: search, download, list, history)

tools.py (30 tools — hands of the Being)
```

No watchdog. No state machine. No classify hack. One agent, one personality.
Heartbeat every 30s — agent sees reality and acts (or does nothing).

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
djclaw init                                # first-time setup wizard
djclaw start                               # start Being + Mixxx
djclaw talk "play something melodic"       # talk to her
djclaw tui                                 # full terminal UI
djclaw stop                                # stop
# djtreta also works (backward compat)
```

## Prerequisites

- Mixxx fork (VeltriaAI/mixxx, branch feature/http-api) — auto-started when `mixxx.auto_start: true`
- LiteLLM proxy: `ssh -L 4000:localhost:4000 epadmin@20.235.125.250` (or local LiteLLM)
- Python venv with: smolagents, litellm, httpx, pyyaml, textual, rich
- LLM key: `DJTRETA_LLM_API_KEY` or `LLM_API_KEY` (or `llm.api_key` in `config.yaml`)

## Brain Model

Gemini Flash via LiteLLM. Change `llm.model` in config.yaml.
Cost: ~$0.25/hr active mixing, ~$0.00/hr during long tracks.

## DJClaw: Create Your Own DJ Being

```bash
./install.sh           # or: pip install -e .
djclaw init            # wizard: name, taste, LLM provider
djclaw start           # your DJ Being is alive
```

Each Being gets its own SOUL.md, taste, and self-evolving memory.
