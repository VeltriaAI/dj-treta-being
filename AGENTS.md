# AGENTS.md — DJ Treta Session Protocol

## Starting a Session

1. Read `.beings/SOUL.md` — your identity
2. Read `.beings/MEMORY.md` — your musical memory
3. Read `.beings/USER.md` — listener preferences
4. Check `config.yaml` — current configuration

## Architecture

```
Brain (smolagents + LiteLLM)     →  PLANS decisions
  ↓
Daemon (state machine)           →  ORCHESTRATES timing
  ↓
Executor (deterministic)         →  EXECUTES transitions
  ↓
Mixxx (HTTP API on :7778)        →  PLAYS audio
```

## State Machine

```
STARTING → PLAYING → PREPARING → TRANSITIONING → PLAYING (loop)
                                                     ↓
                                                  RECOVERY
                                                     ↓
                                                  STOPPED
```

## Running

```bash
# Activate environment
cd ~/beings/dj-treta
source .venv/bin/activate

# Start daemon
python -m agent --mood techno-deep --duration 60

# Or via MCP (from Claude Code)
# dj_agent_start, dj_agent_stop, dj_agent_status
```

## Safety Rules

- Music NEVER stops — old track plays until transition completes
- Never load on outgoing deck during transition
- Emergency fallback: hard_cut to any unplayed track
- Max 10 consecutive errors before recovery mode
- Recovery: if Mixxx still playing, resume. If not, stop gracefully.
