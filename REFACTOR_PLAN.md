# DJ Treta v8 — Agent Separation Refactor

**Branch:** `refactor/v8-agent-separation`
**Started:** 2026-04-18
**Status:** Planning — not implementing yet
**Core principle:** LLM decides. Python executes. One truth source per concern. Remove all soft-filter guards.

---

## Table of contents

1. [Context — why v8](#context)
2. [The 5 layers](#the-5-layers)
3. [Session class — single source of truth](#session-class)
4. [Planner agent — continuous suggestion engine](#planner-agent)
5. [Library manager — owns library growth](#library-manager)
6. [Producer — owns original music generation](#producer)
7. [DJ agent — final authority on playback](#dj-agent)
8. [Python heartbeat — thin execution + safety layer](#heartbeat)
9. [Coordination — how layers talk](#coordination)
10. [Migration sequence](#migration)
11. [Open questions](#open-questions)

---

## <a id="context"></a>1. Context — why v8

Today's system has drifted far from Software 3.0. Examples of the drift:

- **Planner LLM writes markdown that Python ignores.** `_run_planner` produces verbose plans, then `_load_next_on_idle` runs its own SQL query and picks a different track.
- **3 truth sources** for state: process memory, `/tmp/dj-treta-state.json`, `.beings/session.json` — all drift.
- **Mood filter is silent soft-gate** — `mood_match = [filter]; if mood_match: candidates = mood_match` silently falls through when empty.
- **Planner does 3 jobs mixed** — planning + library search/download + producer delegation — all in one synchronous thread.
- **Library agent exists but dormant** — buried under DJ, bypassed by planner.
- **DJ agent has no track-selection authority** — only decides transition timing, Python picks the track.
- **Heartbeat P4 wasteful invocation** — calls DJ agent every 15s even when schedule is locked, causing log amplification and LLM churn.

v8 flips the script: **each agent has one clear job, one authority, one peer contract. Python is thin.**

---

## <a id="the-5-layers"></a>2. The 5 layers

```
┌─────────────────────────────────────────────────────────────┐
│  SESSION CLASS  — single source of truth, auto-serialized   │
│  reads/writes:  mood, mood_profile, playlist, played,       │
│                 directives, set info, chat history, flags   │
└─────────────────────────────────────────────────────────────┘
         ▲         ▲          ▲          ▲          ▲
         │         │          │          │          │
    ┌────┴───┐ ┌──┴────┐ ┌───┴────┐ ┌───┴───┐ ┌────┴────┐
    │ PLANNER│ │LIBRARY│ │PRODUCER│ │  DJ   │ │  BEING  │
    │  (LLM) │ │ (LLM) │ │ (LLM)  │ │ (LLM) │ │  (LLM)  │
    │        │ │       │ │        │ │       │ │         │
    │ plays  │ │search │ │ Lyria3 │ │ load  │ │  chat   │
    │playlist│ │+ DL + │ │ gen    │ │ + mix │ │ + soul  │
    │suggest │ │canon  │ │        │ │       │ │         │
    └────────┘ └───────┘ └────────┘ └───────┘ └─────────┘
         ▲          ▲         ▲         ▲          ▲
         └──────────┴─────────┴─────────┴──────────┘
                             │
         ┌───────────────────┴──────────────────┐
         │  PYTHON HEARTBEAT — thin execution   │
         │  - emergency silence recovery        │
         │  - precise-timing transition runner  │
         │  - agent invocation triggers         │
         │  - safety invariants                 │
         └──────────────────────────────────────┘
                             │
                      ┌──────┴──────┐
                      │    MIXXX    │
                      │ (audio I/O) │
                      └─────────────┘
```

Five LLM agents (Being, Planner, Library, Producer, DJ) as **peers**, not nested sub-agents. All read/write shared Session state. Python heartbeat is thin — no decisions, only execution + safety.

---

## <a id="session-class"></a>3. Session class — single source of truth

### Design

A class that wraps every piece of live state. Any property mutation triggers an async debounced write to `session.json`. Readers always see consistent state; writers never worry about "did I forget to persist?"

### File

`agent/session_state.py` (new). Replaces scattered `self.mood`, `self.tracks_played`, `self.current_set`, `self.user_intent`, `/tmp/dj-treta-state.json`, `.beings/session.json`, `/tmp/dj-treta-playlist.json`, etc.

### Properties (initial set — can grow)

```python
class Session:
    # Identity / set
    set_id: str
    set_started_at: float
    set_target_duration_min: int
    set_status: str  # "live" | "ended"

    # Intent
    mood: str                    # raw user input, e.g. "BollyAffro"
    mood_profile: dict | None    # LLM-resolved: {canonical_genre, alternates, bpm_range, energy_range, vibe}
    user_intent: str             # last user message for planner
    planner_directive: str       # Being → Planner
    dj_directive: str            # Being → DJ

    # Playback state
    tracks_played: list[dict]    # [{path, title, started_at, ended_at, deck, transition_used}]
    current_deck: int            # 1 or 2
    current_track_path: str
    current_position_s: float

    # Playlist (planner's output — advisory)
    playlist: list[dict]         # [{path, title, bpm, key, energy, reason, transition_hint, rank}]
    playlist_updated_at: float
    playlist_mood_snapshot: str  # the mood that was active when this playlist was built

    # Signals (event bus — see §9)
    replan_requested: bool
    library_need: dict | None    # {"mood": "bollyafro", "count": 3, "reason": "..."}
    producer_need: dict | None   # {"mood": ..., "bpm": ..., "key": ..., "brief": "..."}

    # Housekeeping
    last_reflect_count: int
    chat_history: list[tuple]
    emergency_count: int
    saved_at: float
```

### Auto-serialization

```python
session = Session.load()          # from session.json on daemon start
session.mood = "BollyAfro"        # triggers a debounced write
session.tracks_played.append(...) # also triggered (via wrapped list)
```

Implementation sketch: use `__setattr__` + a background flush thread that coalesces writes every 500ms. For nested dicts/lists, use an observed wrapper or call `session.flush()` explicitly when mutating in place. Keep it simple — not a reactive framework.

### Invariants

- **Never** touch the state directly — always through `session.x`.
- **Never** write to `session.json` from anywhere else.
- Every agent reads via `session`, writes via `session`.

### What goes AWAY after this lands

- `STATE_FILE = "/tmp/dj-treta-state.json"` (heartbeat broadcast file)
- `MOOD_CHANGE_FILE = "/tmp/dj-treta-mood-change.json"` (Being→Python directive)
- `DIRECTIVE_FILE = "/tmp/dj-treta-directives.json"`
- `PLAYLIST_FILE = "/tmp/dj-treta-playlist.json"` (markdown blob)
- `self.mood`, `self.tracks_played`, `self.current_set`, `self.user_intent`, etc. on the DJTretaBeing
- `_save_session()` / `_restore_session()` ad-hoc functions in `session.py`

---

## <a id="planner-agent"></a>4. Planner agent — continuous suggestion engine

### One-line mission

**Keep `session.playlist` populated with a ranked list of strong next-track candidates, given current mood + library + history.**

### What it does

- Runs continuously in its own thread.
- On every wakeup:
  1. Reads Session (mood_profile, tracks_played, current_track_path, user_intent, planner_directive, feedback from DB).
  2. Reads full analyzed library from DB.
  3. LLM call with rich context → returns **structured JSON playlist** (ranked list of 5-10 candidates).
  4. Writes to `session.playlist`.
- Wakes up on:
  - Playlist consumed (DJ loaded 3 out of 5 → plan 3 more).
  - `session.mood` or `session.mood_profile` changed.
  - `session.user_intent` is non-empty (Being set it after user chat).
  - `session.planner_directive` is non-empty.
  - Library grew significantly (new tracks appeared — may shift rankings).
  - Max idle time (e.g., 60s) as safety poll.

### What it STOPS doing

- ❌ Calling `search_music` / `download_track` (moves to Library manager).
- ❌ Delegating to producer sub-agent (moves to Producer).
- ❌ Loading tracks on idle deck (`_load_next_on_idle` is deleted from planner).
- ❌ SQL pre-filter on candidates (LLM sees full library).
- ❌ Soft mood filter + genre override + originals preference + RANDOM fallback — all deleted.
- ❌ Writing markdown playlist. Writes **structured JSON** via Session.
- ❌ Triggering reflection threads (that's Being's job).

### What it CHANGES

- Prompt becomes tighter: "Here's the library, here's the mood, here's history. Return top 5 candidates as JSON with reasoning."
- LLM context includes **full library** (~163 tracks with canonical + BPM + key + energy + mood + remixer).
- Trigger is **event-driven** via Session, not polling every 15s.
- Output is **non-binding** — it's a suggestion for DJ, not a command.

### Playlist schema

```json
{
  "planned_at": 1776500000,
  "mood_snapshot": "bollyafro",
  "reasoning_summary": "Library has 38 bolly tracks at 117-125 BPM. Current track building energy, picking 3 same-BPM to hold peak, 2 lower-BPM for optional drop.",
  "tracks": [
    {
      "rank": 1,
      "path": "/Users/.../afusic - Pal Pal.mp3",
      "title": "afusic - Pal Pal (Madoc Remix)",
      "bpm": 117, "key_camelot": "F#m", "energy": 8,
      "genre": "bollyafro",
      "reason": "Matches current BPM, lifts energy via vocal",
      "transition_hint": { "technique": "crossfade", "duration": 45, "at_section": "breakdown" }
    },
    { "rank": 2, ... },
    { "rank": 3, ... },
    { "rank": 4, ... },
    { "rank": 5, ... }
  ]
}
```

### Emitting signals

If planner sees library is thin for current mood:
```python
session.library_need = {
  "mood": "bollyafro",
  "count": 3,
  "bpm_hint": [115, 125],
  "reason": "Only 12 unplayed bolly tracks left in library"
}
```

Library manager picks this up and acts.

### Tools planner needs

- `list_library_with_metadata` (new — returns full analyzed library with canonical/BPM/key/energy/mood)
- `get_set_history` (already exists)
- `get_liked_tracks` / `get_disliked_tracks` (already exist)
- **That's it.** No search, no download, no load. Pure reasoning.

### Size after refactor

- **Today:** 402 lines (`planner_loop.py`)
- **Target:** ~120 lines (thin loop + event listener + LLM invoke + structured write)

---

## <a id="library-manager"></a>5. Library manager — owns library growth

### One-line mission

**Keep the library rich enough for whatever mood Treta plays.**

### What it does

- Runs continuously in its own thread.
- Watches:
  - `session.library_need` signal from planner.
  - Library stats (tracks per mood, unplayed per mood, artist diversity).
  - Daily/hourly cadence for proactive fills.
- On need detected:
  1. LLM plans search queries (e.g., "BollyAfro Punjabi remix 2025", "Afro house bollywood mashup 2024").
  2. Calls `search_music(query)` for each.
  3. Picks top candidates from results, filtered by:
     - Duration 2-10 min (already in search_music)
     - Diversity (don't all be same uploader)
  4. Calls `download_track(url, genre)` — goes through our 3-layer canonical flow.
  5. Clears `session.library_need`.

### What it STOPS doing

- ❌ Being nested under DJ as sub-agent — elevated to peer.
- ❌ Passive — no more "only acts when someone delegates." Proactive.

### What it KEEPS doing

- ✅ `search_music` (yt-dlp search with filters — already works).
- ✅ `download_track` (our 3-layer flow with canonical identity — just rewritten).
- ✅ Background `_enrich_track` thread (librosa + Gemini analysis + ID3 tags + DB update) — already async, keep.

### Responsibilities (expanded)

- **Search:** LLM crafts queries based on mood profile. Diversifies terms.
- **Download:** Our new 3-layer flow (URL dedup → LLM canonical check → download with canonical filename).
- **Canonicalize:** Already in `canonicalize.py` — reuse.
- **Enrich:** Background librosa + Gemini analysis — already works.
- **Dedup:** Prevent duplicate URLs and duplicate canonical tracks from entering library.
- **Gap analysis:** "Library has 99 melodic-techno, 20 bolly. Mood is bolly. Fill the gap."

### Tools library needs

- `search_music`
- `download_track` (the new 3-layer version)
- `list_library_with_metadata` (for gap analysis)
- `get_set_history` (don't re-download tracks just played)

### Trigger conditions

| Trigger | Source | Action |
|---|---|---|
| `session.library_need` set | Planner | Act immediately |
| Mood changed, library thin for new mood | Observed via Session | Auto-fill |
| Daily cadence | Timer | Refresh diversity |
| User explicitly asks | Being → signal | Immediate |

### File changes

- Move library agent definition out of DJ sub-agent chain, make root-level peer.
- `search_music` + `download_track` tools **removed from planner's tool set**.
- Library thread started in `main.py` alongside planner thread.

---

## <a id="producer"></a>6. Producer — owns original music generation

### One-line mission

**Generate original Treta tracks that fit the current vibe and fill library gaps the internet can't.**

### What it does

- Runs continuously in its own thread (when `config.producer.enabled`).
- Watches:
  - `session.producer_need` signal (from planner or library).
  - Current mood, current energy arc, recently played tracks (vibe awareness).
  - Library composition (gap detection — "no chill bridge tracks in bollyafro").
- On need detected:
  1. LLM composes a brief: genre, BPM, key, mood, instrumentation, texture, energy.
  2. Calls `generate_track(brief)` — Lyria 3 via Vertex AI.
  3. Saves to `~/Music/DJTreta/ai-generated/` (configurable via `config.producer.genre_dir`).
  4. Enters same canonical + enrich pipeline as downloads.
  5. Clears `session.producer_need`.

### What it STOPS doing

- ❌ Being TWO sub-agent instances (one under DJ, one under Planner) — one canonical peer.
- ❌ Waiting for planner to explicitly delegate — can self-initiate.

### What it KEEPS doing

- ✅ `generate_track` via Lyria 3 (already works).
- ✅ Track enrichment pipeline (librosa + ID3 + DB).

### Trigger conditions

| Trigger | Source | Action |
|---|---|---|
| `session.producer_need` set | Planner/Library | Act immediately |
| "Library has zero chill tracks for mood X" | Gap analysis | Proactive generate |
| User asks "generate something" | Being → signal | Immediate |
| Budget cap reached | Safety | Pause for cooldown |

### Tools producer needs

- `generate_track` (LongRunningFunctionTool — Lyria 3)
- `list_library_with_metadata` (for gap sensing)
- Session reads (mood, current tracks)

---

## <a id="dj-agent"></a>7. DJ agent — final authority on playback

### One-line mission

**You are the DJ in front of the crowd. Pick from suggestions, load, mix, transition.**

### What it does

- Invoked by heartbeat when deck transition decision is needed.
- On each invocation:
  1. Reads Session (playlist, current track, mood, user vibe feedback, mood_profile).
  2. Reads Mixxx state (decks, positions, BPMs).
  3. Looks at planner's playlist suggestions (top 5, ranked).
  4. **Decides:** which candidate to load next, or request a replan with different criteria.
  5. If loading: `load_track(path)` on idle deck.
  6. Decides transition technique + timing → `schedule_transition(...)`.
  7. Python heartbeat P3 executes at the specified position.

### What it CHANGES

- DJ now has **two** decisions, not just one:
  - **Track selection** — pick from planner's playlist (or request replan).
  - **Transition timing/technique** — as today.
- Invocation rate changes — only when decision actually needed (not every 15s).
- Can emit signals back:
  - `session.replan_requested = True` — "your playlist doesn't match current crowd, replan."
  - Override a loaded track if something better shows up post-planner-update.

### What it STOPS doing

- ❌ Being called wastefully every 15s when schedule is already locked (Python fixes P4 gate).
- ❌ Depending on Python SQL filter for track picks — DJ sees planner's ranked playlist directly.

### What it KEEPS

- ✅ Transition technique library (crossfade, bass_swap, filter_sweep, echo_out, hard_cut).
- ✅ `schedule_transition` pattern — LLM sets intent, Python executes with 0.2s precision.
- ✅ Lock file to prevent double-scheduling.

### Tools DJ needs

- `get_dj_status`, `get_live_data`
- `load_track(path, deck)` (moved from library sub-agent, direct to DJ)
- `schedule_transition(...)`
- `hear_music` (optional — if DJ wants to sanity-check mood match)
- `analyze_track`, `preview_track`
- Session reads (playlist especially)

### Heartbeat gate fix

```python
# OLD (heartbeat.py P4): invoke DJ every 15s past 50% if not busy
if (idle_ready and past_50pct and not busy and not transition_pending):
    invoke_dj()

# NEW: also skip if scheduled transition file exists (already decided)
if (idle_ready and past_50pct and not busy and not transition_pending
        and not sched_file.exists()):  # ← add this
    invoke_dj()
```

One-line fix. Saves 4-6 wasted LLM invocations per transition cycle.

---

## <a id="heartbeat"></a>8. Python heartbeat — thin execution + safety layer

### One-line mission

**Keep music playing. Execute scheduled decisions with precise timing. Do not decide anything.**

### What it owns (keep)

- **P1 Silence recovery** — if both decks stop, emergency load any available track. Music must never stop.
- **P3 Scheduled transition execution** — reads `/tmp/dj-treta-scheduled-transition.json` at the right position, executes precisely.
- **Auto-transition safety net** — if track <30s remaining and DJ hasn't scheduled, force a straight crossfade.
- **Agent invocation triggers** — decide WHEN to wake DJ, not what to decide.

### What it LOSES

- ❌ SQL-based track selection (`_load_next_on_idle` moves conceptually to DJ, implementation may stay as a thin "apply playlist pick to Mixxx" helper).
- ❌ Soft mood filter, genre override, random fallback — all gone.
- ❌ Keeping its own copy of state — reads Session.

### What it CHANGES

- P4 gate includes `not sched_file.exists()` (see above).
- P2 auto-transition fires only as a safety net — DJ's scheduled decisions are primary.
- Sleep intervals shorter on state changes, longer on steady state (already dynamic today).

---

## <a id="coordination"></a>9. Coordination — how layers talk

### Core pattern: Session as event bus

Agents don't call each other directly. They:
- **Read** Session for current state.
- **Write** Session with intent or signals.
- Background threads **watch** their relevant signals.

### Signal catalog

| Signal (Session property) | Writer | Reader(s) | Purpose |
|---|---|---|---|
| `mood`, `mood_profile` | Being (on user message), commands | Planner, DJ, Library | Current taste target |
| `user_intent` | Being | Planner | Transient request to prioritize |
| `planner_directive` | Being | Planner | Being steers planner |
| `dj_directive` | Being | DJ | Being steers DJ |
| `replan_requested` | DJ, Being | Planner | "Re-do the playlist" |
| `library_need` | Planner | Library | "Grow library for this mood" |
| `producer_need` | Planner, Library | Producer | "Generate a track for this gap" |
| `playlist` | Planner | DJ | Ranked candidates |
| `tracks_played` | DJ (on transition), heartbeat (on auto-trans) | Planner, UI | Set history |
| `current_track_path`, `current_position_s` | Heartbeat (from Mixxx poll) | All agents | State awareness |

### Thread model

Each agent runs in its own long-lived thread, waking on a mix of:
- **Signal change** (threading.Event or a poll on session property).
- **Timer** (max idle wake-up for safety).
- **External trigger** (command, directive).

No agent-to-agent direct calls. No file-based IPC (Session replaces all `/tmp/*.json`).

### Locking

- Session writes are thread-safe (internal lock on `__setattr__`).
- Agent-owned transient state (mid-LLM-call scratchpad) stays local.

---

## <a id="migration"></a>10. Migration sequence (bottom-up, safe)

### Phase 0 — land what we have (this branch, before refactor)

Already done this session:
- ✅ Canonical identity at download (schema + LLM + 3-layer flow)
- ✅ Consciousness loop gated off (evolution.enabled)

Commit + test on `refactor/v8-agent-separation`.

### Phase 1 — Session class (foundation)

Introduce `agent/session_state.py`. Port `self.mood`, `self.tracks_played`, `self.current_set`, `self.user_intent`, `self.planner_directive`, `self.dj_directive`, chat_history into it. Replace direct reads/writes across codebase. Delete `STATE_FILE`, `MOOD_CHANGE_FILE`, `DIRECTIVE_FILE`. Keep daemon running through the swap (property-level, one at a time).

**Success:** `session.json` is the only file on disk that holds live state. TUI reads it. Agents read/write it.

### Phase 2 — Mood as profile

Implement LLM mood resolver (see `project_dj_mood_profile.md`). On `session.mood = X` write, Being invokes mood LLM, writes `session.mood_profile`. Planner/DJ/Library read `mood_profile`, not raw `mood`.

**Success:** `BollyAffro` typo correctly resolves to `bollyafro` canonical. Silent wrong-genre bug gone.

### Phase 3 — Planner cleanup

- Delete `_load_next_on_idle` and `_auto_load_track` from `PlannerMixin`.
- Delete all SQL filter branches from `_run_planner` (mood soft filter, genre override, originals preference, random fallback).
- Change planner output from markdown to structured JSON written to `session.playlist`.
- Remove `search_music` / `download_track` / producer sub-agent from planner's tool set.
- Make replan event-driven (watch `session.replan_requested`, mood change, etc.).

Track loading is temporarily broken — next phase fixes.

**Success:** Planner's LLM output is structured JSON matching the schema in §4. Validated end-to-end.

### Phase 4 — DJ agent track-selection authority

- Add playlist-reading to DJ's context.
- Move `load_track` tool to DJ.
- DJ picks from playlist, loads, schedules transition.
- Heartbeat P4 updated to pass playlist in invocation.
- Heartbeat P4 gate: skip when `sched_file.exists()` (stop wasteful invocations).

Loading works again, now via DJ.

**Success:** Track selection goes: Planner playlist → DJ picks rank N → DJ loads. No SQL in between.

### Phase 5 — Library manager elevation

- Extract library agent from DJ sub-agent chain, make independent thread.
- Library watches `session.library_need`.
- Proactive gap analysis on mood change.
- Remove `search_music` / `download_track` from DJ and Being tool sets (except Being retains for direct user requests).

**Success:** Library fills itself without planner intervention. Planner only emits signals.

### Phase 6 — Producer peer

- Producer becomes independent thread.
- Single canonical instance (delete duplicate in DJ + Planner sub-agent chains).
- Watches `session.producer_need`.
- Proactive gap sensing.

**Success:** Producer generates tracks when library needs them, not when planner asks.

### Phase 7 — Heartbeat slim-down

- Delete `_run_planner`-triggering code from heartbeat (planner is self-driven now).
- Delete reflection-thread triggers (Being owns self-reflection).
- Keep P1 (silence), P3 (scheduled execution), P2 (auto-trans safety net).
- Dynamic sleep based on state.

**Success:** Heartbeat is a <200-line file focused on execution + safety.

### Phase 8 — cleanup + observability

- Delete unused files: `_archive/`, stale `/tmp/dj-treta-*.json` paths.
- Add structured logging per agent.
- TUI reads Session directly.
- Tests — ensure all 62+ tests still pass, add new ones for Session contracts.

---

## <a id="open-questions"></a>11. Open questions

- [ ] How does DJ see the playlist — poll every tick, or signal-driven? (Probably signal: `playlist_updated_at` changed since DJ's last look.)
- [ ] What if DJ's pick and planner's rank-1 differ — do we record that for learning? (Yes — feedback loop for later.)
- [ ] Should Session class be sync-write (blocking) or async-debounced (500ms)? Leaning async.
- [ ] How does Being agent invoke ripple through — does Being write to Session directly, or is there a Being-specific property set? (Direct writes to Session.)
- [ ] Rollback plan if v8 breaks live streaming? (Branch-based, main stays stable.)
- [ ] Heartbeat P4: move all P4 logic into DJ agent itself (DJ decides when to decide)? Too ambitious for Phase 4 — defer.
- [ ] Library manager quota — how much storage to allow before it stops downloading? (Add later, not Phase 5.)
- [ ] Producer cost cap — per-day budget on Lyria 3 calls? (`config.producer.max_per_day` — Phase 6.)

---

## Agent-by-agent one-liner summary

| Agent | v7 today | v8 target |
|---|---|---|
| **Being** | Chat + scattered tools | Chat, soul, mood resolver, cross-agent directives. Owns voice. |
| **Planner** | Markdown blob + SQL candidates + search + download + generate | Pure ranked-playlist advisor. Non-binding. Event-driven. |
| **Library** | Dormant sub-agent of DJ, bypassed | Independent peer. Owns search + download + canonicalize + enrich. Reactive + proactive. |
| **Producer** | Duplicated sub-agent (DJ + Planner) | Single independent peer. Owns Lyria 3. Reactive + proactive. |
| **DJ** | Transition timing only, Python picks tracks | Final authority: picks from planner playlist + decides transition. Can replan-request. |
| **Heartbeat** | 5 priorities with track selection SQL | 3 priorities — silence, scheduled execution, safety. No decisions. |
| **Session** | 3 drifting truth sources | Single class, auto-serialized, single file. Event bus via properties. |

---

## Principles (pinned)

1. **LLM decides, Python executes.** If you see an `if/else` that "picks" between options — it's wrong.
2. **One authority per concern.** Two agents never do the same job.
3. **One truth source.** Session class. No parallel state.
4. **Non-binding suggestions, final authority.** Planner suggests, DJ decides. Like djay / Traktor.
5. **Signals not calls.** Agents don't invoke each other; they read/write Session.
6. **Music never stops.** Heartbeat keeps the safety invariants even if agents fail.
