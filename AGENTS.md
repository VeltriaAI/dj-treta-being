# AGENTS.md — DJClaw (v4.2)

## Before you help with this repo

1. Read [`.beings/SOUL.md`](.beings/SOUL.md) — identity
2. Read [`.beings/MEMORY.md`](.beings/MEMORY.md) — musical heuristics
3. Read [`.beings/USER.md`](.beings/USER.md) — listener context
4. Read [`config.yaml`](config.yaml) — Mixxx URL, LLM, library path, capabilities

## Architecture

Pure **Software 3.0**: no deterministic DJ state machine. Agent sees reality and decides.

```
DJTretaBeing (main.py) — single process, 4 threads:

  Thread 1: Main loop (heartbeat, 5-15s adaptive)
    ├── PRIORITY 1: Silence → emergency_play (direct Mixxx API, instant)
    ├── PRIORITY 2: Scheduled transition file exists → spawn executor thread
    ├── PRIORITY 3: Agent decides transition (past 50%, idle deck ready)
    │   └── agent.run() → schedule_transition tool → returns in 3-7s
    └── PRIORITY 4: Backup idle deck load (planner missed it)

  Thread 2: Planner loop (30s cycle)
    ├── Detect track changes → load idle deck immediately
    ├── Re-plan every 2 tracks (configurable)
    ├── Download new tracks from YouTube + generate with Lyria 3
    └── Self-reflection every 5 tracks

  Thread 3: Relay loop (3Hz WebSocket)
    ├── Poll Mixxx status + live VU data
    ├── PerceptionEngine: energy, mood, beat phase, breakdown detection
    ├── Sample energy_arc every 10s → stored on set
    └── Push state to dj.treta.life

  Thread 4: State writer (2s loop)
    └── Write /tmp/dj-treta-state.json for TUI

  On-demand: Transition executor thread
    ├── Adaptive polling: 5s→2s→0.5s→0.2s near target
    ├── Abort if track stops playing
    └── Execute: crossfade / bass_swap / filter_sweep / echo_out / hard_cut
```

### Agent Architecture (smolagents)

```
DJ Agent (dj_treta) — Manager, 20 steps, planning_interval=5
├── Tools: schedule_transition, hear_music, analyze_track, preview_track,
│          get_dj_status, get_live_data, save_learning, recall_learnings,
│          read_file, write_file
├── Mixer Agent (mixer) — 19 tools, 10 steps
├── Library Agent (library) — 4 tools, 8 steps
└── Producer Agent (producer) — 3 tools, 5 steps (Lyria 3)

Planner Agent (planner) — Separate, 15 steps
└── Tools: analyze_track, preview_track, list_library_tracks,
           search_music, download_track, generate_track, recall_learnings
```

### Transition Flow (decoupled)

```
Agent thinks → calls schedule_transition → writes temp file → returns (3-7s)
  ↓
Heartbeat detects file → spawns executor thread
  ↓
Executor polls Mixxx position with adaptive timing
  ↓
Position reached (0.2s accuracy) → execute technique → cleanup → done
```

### Data Layer

```
SQLite: djtreta.db
├── tracks (path, title, bpm, key, energy, timeline, analyzed_at)
├── sets (id, title, mood, energy_arc, peak_energy, status)
├── set_history (track per set with transition type)
├── learnings (self-evolution notes)
└── track_pairs (quality ratings)

Temp files:
├── /tmp/dj-treta-state.json — TUI reads
├── /tmp/dj-treta-scheduled-transition.json — pending transition
├── /tmp/dj-treta-billing.json — token usage
├── /tmp/dj-treta-thinking.log — agent thoughts
├── /tmp/dj-treta-daemon.log — process stdout
└── /tmp/dj-treta-playlist.json — planner output
```

## Safety

- **`capabilities.allow_shell`**: default `false`. Shell tool disabled unless explicitly enabled.
- **`read_file` / `write_file`**: sandboxed to repo root and `library.music_dir`.
- **Secrets**: use `DJTRETA_LLM_API_KEY` env var, not committed config.
- **Music never stops**: emergency recovery via direct Mixxx API (no agent needed).
- **Transition rules**: agent can only schedule at breakdowns/outros (energy ≤ 5), never drops/buildups.

## Music Production

DJ Treta generates original tracks via Google Lyria 3. See [`docs/MUSIC_GENERATION.md`](docs/MUSIC_GENERATION.md).

- `generate_track` tool — specifies BPM, key, mood, instruments, style
- Producer sub-agent — managed alongside mixer and library
- Planner also has `generate_track` for pre-generating during set planning
- Tracks saved to `~/Music/DJTreta/ai-generated/`

## Related Docs

- [`docs/MUSIC_GENERATION.md`](docs/MUSIC_GENERATION.md) — music generation spec
- [`docs/RELAY_SPEC.md`](docs/RELAY_SPEC.md) — WebSocket protocol for dj.treta.life
- [`docs/ROCK_SOLID_IMPLEMENTATION.md`](docs/ROCK_SOLID_IMPLEMENTATION.md) — v2 hardening pass (historical)
- [`CLAUDE.md`](CLAUDE.md) — file map and prerequisites
- [`README.md`](README.md) — user-facing overview
