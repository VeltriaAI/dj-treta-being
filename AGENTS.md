# AGENTS.md — DJClaw (v3.0)

## Before you help with this repo

1. Read [`.beings/SOUL.md`](.beings/SOUL.md) — identity
2. Read [`.beings/MEMORY.md`](.beings/MEMORY.md) — musical heuristics
3. Read [`.beings/USER.md`](.beings/USER.md) — listener context
4. Read [`config.yaml`](config.yaml) — Mixxx URL, LLM, library path, capabilities

## Architecture (current)

Pure **Software 3.0**: no deterministic DJ state machine. No classify hack.

```
agent/main.py     →  heartbeat loop (30s) + command handler
                     _heartbeat() → always sends reality to agent
                     _agent_talk() → one agent, one personality
                     _agent_reflect() → self-evolution every 5 tracks
smolagents        →  DJ manager + mixer + library sub-agents
agent/tools.py    →  Mixxx HTTP, perception, library, sandboxed file tools
agent/init.py     →  djclaw init wizard (create new DJ Beings)
Mixxx (HTTP API)  →  audio
```

Key v3.0 changes from v2.0:
- **Heartbeat** replaces pulse — agent sees reality every 30s and decides (no Python if/else gates)
- **No classify hack** — all messages go through agent.run(), one personality always
- **Conversation memory** — rolling buffer of last 10 messages, persisted in session.json
- **Self-evolution** — reflects every 5 tracks, updates MEMORY.md via write_file
- **DJClaw packaging** — `pip install -e .`, `djclaw init` wizard, templates for new Beings

## Run

```bash
cd ~/beings/dj-treta
pip install -e .        # or ./install.sh
djclaw init             # first-time: name, taste, LLM provider
djclaw start            # start Being + Mixxx
# djtreta start also works
```

Set **`DJTRETA_LLM_API_KEY`** or **`LLM_API_KEY`** in the environment.

## Safety expectations

- **`capabilities.allow_shell`**: default `false`. Shell tool is disabled unless explicitly enabled.
- **`read_file` / `write_file` / `list_files`**: only under the repo root and `library.music_dir`.
- **Music**: design goal is continuous playback; recovery is agent-driven, not a separate executor.

## Related docs

- [`docs/ROCK_SOLID_IMPLEMENTATION.md`](docs/ROCK_SOLID_IMPLEMENTATION.md) — hardening pass handoff
- [`CLAUDE.md`](CLAUDE.md) — file map and prerequisites
- [`README.md`](README.md) — user-facing overview
