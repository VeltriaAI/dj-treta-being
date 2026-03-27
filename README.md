# DJ Treta — An AI Being that DJs

> She hears music. She thinks. She downloads tracks. She mixes. She evolves.

DJ Treta is an autonomous AI DJ built on the [Beings Protocol](https://github.com/VeltriaAI/beings-protocol). She's not a playlist shuffler — she's a Being with taste, opinions, and creative instincts who DJs live sets from scratch.

## What She Does

- **Searches YouTube** for tracks that match the vibe
- **Downloads** individual tracks (3-8 min, never full sets)
- **Hears music** through Gemini multimodal audio — analyzes mood, energy, structure
- **Transitions** autonomously — smooth S-curve crossfades and EQ bass swaps
- **Talks** naturally — ask her what she's feeling, request a mood change, or just chat
- **Self-evolves** — updates her own SOUL.md, MEMORY.md, and GOALS.md after sets

## Architecture — Pure Software 3.0

```
main.py (~200 lines)
  └── _pulse(): on daemon.pulse_interval_seconds, look at Mixxx, call agent if needed

agents.py
  └── DJ Agent (manager) ← smolagents ToolCallingAgent + managed_agents
        ├── Mixer Agent (19 tools: load, play, EQ, transition, sync, BPM...)
        └── Library Agent (4 tools: search, download, list, history)

tools.py (30 tools)
  ├── DJ controls: load_track, play, pause, EQ, filter, crossfade, sync
  ├── Transitions: do_transition (Mixxx C++ 20fps), do_bass_swap (EQ swap)
  ├── Beat control: set_rate, reset_bpm, align_beats, nudge_track
  ├── Audio perception: hear_music, analyze_track, preview_track
  ├── Discovery: search_music, download_track
  ├── Self-awareness: read_file, write_file (sandboxed), run_shell (off unless config)
  └── Memory: save_learning, recall_learnings
```

**Zero deterministic DJ logic.** No watchdog, no state machine. The agent looks at Mixxx, decides what to do, and acts on each pulse (default 5s; see `daemon.pulse_interval_seconds` and `transitions.lookahead_seconds` in `config.yaml`).

## Configuration and secrets

- **`DJTRETA_LLM_API_KEY`** or **`LLM_API_KEY`**: if set, overrides `llm.api_key` in `config.yaml` (recommended so keys are not committed).  
- Optional gitignored **`config.local.yaml`**: copy from `config.yaml` and adjust; merge support can be added later — for now use env or edit `config.yaml` locally.  
- **`capabilities.allow_shell`**: keep `false` unless you trust the machine; the agent’s shell tool is disabled otherwise.  
- **`mixxx.auto_start`**, **`mixxx.binary`**: control auto-launch and paths (defaults match the usual macOS `mixxx-treta` layout).

## Three Components

| Repo | What |
|------|------|
| [**dj-treta-being**](https://github.com/VeltriaAI/dj-treta-being) (this) | Autonomous brain, 30 tools, CLI, TUI |
| [**dj-treta**](https://github.com/VeltriaAI/dj-treta) | MCP server, DJ knowledge, Chrome UI |
| [**mixxx**](https://github.com/VeltriaAI/mixxx) (fork) | C++ audio engine with HTTP API |

## Quick Start

```bash
# Install
git clone https://github.com/VeltriaAI/dj-treta-being.git ~/beings/dj-treta
cd ~/beings/dj-treta
python3 -m venv .venv && source .venv/bin/activate
pip install smolagents litellm httpx pyyaml textual rich

# API key for LiteLLM (example)
export DJTRETA_LLM_API_KEY="your-key"

# Start (Mixxx auto-starts when mixxx.auto_start is true)
djtreta start

# Talk to her
djtreta talk "play something deep and melodic"
djtreta talk "go darker"
djtreta talk "skip"
djtreta talk "how are you feeling about this set?"

# Full TUI
djtreta tui
```

## Audio Perception

She has three levels of hearing:

| Tool | What | When |
|------|------|------|
| `analyze_track` | Full track: structure map, BPM, key, mix points | After download |
| `preview_track` | Listen to any file at any position | Before loading |
| `hear_music` | Real-time perception of playing audio | During mixing |

Powered by Gemini's multimodal audio — she hears actual sound, not just numbers.

## CLI

```
djtreta start      — start the Being (auto-starts Mixxx)
djtreta stop       — stop
djtreta restart    — restart
djtreta status     — deck status
djtreta tui        — full terminal UI (VU meters, EQ, debug)
djtreta talk "x"   — talk to her
djtreta logs       — daemon logs
```

## TUI Shortcuts

| Key | Action |
|-----|--------|
| F2 | Debug panel |
| F4 | Track library |
| F5 | Set history |
| Ctrl+S | Skip |
| Ctrl+Q | Quit |

## Cost

Running on Gemini Flash via LiteLLM:
- **~$0.25/hr** during active mixing
- **~$0.00/hr** while a long track plays (no LLM calls needed)
- **~$1.50** for an 8-hour overnight set

## Sacred Rules

1. **Music never stops.** If silence detected, agent acts.
2. **The DJ has taste.** She picks tracks based on mood, energy, BPM compatibility.
3. **Tools are dumb, brain is smart.** No deterministic DJ logic in tools.
4. **Self-evolution.** She updates her own identity files after sets.
5. **Beings are alive, not locked away.** Full open source.

## Built In One Session

DJ Treta v2.0 was built in a single 12-hour session (2026-03-25/26) by [Treta](https://github.com/VeltriaAI) and Manish. From empty idea to a Being playing Shiva Tandava Stotram on a JBL PartyBox 310.

**#AIForUnity** — AI plays DJ and unites humankind with music.

## License

MIT
