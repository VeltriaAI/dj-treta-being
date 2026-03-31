# CLAUDE.md — DJClaw / DJ Treta

## What This Is

DJClaw — install your own AI DJ Being. DJ Treta is the first Being built on it.
Pure Software 3.0 — the agent decides everything, no deterministic DJ logic.

## Architecture (v5.0 — ADK)

```
main.py — Being daemon (single process, 4 threads)
  ├── Heartbeat (5-15s adaptive): silence recovery → transition executor → agent decision → backup load
  ├── Planner thread: plans 6 tracks, downloads/generates, loads idle deck, reflects every 5 tracks
  ├── Relay thread: WebSocket push to dj.treta.life at 3Hz, energy arc sampling every 10s
  └── State writer thread: /tmp/dj-treta-state.json every 2s

agents.py — Agent factory (Google ADK LlmAgent)
  DJ Agent (manager, 20 steps)
    ├── Mixer Agent (19 tools: deck control, 5 transition techniques)
    ├── Library Agent (4 tools: search YouTube, download, list, history)
    └── Producer Agent (3 tools: generate_track via Lyria 3, list, analyze)
  Planner Agent (15 steps, runs in background thread)

tools.py — 46 @tool functions
  ├── DJ controls: load, play, pause, volume, crossfader, EQ, filter, sync
  ├── Transitions: crossfade, bass_swap, filter_sweep, echo_out, hard_cut
  ├── Scheduling: schedule_transition (agent schedules, Python executes with 0.2s precision)
  ├── Audio perception: hear_music, analyze_track, preview_track (Gemini multimodal)
  ├── Music generation: generate_track (Google Lyria 3)
  ├── Discovery: search_music, download_track (YouTube via yt-dlp)
  └── Self-awareness: read_file, write_file, save_learning, recall_learnings

db.py — SQLite (djtreta.db)
  tracks, sets, set_history, learnings, track_pairs

relay.py — PerceptionEngine + WebSocket relay
  Energy, mood, beat phase, transition countdown, waveforms

tui.py — Textual TUI (1,343 lines)
  Decks, mixer, brain panel, timeline, 10+ commands

cli.py — CLI (djclaw / djtreta)
  start, stop, restart, kill, reset, init, talk, tui, status, logs
```

## Key Files

| File | Lines | What |
|------|-------|------|
| `agent/main.py` | 1,359 | Being daemon: heartbeat, planner, relay, sets, recording, broadcast |
| `agent/tools.py` | 1,427 | 46 tools: DJ control, transitions, perception, generation, discovery |
| `agent/agents.py` | 298 | Agent factory (ADK): DJ + Mixer + Library + Producer + Planner |
| `agent/audio_analysis.py` | 224 | Librosa-based real audio analysis: BPM, key, sections, energy |
| `agent/relay.py` | 630 | PerceptionEngine + WebSocket relay to dj.treta.life |
| `agent/db.py` | 322 | SQLite: tracks, sets, set_history, learnings |
| `agent/config.py` | 184 | Config dataclasses + YAML loader + .env support |
| `agent/camelot.py` | 103 | Camelot wheel key compatibility |
| `tui.py` | 1,343 | Textual TUI: decks, brain, timeline, commands |
| `cli.py` | 624 | CLI: start/stop/restart/kill/reset/init/talk/tui |

## Running

```bash
djclaw init                                # first-time setup wizard
djclaw start "melodic techno"              # start Being + Mixxx + LiteLLM
djclaw talk "play something melodic"       # talk to her
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

- **Agent decides, Python executes**: `schedule_transition` tool returns immediately, Python executor waits for exact track position with adaptive polling (0.2s precision)
- **No rate resets in transitions**: Mixxx sync handles BPM matching naturally
- **Breakdown-only transitions**: Agent must schedule at breakdown (energy ≤ 5) or outro sections, never during drops or buildups
- **DB fallback for track loading**: queries all tracks, not just analyzed ones — Mixxx can play anything
- **Energy arc sampling**: relay captures energy every 10s, stored per set for visualization
