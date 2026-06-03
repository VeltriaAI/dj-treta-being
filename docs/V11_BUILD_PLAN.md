# DJ Treta v11 — Shared-Consciousness Substrate: Build Plan

*Implementation-ready, from the design workflow (run `wf_a0917256-48e`). Branch base: `v10`/`fix/v10-tier1-audit`.
Authority: `docs/V11_EVOLUTION_DECISION.md`. Substrate = append-only JSONL (`runtime_path("events.jsonl")`),
NOT SQLite (decision-doc locked; SQLite-unification = v12 future work, not a blocker).*

## Build order (ship each as its own merge; validate before the next)

### Phase 0 — Wire the free room-sense (hours, pure read-path, zero audio risk) — BUILD FIRST
Treta hears her **own mixer output** (energy/direction/density/tension/breakdown/buildup/drop, ~3 Hz, LLM-free)
as one terse advisory line in the DJ prompt. Decoupled from `relay.enabled`. **Validates the Flash
prompt-render budget before P1 widens it** — the program's central reliability bet, de-risked cheaply.
- `session_state.py`: `_FIELD_DEFAULTS` += `"room_sense": None` (NOT in `CRITICAL_FIELDS` — 3 Hz transient, 500ms debounce).
- `main.py`: new `_room_sense_loop` (its OWN `PerceptionEngine`, GET-only `/api/live` @0.5s, publish `session.room_sense` @2s); start thread **unconditionally** in `start()` before the relay block.
- `prompts.py`: `build_dj_user_message` gains `room_sense` kwarg → one **staleness-gated** (>15s → drop) line, single most-actionable tag (`[DROP]`/`[BREAKDOWN]`/`[BUILDUP]`).
- `heartbeat.py`: pass `room_sense=getattr(self.session,"room_sense",None)` at the one DJ call site.
- **GO:** `room_sense` non-None ~20s after boot even with `relay.enabled:false`; exactly one `ROOM (my output):` prompt line, zero when stale; no Mixxx writes from the loop; 30-min set, no glitch, no new error.

### Phase 1 — The Notebook substrate + per-agent read/write (1–2 days) — ship wake-dormant
Durable append-only event log (survives restart, unlike `thinking.log`) + cheap in-memory now-view projection.
All 5 agents' think/call auto-logged via the single `adk_runner._process_event` tap. Specialists read a terse
rendered slice in-prompt (≤3 lines); root Being reads richer via a `read_workspace` tool.
- NEW `agent/notebook.py` (`Notebook` class + `register_notebook`/`get_notebook`, own `threading.Lock`, append-one-line, in-memory ring deque(200), `replay()`, `now_view()`, `compact()`, salience-callback bus).
- Schema: `{seq monotonic gap-free across restart, ts, author, kind, payload, salience, confidence, event_id}`. Kinds: percept/decision/transition/claim/directive/generated_track/reflection.
- `_process_event` tap (think→percept, call→decision, schedule_transition/do_*→transition@0.9).
- NEW `read_workspace` tool in `tools/visibility.py` (Being-only); `prompts.py` `render_notebook_slice(max_lines=3)` into DJ + planner (v8+v9); `main.py` instantiate+replay+register, do NOT truncate events.jsonl on reset.
- **GO:** log grows during a set; kill-9→replay seeds monotonic seq (no reset), file not truncated; `read_workspace` returns from Being; **Flash tool-drop rate ≤ pre-P1 baseline** (else shrink the band, don't ship); append never blocks behind Session flush (separate lock).

### Phase 2 — Salience + autonomous wake (1 day) — flip `evolution.enabled` LAST
High-salience notebook appends (crowd-collapse, drop-landed, skip-burst, human directive, contradiction) wake
the Being off-cadence via the proven `_on_mood_change` thread-offload pattern. Closes the
`self_suggestion`-dies-in-5-min gap.
- NEW `agent/salience.py` (pure `score_event`, `WAKE_THRESHOLD=0.70`) + NEW `agent/suppressor.py` (pure-function vetoes; music-never-stops sits ABOVE, never routed through veto).
- `session_state.py` += `"last_event": None`; `notebook.append` mirrors row → existing callback bus carries wake.
- `main.py`: gated `if evolution.enabled:` register `_on_event` (score→wake_veto→cooldown/`_wake_in_flight`→`being-wake` thread→`_invoke_being`). `prompts.py` `build_wake_user_message`. `heartbeat.py` P4 ladder → single `p4_veto()` (behavior-preserving, diff-tested, riskiest edit — do last).
- Anti-duplication: wake output tagged `author="being:wake"` (vetoed from re-trigger); `_wake_in_flight` serialize; 60s cooldown; reflection/journal/intention loops unchanged (they already run unconditionally).
- **GO:** flag false → zero overhead, P0/P1 identical; flag true → synthetic skip-burst fires exactly ONE wake within cooldown; human directive pierces Sarathi veto; autonomous wake during manish_in_motion vetoed; no wake echo loop.

**Phase 3 (Jetson crowd sense) — hardware-deferred.** `crowd_pulse` field + P4 crowd gate are schema-reserved
(`now_view`→None, salience LOW); build only after P2 soaks live.

## File-ownership partition (parallel-build safety)
**Cross-phase, single-owner, NEVER parallel-split:** `prompts.py`, `heartbeat.py`, `main.py`, `session_state.py`
(touched in P0+P1+P2 — one integrator owns each across the whole build).
**Parallelizable within P1:** `notebook.py` (build + freeze API FIRST), then `adk_runner.py`,
`tools/visibility.py`+`__init__.py`, `agents.py`, `being_heartbeat.py`, `planner_loop.py`, `library_loop.py`.
**Parallelizable within P2:** `salience.py` + `suppressor.py` (fully isolated).

## Prior-art folded in / over-engineering rejected
**In:** monotonic seq + event_id (idempotency), optional correlation_id, now-view as pure derived projection,
**three-tier read (LLM never sees raw log — now-view + ≤10min/≤15-event band, drop lowest-salience first)**,
claims resolved by seq order (lock-free), salience-gated wake as a threshold check, suppressor as one-module veto.
**Rejected:** SQLite substrate (v12 unify later), CQRS/sagas, Kafka/Redis/brokers, generic snapshot engine,
OpenTelemetry, async pub-sub lib. Single-process daemon → in-memory ring + file IS the bus.

## Top risks → mitigations
- **Flash prompt-bloat → dropped tool calls (HIGH):** render ≤3 lines, `""` when quiet, raw log never injected, drop-lowest-salience-first; P0 validates budget on one line first; GO gate measures drop-rate vs baseline.
- **Audio-thread safety (HIGH):** every notebook write try/except-pass; P1/P2 silence+emergency never read notebook + sit above P4; room-sense loop GET-only; replay pure in-memory; callbacks spawn threads, never block.
- **Loop duplication (MED):** reflection/journal/intention already run unconditionally — wake is the only event-driven member, gives self_suggestion a faster consumer, not a 2nd writer.
- **Hot-path lock (MED):** Notebook owns its OWN lock, append = one line, compaction hourly off hot path.
- **evolution.enabled blast radius (MED):** ship P2 flag-false (zero overhead), flip only after P1 soaks a live set; single reversible config change.
