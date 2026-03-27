# AGENTS.md — DJ Treta (v2 Being)

## Before you help with this repo

1. Read [`.beings/SOUL.md`](.beings/SOUL.md) — identity  
2. Read [`.beings/MEMORY.md`](.beings/MEMORY.md) — musical heuristics  
3. Read [`.beings/USER.md`](.beings/USER.md) — listener context  
4. Read [`config.yaml`](config.yaml) — Mixxx URL, LLM, library path, capabilities  

## Architecture (current)

Pure **Software 3.0**: no deterministic DJ state machine in the live path.

```
agent/main.py     →  pulse loop + command file (TUI/CLI)
smolagents        →  DJ manager + mixer + library sub-agents
agent/tools.py    →  Mixxx HTTP, perception, library, sandboxed file tools
Mixxx (HTTP API)  →  audio
```

The archived daemon/state-machine code lives under [`agent/_archive/`](agent/_archive/) for reference only.

## Run

```bash
cd ~/beings/dj-treta
source .venv/bin/activate
djtreta start
# or
python -m agent [--config /path/to/config.yaml]
```

Set **`DJTRETA_LLM_API_KEY`** or **`LLM_API_KEY`** in the environment (overrides `config.yaml`), or set `llm.api_key` locally (avoid committing secrets).

## Safety expectations

- **`capabilities.allow_shell`**: default `false`. Shell tool is disabled unless explicitly enabled.  
- **`read_file` / `write_file` / `list_files`**: only under the repo root and `library.music_dir`.  
- **Music**: design goal is continuous playback; recovery is agent-driven, not a separate executor.

## Related docs

- [`docs/ROCK_SOLID_IMPLEMENTATION.md`](docs/ROCK_SOLID_IMPLEMENTATION.md) — handoff: hardening pass, config, sandbox, behavior (for Claude Code / contributors)  
- [`CLAUDE.md`](CLAUDE.md) — file map and prerequisites  
- [`README.md`](README.md) — user-facing overview  
