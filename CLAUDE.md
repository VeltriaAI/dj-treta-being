# CLAUDE.md — DJClaw / DJ Treta

## What This Is

DJClaw — install your own AI DJ Being. DJ Treta is the first Being built on it.
Pure Software 3.0 — the agent decides everything, no deterministic DJ logic.

## Architecture (v6.0 — Being as Brain)

Three-agent architecture: Being (brain) directs DJ and Planner agents via directives.

```
main.py — Being daemon (single process, 4 threads)
  ├── Heartbeat (5-15s adaptive): silence recovery → transition executor → agent decision → backup load
  ├── Planner thread: plans 6 tracks, downloads/generates, loads idle deck, reflects every 5 tracks
  ├── Relay thread: WebSocket push to dj.treta.life at 3Hz, energy arc sampling every 10s
  └── State writer thread: /tmp/dj-treta-state.json every 2s

agents.py — Agent factory (Google ADK LlmAgent)
  Being Agent (treta) — the brain: conversation, directives, mood control
    Tools: set_dj_directive, set_planner_directive, set_mood, get_directives,
           clear_directives, get_dj_status, hear_music, save/recall_learnings
  DJ Agent (dj_treta) — autonomous deck control + transitions
    ├── Mixer Agent (19 tools: deck control, 5 transition techniques)
    ├── Library Agent (4 tools: search YouTube, download, list, history)
    └── Producer Agent (3 tools: generate_track via Lyria 3, list, analyze)
  Planner Agent (planner) — background track selection + downloading

tools/ — 56 @tool functions across 8 modules
  ├── mixxx.py: load, play, pause, volume, crossfader, EQ, filter, sync (16 tools)
  ├── transitions.py: crossfade, bass_swap, filter_sweep, echo_out, hard_cut, schedule (6 tools)
  ├── perception.py: hear_music, analyze_track, preview_track (3 tools)
  ├── generation.py: generate_track via Lyria 3 (1 tool)
  ├── discovery.py: search_music, download_track + helpers (5 tools)
  ├── library.py: list_library_tracks, get_set_history (2 tools)
  ├── meta.py: read_file, write_file, save_learning, recall_learnings (6 tools)
  ├── directives.py: set_dj_directive, set_planner_directive, set_mood, get/clear (7 tools)
  └── helpers.py: internal utilities (10 helpers)

adk_runner.py — ADK invocation: _invoke_agent(), _invoke_being(), _invoke_planner()
  Separate ADK sessions for Being, DJ, and Planner agents

db.py — SQLite (djtreta.db)
  tracks, sets, set_history, learnings, track_pairs

relay.py — PerceptionEngine + WebSocket relay
  Energy, mood, beat phase, transition countdown, waveforms

tui.py — Textual TUI (1,499 lines)
  Decks, mixer, brain panel, playlist sidebar, timeline, 10+ commands

cli.py — CLI (djclaw / djtreta)
  start, stop, restart, kill, reset, init, talk [--readonly], tui, status, logs
```

## Key Files

| File | Lines | What |
|------|-------|------|
| `agent/main.py` | 388 | Being daemon: startup, stale file cleanup, session restore |
| `agent/heartbeat.py` | 309 | Heartbeat loop: silence recovery, transition executor, agent decisions |
| `agent/planner_loop.py` | 372 | Planner thread: plan 6 tracks, download, generate, load idle deck |
| `agent/agents.py` | 399 | Agent factory (ADK): Being + DJ + Mixer + Library + Producer + Planner |
| `agent/adk_runner.py` | 171 | ADK runner: _invoke_agent, _invoke_being, _invoke_planner + billing |
| `agent/tools/` | ~1,700 | 56 tools across 8 modules (directives, mixxx, transitions, perception, etc.) |
| `agent/audio_analysis.py` | 224 | Librosa-based real audio analysis: BPM, key, sections, energy |
| `agent/relay.py` | 630 | PerceptionEngine + WebSocket relay to dj.treta.life |
| `agent/db.py` | 324 | SQLite: tracks, sets, set_history, learnings |
| `agent/transitions.py` | 83 | Transition executor thread (decoupled from agent) |
| `agent/config.py` | 193 | Config dataclasses + YAML loader + .env support |
| `agent/camelot.py` | 103 | Camelot wheel key compatibility |
| `tui.py` | 1,499 | Textual TUI: decks, brain, playlist sidebar, timeline, commands |
| `cli.py` | 635 | CLI: start/stop/restart/kill/reset/init/talk/tui |

## Running

```bash
djclaw init                                # first-time setup wizard
djclaw start "melodic techno"              # start Being + Mixxx + LiteLLM
djclaw talk "play something melodic"       # talk to the Being (brain)
djclaw talk --readonly "what's playing?"   # readonly mode (live web listeners, no deck control)
djclaw tui                                 # full terminal UI
djclaw stop                                # graceful stop
djclaw kill                                # nuclear stop (Mixxx + LiteLLM too)
```

## Prerequisites

- Mixxx fork (VeltriaAI/mixxx, branch feature/http-api) — auto-started
- Gemini API key (free tier works) or any LiteLLM-compatible model
- Python 3.10+ with: smolagents, litellm, httpx, pyyaml, textual, rich
- macOS (Linux planned)

## Key Architectural Decisions

- **Being as Brain**: Being agent (`treta`) handles all conversation and sets directives for DJ + Planner agents. Three separate ADK sessions.
- **Directive system**: Being issues `set_dj_directive()` / `set_planner_directive()` / `set_mood()` — agents read directives on their next cycle. No micromanagement.
- **Readonly talk**: `djclaw talk --readonly` for live web listeners. Chat only, no deck control or directives.
- **Agent decides, Python executes**: `schedule_transition` tool returns immediately, Python executor waits for exact track position with adaptive polling (0.2s precision)
- **Double transition guard**: Heartbeat checks `_transition_pending` flag before spawning a new auto-transition
- **No rate resets in transitions**: Mixxx sync handles BPM matching naturally
- **Breakdown-only transitions**: Agent must schedule at breakdown (energy ≤ 5) or outro sections, never during drops or buildups
- **Track loop fix**: `find_compatible_tracks` uses LIKE (substring match) instead of exact match for played titles — handles Mixxx/DB title mismatches
- **DB fallback for track loading**: queries all tracks, not just analyzed ones — Mixxx can play anything
- **Stale file cleanup**: startup cleans transition, directive, mood, billing, and playlist temp files
- **Energy arc in state**: state file includes `energy_arc` (last 60 samples) and `peak_energy` per set
