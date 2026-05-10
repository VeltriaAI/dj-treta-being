# DJ Treta v10 — Living Draft

**Started:** 2026-05-03
**Status:** Open notebook. Add findings as they surface. Do NOT implement from this yet.
**Predecessors:** `REFACTOR_PLAN.md` (v8 agent separation), `docs/ROCK_SOLID_IMPLEMENTATION.md`

---

## Why this doc exists

We have been vibe-coding through v6 → v7 → v8 → v9.3. Fast iteration, real ship. The cost: architectural debt that we know about but keep deferring because "fixing it right" means destabilising the running Being and rebuilding. That tradeoff has been correct so far — shipping > purity.

This doc is the **dumping ground** for everything we'd want to fix when we eventually pay the debt down. We keep writing into it. When the cost of *not* fixing exceeds the cost of the rebuild, v10 happens. Until then, it's a notebook.

**Rules of this doc:**
- Append-only by default. Only edit existing entries to mark resolved or add evidence.
- Every finding gets: **what's wrong**, **evidence (a real bug/incident, not theory)**, **proposed fix**, **blast radius**.
- "Vibe-coded for a reason" entries are valid too — note things we'd keep, not just things we'd change.

---

## Reality check (2026-05-03)

We are NOT rebuilding now. v9.3 is live, music is playing, real users (us) depend on it. v10 is the *destination*, not the next sprint. The next sprints are still feature work + targeted fixes inside the current architecture.

**Trigger conditions for actually starting v10:**
- (a) A class of bugs we can't fix without architectural change keeps recurring (currently: state races, planner/DJ desync — getting close).
- (b) A new capability we want is blocked by current shape (none yet).
- (c) Onboarding a second contributor and the mixin god-class is in the way.

Until at least one of these fires, this doc just grows.

---

## Architectural findings

### F1 — Mixin pile-up is a god-object in disguise
**What:** 13 mixins composed onto one Being class in `agent/main.py`. Heartbeat / Planner / Library / Producer / Transition / Session / Commands / ADKRunner / Evolution / WSServer / BeingHeartbeat / Sets / (+ base).
**Evidence:** Every mixin reads/writes `self.*` shared state. Mixin import order in `main.py` is load-bearing but invisible. Locating mutations of `self.tracks_played` requires a cross-file grep.
**Fix:** Composition. A small `Being` core holding named subsystems (`session`, `planner`, `mixer`, `library`, `producer`, `knowledge`, `transport`) as attributes, each a plain class with an explicit interface.
**Blast radius:** High. Touches `agent/main.py` and every mixin file. Cannot be done incrementally without an adapter layer.
**Status:** Open. Defer until trigger (c) fires or F3 forces it.

### F2 — One process owns everything (no isolation)
**What:** DJ loop, planner loop, library downloader, producer (model calls), WS server, heartbeat, evolution — all in one Python process.
**Evidence:** Render thread blocking on Mixxx HTTP — already band-aided with `allow_blocking=False` (see `feedback_tui_render_thread_no_blocking`). Library download stalls have starved planner. Producer crashes have killed the music.
**Fix:** Process split — **agent process** (DJ + planner + Mixxx control = hot path) vs **support process(es)** (library downloads, producer generation, knowledge ingestion). Local socket / Unix domain socket between them.
**Blast radius:** Medium-high. New IPC layer + supervisor. But mostly additive — hot path code barely changes.
**Status:** Open. **Highest reliability ROI.** Likely the first v10 milestone we'd actually ship.

### F3 — Inter-agent comms via shared JSON files (directives)
**What:** Planner ↔ DJ agent communicate by writing/reading directive files through tool calls. No schema, no ordering, no replay log.
**Evidence:** BUG-4 (skip signal needs tool_calls-aware clearing). `session.json` race requiring stop → clear → start ordering. Planner footguns (empty playlists + emergency-load loops). All recorded in auto-memory.
**Fix:** Typed message bus — versioned message structs over an in-process queue (or socket if F2 lands first). Single writer per topic.
**Blast radius:** Medium. Replaces ~12 directive tools + the JSON file dance.
**Status:** Open. Pairs naturally with F2.

### F4 — State lives in too many places
**What:** SQLite (`djtreta.db`) + LanceDB + `session.json` + `/tmp/dj-treta-*.json` + learnings file + filesystem track library + directive files.
**Evidence:** Repeated stale-read and race incidents. No single source of truth means every fix is local.
**Fix:** Hierarchy —
- Durable canonical → SQLite (sets, tracks, learnings)
- Live runtime → one in-memory `Session` object, single-writer snapshot to one file
- Knowledge → LanceDB read-only from runtime's POV
- Kill the rest, or demote them to rebuildable caches.

**Blast radius:** Medium. Touches every persistence call site but mechanical.
**Status:** Open. Do alongside F3.

### F5 — Tool surface is too wide and uneven
**What:** ~3.5k LOC across 12 tool modules. `transitions.py` is 1,298 lines — an engine masquerading as a tool module. DJ agent currently sees ~30+ tools across mixer/library/perception/producer/meta/directives/evolution/spawn.
**Evidence:** Recurring prompt-tightening commits. LLM occasionally picks the wrong tool tier (e.g. invokes evolution when asked to skip a track — anecdotal, watch for it).
**Fix:** Two moves —
1. Move engines out of `tools/`. Expose 2–3 thin tools, keep the engine in `agent/transitions_engine.py`.
2. Tier the toolset per agent. DJ agent does not need evolution/spawn in-loop.

**Blast radius:** Low-medium. Mostly file moves + import updates + prompt edits.
**Status:** Open. Cheap. Can ship inside current architecture without waiting for v10.

---

## Things we'd KEEP from current architecture

- **Two-agent split (planner = slow strategic, DJ = fast reactive).** Right decomposition. Don't collapse it.
- **MCP server mirroring the tool surface.** Same capability, different transport — clean.
- **Operator-mode install (`/opt/djclaw` + systemd templates).** Right shape for a long-lived Being. Keep.
- **Evolution as a first-class subsystem.** Most projects bolt this on later — we built it in.
- **Eval suites + unit tests in `tests/`.** Already-paid investment, port forward.
- **Being-as-LLM-orchestrator philosophy.** "LLM decides. Python executes." (from REFACTOR_PLAN.md) is the right axis. v10 is about cleaner *plumbing*, not changing this thesis.

---

## Open questions for v10 design

1. **Process model:** single-process with threads? multi-process with IPC? actor model? Pick one and commit.
2. **Directive bus transport:** in-process queue, Unix socket, Redis, or SQLite as a queue? Cost vs ops surface.
3. **Mixin replacement strategy:** big-bang rewrite vs strangler-fig (new core wraps old mixins, peel off one at a time)? Strangler is slower but keeps music playing.
4. **Test forward-compat:** how much of the current eval/unit suite carries over without rewrite? Probably eval/* yes (black-box), test/* mixed.
5. **State migration:** SQLite schema is durable — keep. JSON state files are transient — drop. What about learnings file?
6. **Backwards compat for operators:** `/opt/djclaw` layout, systemd unit names, config schema — breaking these breaks every existing deploy. Default: keep paths stable, change internals.

---

## Cost-of-rebuild estimate (rough, update as we learn)

- F1 + F3 + F4 together (the entangled trio): ~2–3 weeks focused, with music staying live via strangler-fig. Probably double if we try big-bang.
- F2 (process split): ~3–5 days standalone if F1 is untouched, more if combined.
- F5 (tool reshuffle): ~1 day, doable any time, no v10 dependency.

**Total v10 budget guess:** 3–4 focused weeks. Real number: 6–8 weeks given normal interruption rate.

---

## Findings backlog (add freely below)

<!-- Append new findings here. Use the F1–Fn template. Don't reorganise — that's a v10-day job. -->

---

## Deep-dive #2 — Transitions engine (2026-05-04)

Files: `agent/tools/transitions.py` (1,343 LOC), `agent/transitions.py` (151 LOC mixin), `agent/playback_applier.py` (172 LOC). 7 transition styles (`do_transition`, `do_bass_swap`, `do_filter_sweep`, `do_hard_cut`, `do_echo_out`, `do_riser`, `do_dissolve`) plus `schedule_transition`, `_wait_phrase_boundary`, `_tempo_ride`, `_apply_bpm_after`.

### End-to-end map
LLM (DJ agent) calls `schedule_transition(to_deck, at_position, technique, duration, ...)` which writes `scheduled-transition.json`. Heartbeat P3 picks up the file, fires `_execute_scheduled_transition(sched)` in a daemon thread. That polls Mixxx status until `at_position` reached (adaptive 5s/2s/0.5s/0.2s sleeps), then dispatches via `if/elif technique == "..."` to one of 7 `do_*` functions. Each `do_*` runs synchronously in the executor thread (10–120s blocking), driving Mixxx via HTTP through `_mixxx_post`/`_mixxx_get` helpers. On finish, the executor writes `idle_needs_load = True` to Session and unlinks lock files. `playback_applier.load_on_deck` is the parallel low-level loader DJ agent calls via `load_track` tool. State touched: `scheduled-transition.json`, `transition-pending.lock`, `self._transition_pending`, `session.replan_requested`, `session.idle_needs_load`, `current_set.energy_arc`. Async story: synchronous tool body inside a daemon thread, with an inner daemon thread for bass-restore. No supervisor, no cancellation.

### Findings

**F18 — Pre-flight boilerplate duplicated 7 times across `do_*`**
What: Every `do_*` opens with the same 3-step gate: status fetch, "no track loaded" abort, "<30s remaining" abort. Strings drift slightly (e.g. "Load a track first with load_track" vs "Load a track first" vs "Deck X has no track loaded!").
Evidence: `tools/transitions.py:162–171, 295–304, 425–431, 522–528, 675–683, 978–986, 1096–1104` (7 copies).
Fix: One `_preflight_or_abort(to_deck, min_remaining=30) -> str | None` helper. Each do_* becomes `if err := _preflight_or_abort(...): return err`.
Blast: Trivial. Status: Open / pre-v10.

**F19 — Post-flight cleanup duplicated 5+ times**
What: Crossfader → final position, pause out_deck, volume reset both decks, EQ reset both decks, eject out_deck, optional `_apply_bpm_after`. Same dance, slight ordering drift between styles.
Evidence: `transitions.py:256–272, 384–405, 1052–1075` (and similar in echo/riser/dissolve).
Fix: `_postflight(to_deck, out_deck, bpm_after, glide_duration)`. Style-specific cleanup (eject yes/no, volume yes/no) becomes a kwargs flag.
Blast: Trivial. Status: Open / pre-v10.

**F20 — Scheduled-transition dispatch drops `bpm_after` / `glide_duration` / `duration_bars`**
What: `_execute_scheduled_transition` reads `technique` and `duration` from the schedule JSON but the dispatch (`do_bass_swap(to_deck, duration)` etc.) never passes the BPM/glide/bars args. Agent's intent silently ignored if it set them via `schedule_transition`.
Evidence: `agent/transitions.py:100–113`. Compare to `do_*` signatures in `tools/transitions.py:143, 275, 408, ...`.
Fix: Persist all kwargs in the schedule file; dispatch with `**sched_kwargs`.
Blast: Trivial. Status: Open / pre-v10.

**F21 — 7+ `TODO live-validate` comments without owner or date**
What: Multiple unverified assumptions about Mixxx-fork control names (`beat_distance`, `beat_active`, `parameter2` on QuickEffectRack1, Echo slot index, resonance neutral default).
Evidence: `tools/transitions.py:24, 447, 471, 491, 533, 588, 612, 1040`.
Fix: Audit each against current Mixxx-fork build (`mixxx-treta` repo), pin a comment to the actual control reference, or wrap with a feature-detect probe at startup.
Blast: Low. Status: Open / pre-v10. **This is the biggest reliability foot-gun in the engine** — silent failure mode.

**F22 — Bass-restore daemon thread is fire-and-forget**
What: `do_transition` spawns a daemon thread that sleeps `duration*0.7` seconds and restores incoming LO. If the main `/api/transition` errors mid-fade or the watchdog forces an early end (Patch C), the bass-restore still fires later, possibly clobbering the next track's EQ.
Evidence: `tools/transitions.py:211–216, 226–251`. The "make sure bass-restore has run" line at 254 papers over this but the thread keeps running.
Fix: Use a `threading.Event` cancel signal; the watchdog forced-end branch sets it; the bass-restore checks it before posting.
Blast: Low. Status: Open / pre-v10.

**F23 — `tools/transitions.py` is the engine, not a tool module (validates F5)**
What: 1,343 LOC, 11 functions, internal helpers, threading, polling loops. Compare to `tools/library.py` (32 LOC) — both live under `tools/` but they are fundamentally different things.
Evidence: `tools/transitions.py` whole file vs `tools/library.py`.
Fix: Move to `agent/transitions_engine.py`. Keep ~3 thin tool wrappers in `tools/transitions.py` that the LLM sees: `schedule_transition`, `cancel_transition`, `transition_status`. Engine functions become internal — LLM never calls them directly.
Blast: Medium. Touches imports across heartbeat, mixin, and `agent/transitions.py`. Status: Open / **v10**.

**F24 — `bpm_after` is a string-typed enum**
What: Accepts `"keep"`, `"reset"`, or a string-encoded float like `"126.5"`. Every call site does `try: float(bpm_after)`.
Evidence: `tools/transitions.py:118–140`.
Fix: Union type — `Literal["keep", "reset"] | float`. Or add a separate `target_bpm: float | None` arg.
Blast: Low. Status: Open / pre-v10.

**F25 — Unconditional `eject` of outgoing deck after every transition**
What: Every `do_*` ends with `_mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})`. Combined with the new `idle_needs_load = True` post-transition, this guarantees the next track has to be selected + loaded fresh — Mixxx's pre-cue/analysis advantage is wasted.
Evidence: `tools/transitions.py:270, 403`, mirrored in echo/riser/dissolve. `agent/transitions.py:147` then sets idle_needs_load.
Fix: Don't eject. Let DJ pick whether to keep the outgoing on its deck (for return-to-track tricks) or have planner pre-load on next signal. `_load_next_on_idle` already overwrites the deck content — eject is redundant.
Blast: Low. Status: Open / **possibly v10** — needs musical judgement.

**F26 — `do_bass_swap` runs a 10fps frame loop blocking executor thread for full duration**
What: 343–382, body runs `total = duration * 10` iterations posting volume/eq/eq updates with `_time.sleep(0.1)` between each. For a 90s swap that's 900 iterations with 900 HTTP posts. Same pattern in `do_filter_sweep`, `do_riser`, `do_dissolve`.
Evidence: `tools/transitions.py:343–382, 906, 1055, 1128`.
Fix: Push the per-frame work into the Mixxx fork as a single command (e.g. `/api/eq_curve { deck, lo: [start, end, duration] }`). Engine emits one HTTP call, Mixxx's audio thread does the curve. Eliminates 90% of per-transition HTTP traffic.
Blast: Medium-high (Mixxx-fork change). Status: Open / **v10** — pairs with mixxx-treta upstream debt.

**F27 — `resolve_track_path` 4-fallback resolution is overgenerous**
What: Falls through to fuzzy normalized-name search if basename misses. `query in normalized(stem)` is a substring match — a query like "live" matches dozens of tracks. Silent wrong-track risk when the canonical path is stale.
Evidence: `playback_applier.py:78–94`.
Fix: Drop fallback 4 (fuzzy). If basename miss, log + return None; let canonicalization (F12) be the durable fix.
Blast: Low. Status: Open / pre-v10. Risk gate: do this only after F12 lands so we don't break cross-machine loads.

**F28 — `refresh_duration` blocks 1s on every load**
What: Hardcoded `time.sleep(1.0)` at the top "give Mixxx a moment to parse." Every track load eats 1s on a thread that could be doing work.
Evidence: `playback_applier.py:161`.
Fix: Poll `/api/deck/N/track_info` with bounded retries (e.g. 4 × 250ms) until duration > 0.
Blast: Trivial. Status: Open / pre-v10.

**F29 — Magic numbers throughout, no named constants**
What: 30s remaining threshold, 0.7 bass restore ratio, 0.9 swap-end ratio, 0.05 phrase threshold, 0.02 poll interval, fps=10, 0.3/0.1/0.2 sleeps. Scattered across all `do_*`.
Evidence: `tools/transitions.py` passim.
Fix: Module-level constants block at top: `MIN_REMAINING_S = 30`, `BASS_RESTORE_RATIO = 0.7`, `PHRASE_BOUNDARY_THRESHOLD = 0.05`, etc.
Blast: Trivial. Status: Open / pre-v10.

**F30 — Error path is string-parsing**
What: Each `do_*` returns either `"Transitioned to..."` or `"ABORTED: ..."`. The DJ agent reads back the LLM-readable string but Python callers (heartbeat, scheduled executor) only `log.info(result[:200])` — they have no programmatic way to know if it succeeded.
Evidence: `tools/transitions.py:272, 405`; `agent/transitions.py:114, 138`.
Fix: Return a `TransitionResult` dataclass `{ok: bool, message: str, technique: str, duration_actual: float}`. Tool layer can stringify for the LLM; Python callers branch on `ok`.
Blast: Low. Status: Open / pre-v10.

**F31 — Patch A/B/C in scheduled executor are load-bearing safety nets, document them as such**
What: `_execute_scheduled_transition` has three named patches: stale-schedule abort, fire-time overshoot guard, crossfade end-safety belt. These are critical — without them, the listener hears cut-outs or replays. They're named in comments but easy to delete in a future refactor that doesn't read closely.
Evidence: `agent/transitions.py:48–94`; `tools/transitions.py:221–251`.
Fix: Move to a `safety/` module with each patch as a named function with a unit test. v10 architecture preserves these as first-class invariants.
Blast: Low (document) / Medium (refactor). Status: Open / **v10 must-keep**.

**F32 — `do_hard_cut` waits for `beat_active` polling at 20ms intervals up to 2s**
What: `_wait_phrase_boundary` is fine at 2s timeout, but for hard_cut's `align="downbeat"` path the entire transition can be ~2s of polling for a single fader move.
Evidence: `tools/transitions.py:533–549`.
Fix: Same as F26 — push downbeat alignment into Mixxx-fork (`/api/snap_crossfade { deck, target, align: "downbeat" }`).
Blast: Medium. Status: Open / v10.

### What we'd KEEP

- **The 7-style transition library itself** — these are real DJ techniques validated by listening, not inventions. Don't collapse them.
- **Patch A/B/C safety nets** in the scheduled executor (F31) — battle-earned.
- **`schedule_transition` as the LLM's only entry point** — already correct: LLM names the moment + technique, Python handles timing.
- **`_wait_phrase_boundary`'s graceful timeout** — fires anyway if no boundary found. "Music never stops" in spirit.
- **`playback_applier.resolve_track_path` (with F27 fix)** — cross-machine portability is a real need.

---

## Deep-dive #3 — State + persistence (2026-05-04)

Files: `agent/session_state.py` (403, the `Session` class — single-source-of-truth), `agent/session.py` (294, `SessionMixin`), `agent/db.py` (648, SQLite layer), `agent/runtime_paths.py` (57), `agent/config.py` (357).

### End-to-end map
The `Session` class (`session_state.py`) is the runtime in-memory state. 25 declared fields (`_FIELD_DEFAULTS`), threading.RLock around all mutations, dirty flag flipped on writes, background flush thread debounces 500ms. 8 fields in `CRITICAL_FIELDS` flush *synchronously inside the write lock* on each mutation. Lists are wrapped in `ObservedList` so `.append()` triggers dirty. Persisted to `.beings/session.json` via tmp+os.replace (atomic). Module-level singleton via `register_session(...)` so LLM-invoked tools can reach the live Session without holding a Being reference. `SessionMixin` writes a *separate* derived `state.json` every 2s (TUI snapshot) and force-flushes Session every 10s as belt-and-suspenders. `db.py` is SQLite (WAL), one fresh connection per call, 30+ DAL functions, schema migrated in-place via `_migrate_tracks_canonical`. `runtime_paths.runtime_path(name)` resolves the IPC dir once via `lru_cache(1)`. `config.py` is dataclasses + YAML + env layering.

### Findings

**F33 — `ObservedList` only catches list-level mutations, not nested-dict mutations**
What: `tracks_played[0]["title"] = "x"` does NOT trigger a flush. Only `.append`/`.extend`/`.pop`/`__setitem__` on the list itself fire. Most callers happen to replace the dict (`current_set = dict(...)` pattern in main.py:489) which works — but `tracks_played[i]["played_at"] = time.time()` would silently not persist.
Evidence: `session_state.py:53–106`. Compare main.py:489–492 (replace pattern, correct) vs heartbeat closures that mutate dict-in-list.
Fix: Document the constraint clearly OR wrap dicts in an `ObservedDict` that also fires on key set. Pragmatic: document — replacing dicts is the established pattern.
Blast: Low (doc) / Medium (full observed-dict). Status: Open / pre-v10.

**F34 — Critical-field synchronous flush holds the lock through fs write**
What: When you set `session.mood = "x"`, the inside of `__setattr__` does `_flush_locked()` which calls `json.dumps(self.to_dict())` + tmp write + rename — all while holding `_lock`. With ~10 threads potentially reading session, one mood write blocks them all for tens-of-ms (more on slow disk). Repeated mood/idle_needs_load writes during a transition compound.
Evidence: `session_state.py:236–240, 283–295`.
Fix: Snapshot to dict inside the lock, release, then write outside. Rename is still atomic. Trades a few ms of staleness in another reader against shorter lock hold.
Blast: Low. Status: Open / pre-v10.

**F35 — `_deep_equal` walks the entire structure on every write**
What: To skip no-op writes the setter calls `_deep_equal(old, value)`. For `tracks_played` with N=247 entries, that's an O(N) walk on every list assignment (replacement, not append). Same for nested set-dicts.
Evidence: `session_state.py:220, 358–371`.
Fix: For ObservedList, identity-compare first (`old is value`); fall through to deep only if they differ. For dicts, compare top-level keys first, then recurse.
Blast: Trivial. Status: Open / pre-v10. Low ROI but cheap.

**F36 — Schema has no version; field typo silently writes a new key**
What: `_FIELD_DEFAULTS` is the schema. Setting an undeclared field warns once but still persists. Fields removed from defaults stay in the JSON forever (loaded back into Session on next start? No — `Session.load` only sets fields *in* defaults). Adding/removing a field is invisible to consumers.
Evidence: `session_state.py:215–216, 336–342`.
Fix: Add `_SCHEMA_VERSION = 1` constant, persist alongside; on load, if version mismatch, run migration table. Strict mode for unknown writes (raise vs warn) togglable for tests.
Blast: Low. Status: Open / **v10** — comes naturally with state-consolidation work.

**F37 — `register_callback` has no `unregister`**
What: Tests/long-running consumers can register; nothing can deregister. Each test that registers a callback leaks; full test suite accumulates a chain of stale callbacks against the singleton.
Evidence: `session_state.py:254–261`.
Fix: Return a token from register; provide `unregister(field, token)`. Trivial.
Blast: Trivial. Status: Open / pre-v10.

**F38 — Module-level `register_session` warns then replaces silently**
What: Tests that instantiate a Session in a fixture and don't tear it down register it; next test's fresh Session replaces the singleton. Consumers holding a reference (e.g. an MCP request mid-flight) suddenly read the wrong session.
Evidence: `session_state.py:380–402`.
Fix: Add `clear_session()` for test teardown. Document the invariant: one Session per process, registered exactly once outside tests.
Blast: Trivial. Status: Open / pre-v10.

**F39 — `STATE_FILE` and `PERSIST_FILE` constants in session.py are dead**
What: `session.py:13–14` defines module-level paths but Session has its own; only `STATE_FILE.write_text(...)` at line 137 still uses one. `PERSIST_FILE` is fully dead — also dead in `main.py:58` (F-extension of F: dead globals across boot+state).
Evidence: `session.py:13–14, 137`. Compare `main.py:58`.
Fix: Delete `PERSIST_FILE` from both. Move `STATE_FILE` constant inside `_write_state` or to `runtime_paths`.
Blast: Trivial. Status: Open / pre-v10.

**F40 — `_state_loop` writes both Session.flush AND STATE_FILE at 2s/10s cadence**
What: `state.json` is the TUI snapshot, regenerated every 2s with full Mixxx HTTP fetches inside. `_save_session()` is `Session.flush()`-as-checkpoint every 10s. Both are belt-and-suspenders against the actual auto-flush. Three persistence layers for the same data: (a) Session auto-flush, (b) explicit checkpoint, (c) derived state.json.
Evidence: `session.py:160–174`. Compare `_flush_loop` in `session_state.py:297–305`.
Fix: Drop the 10s explicit checkpoint — Session's auto-flush + atexit handler covers it. State.json regeneration stays (TUI fallback when WS disconnects).
Blast: Trivial. Status: Open / pre-v10.

**F41 — `db.py`: fresh connection per call, no pool**
What: Every DAL function calls `get_db()` → `sqlite3.connect()`. SQLite WAL handles it, but heartbeat tick can issue ~5 calls in <100ms, each opening + closing a connection.
Evidence: `db.py:47–52, 267–304, 307–...`. Plus 30+ similar functions.
Fix: Module-level connection pool keyed by thread (sqlite is connection-per-thread anyway). Or use `contextlib.contextmanager` and `threading.local` to cache one per thread.
Blast: Low. Status: Open / pre-v10. Modest ROI.

**F42 — `_normalize_track_path` re-loads config on every call**
What: Called inside `upsert_track`. Each call does `from .config import load_config; load_config()` which parses YAML, env, defaults. During `scan_library` this fires per track.
Evidence: `db.py:246–264, 275`.
Fix: Module-level `@lru_cache` or pass `music_dir` in. The lazy import is fine; the lazy *load* is the issue.
Blast: Trivial. Status: Open / pre-v10.

**F43 — `_migrate_tracks_canonical` runs on every `init_db()`**
What: PRAGMA + ALTER + CREATE INDEX called on every cold start. Idempotent (column-existence checks first), but it's ~3 round-trips that aren't gated.
Evidence: `db.py:207, 212–243`.
Fix: After successful migration write `PRAGMA user_version = 1`. Skip if already at version 1. Standard SQLite pattern.
Blast: Trivial. Status: Open / pre-v10. Pairs with F36 schema versioning for Session.

**F44 — `runtime_dir()` is `lru_cache(1)` — env changes mid-process don't take effect**
What: Cache means tests that mutate `DJTRETA_RUNTIME_DIR` between runs see stale dir. Comment says "call cache_clear() to refresh" but no test fixture does.
Evidence: `runtime_paths.py:27–48`.
Fix: Add a `pytest` fixture that calls `runtime_dir.cache_clear()` between tests. Or drop the cache (env lookup is microseconds).
Blast: Trivial. Status: Open / pre-v10.

**F45 — `pulse_interval_seconds: float = 5.0  # legacy` still in dataclass**
What: Field marked legacy but still loaded from YAML and used? Not yet known. Dead-code candidate.
Evidence: `config.py:49`.
Fix: Grep for usage; if zero callers, delete and bump schema (or just delete — yaml extra fields don't crash).
Blast: Trivial. Status: Open / pre-v10.

**F46 — Config has no startup validation**
What: `model: openai/gemini-3-flash`, empty `api_key`, wrong `api_base` — no probe at load time. First failure shows up mid-flight as a tool call exception.
Evidence: `config.py:21–27`. Boot-time probe exists for LiteLLM reachability (`main.py:_litellm_reachable`) but it's a status check, not a model availability check.
Fix: At startup, after `_ensure_litellm`, probe `/v1/models` and warn if `model` not listed. Cheap.
Blast: Trivial. Status: Open / pre-v10.

**F47 — `Session` flush thread starts before init completes (tiny race)**
What: `__init__` starts the flush thread inside the loop that initializes fields. Flush wakes every 500ms, checks `_dirty` (False at init) — currently safe. But adding a default that's True-by-default in future would dirty the flag before all fields exist.
Evidence: `session_state.py:188–199`.
Fix: Move thread start to *end* of `__init__` after all fields populated.
Blast: Trivial. Status: Open / pre-v10.

**F48 — Atomicity: `os.replace(tmp, path)` is correct; serialization holds the lock unnecessarily**
What: Already noted in F34 but worth restating positively — the actual file write is atomic. The architectural correctness is here. The only fix is lock-hold reduction.
Evidence: `session_state.py:288–292`.
Status: Closed-as-noted (F34 is the actionable item).

**F49 — TUI state.json duplicates much of Session, plus Mixxx-derived fields**
What: `state.json` includes `mood`, `tracks_played` (count), `current_set`, `emergency_count` (which are in Session) AND `current_track`/`next_track` from Mixxx HTTP, AND `billing` from billing.json, AND scheduled_transition from scheduled-transition.json. Three sources unified into a fourth file every 2s. The WS state push (`_ws_broadcast("state", ...)`) writes the same payload.
Evidence: `session.py:111–142`.
Fix: state.json becomes a *transient* projection; in v10 the WS push is canonical and state.json is only written when WS isn't connected. Or — simpler — drop state.json entirely and have the TUI subscribe to WS only.
Blast: Low. Status: Open / pre-v10 (drop) or v10 (canonical projection).

### What we'd KEEP

- **Session as in-memory single-source-of-truth** with auto-persist — the architectural shape is right, only the lock-hold and schema-versioning need polish.
- **SQLite WAL + ON CONFLICT(path) DO UPDATE** — the upsert pattern is race-safe and the right level of durability.
- **`runtime_path()` indirection** — beats the old `/tmp/dj-treta-*` literals across modules.
- **Atomic file write via tmp + os.replace** — F48, idiomatic, keep.
- **`_FIELD_DEFAULTS` as the canonical declaration** — the typo-warning behaviour is good DX.

---

## Deep-dive #4 — Tools surface (2026-05-04)

12 modules, 3,518 LOC. Distribution: `transitions.py` 1,343 (38% — covered in slice 2), `perception.py` 392, `discovery.py` 369, `evolve.py` 246, `generation.py` 239, `mixxx.py` 235, `spawn.py` 178, `directives.py` 127, `meta.py` 119, `helpers.py` 91, `library.py` 32. `tools/__init__.py` 147 = imports + `__all__` re-export.

### End-to-end map
LLM calls `FunctionTool(func=...)` wrappers built by `agents.py:create_agents`. Tool functions are plain modules — no class, no shared state except a few module-level singletons (`_YTMUSIC_CLIENT`, `_spawn_loop`, `_spawn_results`). Most tools touch Mixxx via `_mixxx_get/_post` helpers which load config fresh each call. Directive tools mutate the `Session` singleton via `get_session()`. Spawn tools own their own asyncio loop + thread, separate from the Being's main loop.

### Findings

**F50 — `load_config()` called inside every `_mixxx_get`/`_mixxx_post`**
What: `helpers.py:_mixxx_get/_mixxx_post` re-loads config (YAML parse + env merge) on every Mixxx request. With ~10–20 Mixxx HTTP calls per heartbeat, that's a YAML parse per call.
Evidence: `helpers.py:60–80`.
Fix: Module-level cached config or pass `Config` in. Same fix shape as F42.
Blast: Trivial. Status: Open / pre-v10.

**F51 — `_YTMUSIC_CLIENT` global never refreshed, no error recovery**
What: First `search_music` lazy-instantiates an anonymous YTMusic client. If the client gets into a bad state (rate-limited, network blip), there's no reset.
Evidence: `discovery.py:17–25`.
Fix: Wrap calls in retry-with-fresh-client on certain exception classes.
Blast: Trivial. Status: Open / pre-v10.

**F52 — `spawn.py` runs its own asyncio loop + thread, separate from the Being's**
What: Module-level `_spawn_loop = asyncio.new_event_loop(); thread.start()`. The Being already has `self._loop`. Two loops in one process means two LiteLLM client instances, two GC pressures, two failure modes.
Evidence: `spawn.py:18–21`.
Fix: Use the Being's loop. The reason it was forked likely was tests (the Being's loop isn't available at module import). Solution: lazy-attach to `get_session()`'s loop, or have the caller pass the loop in.
Blast: Low. Status: Open / pre-v10. Reduces moving parts.

**F53 — `library.get_set_history` reads `state.json` (TUI snapshot) instead of Session**
What: Tool returns `data.get("tracks_played", [])` from the *derived* state.json file. But state.json's `tracks_played` is just a count (`len(self.tracks_played)`) per session.py:114, NOT the list. Tool likely returns an empty list always.
Evidence: `library.py:25–32`. Compare `session.py:114`.
Fix: Read directly from `Session.tracks_played` via `get_session()`. Reinforces F9 (file-as-IPC anti-pattern).
Blast: Trivial. Status: Open / **probable bug, fix in next PR**.

**F54 — Two different sandbox lists for write paths**
What: `meta.write_file` allows `.beings/`, `agent/`, `tests/`, `docs/`, `templates/`. `evolve._ALLOWED_SCOPES` allows `agent/`, `tests/`, `docs/`, `templates/`, plus three specific .beings/* files. SOUL.md is in `_READONLY_FILES` for evolve but `.beings/` is whole-tree-allowed in meta.write_file → the Being can `write_file(".beings/SOUL.md", ...)` and bypass the readonly guard.
Evidence: `meta.py:43–51`, `evolve.py:19–22`.
Fix: Single `WritePolicy` module with whitelist + readonly-overrides. All write tools route through it.
Blast: Low. Status: Open / **security-relevant, pre-v10**.

**F55 — `meta.write_file` is not atomic**
What: `path.write_text(content)` direct. Crash mid-write or out-of-disk leaves a truncated file. Worse, the Being could half-write its own SOUL.md / MEMORY.md.
Evidence: `meta.py:55`.
Fix: tmp + os.replace pattern (same as Session).
Blast: Trivial. Status: Open / pre-v10.

**F56 — `evolve.evolve` hardcodes `~/.local/bin/claude`**
What: Path to the Claude Code binary is fixed. No version check, no fallback to `which claude`, no timeout in signature.
Evidence: `evolve.py:15`.
Fix: `shutil.which("claude")` with fallback to env `CLAUDE_BIN`.
Blast: Trivial. Status: Open / pre-v10.

**F57 — `spawn._TOOL_SETS` is 4 hardcoded named sets**
What: `"research"`, `"analysis"`, `"production"`, `"introspection"` mapped to lists of tool name strings. New use cases require code edits to `_TOOL_SETS` AND `_resolve_tools` switch dict.
Evidence: `spawn.py:23–48`.
Fix: Define tool sets as YAML/JSON manifests in `~/.config/djclaw/tool_sets/`. Or — better — the Being passes a list of tool names at spawn time.
Blast: Low. Status: Open / pre-v10.

**F58 — `_dj_get/_dj_post` vs `_mixxx_get/_mixxx_post` — two near-identical helpers**
What: `_dj_*` is `_mixxx_*` plus an `error` field rewrite. Both used by tool modules; some callers use one, some the other. Drift risk.
Evidence: `helpers.py:54–91`.
Fix: One helper that returns a typed result (`MixxxResult`); callers introspect the type.
Blast: Trivial. Status: Open / pre-v10.

**F59 — `load_track` (mixxx tool) re-implements load logic instead of calling `load_on_deck`**
What: `mixxx.load_track` resolves path then `_mixxx_post("/api/load", ...)` directly. `playback_applier.load_on_deck` does the same plus logging + bool return + Mixxx-result inspection.
Evidence: `mixxx.py:34–58`. Compare `playback_applier.py:97–128`.
Fix: `load_track` becomes a thin wrapper around `load_on_deck`, returns string for the LLM.
Blast: Trivial. Status: Open / pre-v10.

**F60 — `set_volume` carries a comment-pinned API contract with no test**
What: `# Mixxx /api/volume expects {"deck", "level"} — NOT "volume"`. This was clearly a past bug. No test asserts the contract; if Mixxx fork ever changes, no signal.
Evidence: `mixxx.py:90`.
Fix: One contract test per Mixxx endpoint we depend on. Belongs with F21 (Mixxx-fork TODO live-validate).
Blast: Low. Status: Open / pre-v10.

**F61 — Directive writes are last-write-wins with no merge or queue**
What: `set_dj_directive("X")` overwrites whatever was there. If two callers (Being heartbeat + spawned subagent) both set directives in the same window, one is silently lost.
Evidence: `directives.py:33–46`.
Fix: For v10 typed message bus (F3), directives become an append-only queue with an explicit `clear` action. Pre-v10: log overwrite events at INFO so the loss is visible.
Blast: Low. Status: Open / **v10**, with logging fix pre-v10.

**F62 — Error returns are unstructured strings starting with "ERROR:"**
What: Same as F30 but for non-transition tools. LLM reads prose to know failure. Python callers (heartbeat, spawn result handler) can't programmatically branch.
Evidence: passim — `mixxx.py:48, 56`, `meta.py:14, 25`, `discovery.py`, etc.
Fix: Typed result protocol (e.g. dict with `ok: bool`). Tool layer can stringify for LLM.
Blast: Medium. Status: Open / pre-v10. Foundational for any future programmatic control.

**F63 — `meta.read_file` hardcoded 10,000-char truncation**
What: No offset/limit args. SOUL.md / MEMORY.md / large config files are silently truncated. The Being can't fully read its own identity.
Evidence: `meta.py:18–22`.
Fix: Add `offset: int = 0, limit: int = 10000` args and signal truncation explicitly in the response.
Blast: Trivial. Status: Open / pre-v10. **High-impact for self-awareness.**

**F64 — `_normalize_for_search` is load-bearing for fuzzy track resolution but untested**
What: Strips emojis + normalizes dashes + NFKC + lowercase. Used by `resolve_track_path` fallback (F27) and discovery search. Behaviour change here could cause silent track miss-matches across the system.
Evidence: `helpers.py:43–51`.
Fix: Pin behaviour with unit tests (table-driven inputs).
Blast: Trivial. Status: Open / pre-v10.

**F65 — `tools/__init__.py` duplicates imports and `__all__` listing**
What: 50+ imports plus matching 50+ entries in `__all__`. Adding a tool requires both. Drift inevitable.
Evidence: `tools/__init__.py` whole file.
Fix: Generate `__all__` from `dir()`-walk over each submodule, or use `importlib`.
Blast: Trivial. Status: Open / pre-v10. Cosmetic.

**F66 — `run_shell` is gated by capability but still arbitrary shell**
What: `meta.run_shell` is the kind of capability that, when on, opens the entire Being to LLM-induced command injection. Comment says it's gated; haven't verified the gate path.
Evidence: `meta.py:71–80` (head; tail not read).
Fix: Even with capability ON, run inside a constrained subprocess with PATH/cwd limits. Audit every prompt that mentions run_shell.
Blast: Low. Status: Open / pre-v10. Security-relevant.

### What we'd KEEP

- **Plain functions for ADK FunctionTool wrappers** — the simplicity is right; no need for tool classes.
- **`_resolve_tool_path` + allowed-roots check** in helpers — sandbox principle is right, just unify with F54.
- **`directives.py` shape (set/get/clear/defer)** — the verb set is correct; only the bus underneath needs upgrading (F61).
- **Lazy YT/Lyria client init** — keeps cold-start fast.
- **`library.py` is genuinely 32 LOC of thin tools** — not all tool modules need to be big.

---

## Deep-dive #5 — Planner / Library / Producer loops (2026-05-04)

Files: `agent/planner_loop.py` (920), `agent/library_loop.py` (364), `agent/producer_loop.py` (150). Three sibling daemon-thread mixins.

### End-to-end map
Each loop is `while self._running: tick(); time.sleep(...)`. Each consumes a Session signal: planner → `playlist`/`replan_requested`, library → `library_need`, producer → `producer_need`. Planner builds full library context every tick, runs the planner ADK agent (separate runner from DJ), parses + validates a `PlaylistV1` JSON, writes to `session.playlist`. Library handles two signal shapes (legacy mood-refill `{mood, count, reason}` vs K5 targeted `{video_id, canonical_artist, canonical_song, ...}`) — dispatch is "is `video_id` present?". Producer wraps `generate_track` (Lyria 3) with a daily cap and KB-enriched prompt. All three sleep 5–15s between ticks. Planner additionally handles `_load_next_on_idle` directly — 121 LOC of Python making track-selection decisions.

### Findings

**F67 — Three sibling loops have identical shape, replicated**
What: Each is `signal-check → busy-flag → throttle → fulfil`. Same try/except/sleep/observability pattern in three files.
Evidence: `planner_loop.py:77–150`, `library_loop.py:47–94`, `producer_loop.py:24–62`.
Fix: One `SignalLoop` base class — subclass overrides `signal_field`, `busy_field`, `min_interval`, `fulfil(need)`. Removes ~150 LOC and prevents drift between loops.
Blast: Medium. Status: Open / **v10**.

**F68 — `_load_next_on_idle` (121 LOC) is Python making selection decisions**
What: Despite "LLM decides, Python executes" thesis, this method ranks playlist candidates, applies dedup, picks rank-1, calls `load_on_deck`. The DJ agent has a `load_track` tool — duplication of intent.
Evidence: `planner_loop.py:769–890`.
Fix: Either commit to Python (delete `load_track` from DJ tools) or commit to LLM (delete `_load_next_on_idle`, idle_needs_load signal goes straight to DJ). Half-and-half is the worst of both.
Blast: Medium-high. Status: Open / **v10 — architectural decision**.

**F69 — `_run_planner` rebuilds full library + feedback from DB on every replan**
What: `get_library_with_metadata()`, `get_liked_tracks(10)`, `get_disliked_tracks(10)` called fresh every planner tick (every 4 tracks). Library can be 1000s of rows.
Evidence: `planner_loop.py:178–215`.
Fix: Cache library snapshot for N seconds; invalidate on `tracks` table mtime or via DB trigger.
Blast: Low. Status: Open / pre-v10.

**F70 — Library K5/legacy dispatch via key-presence sniff**
What: `if need.get("video_id"): K5 path; elif need.get("mood"): legacy path`. Type-by-key-presence is fragile and silent on typos.
Evidence: `library_loop.py:71–86`.
Fix: Add explicit `kind: "targeted" | "mood_refill"` field to the signal; reject signals without it.
Blast: Trivial. Status: Open / pre-v10.

**F71 — Producer day-reset is not lock-protected**
What: `self._producer_count_today` and `self._producer_day` mutated from the producer thread, read elsewhere (TUI? telemetry?). No lock around the day-flip.
Evidence: `producer_loop.py:43–47, 56`.
Fix: Move both into Session (with critical-flush) or guard with a mutex. Persist `_producer_count_today` so restart doesn't reset the cap (F82).
Blast: Low. Status: Open / pre-v10.

**F72 — `_playlist_contains_played` is a 4th implementation of "did we play this?"**
What: We have `_idle_was_played` (heartbeat), inline path-set check (heartbeat P4), `_is_played` (emergency_play), and now this in planner. Four implementations.
Evidence: `planner_loop.py:677–735`.
Fix: Validates F11. Single `Session.has_played(track) -> bool` is even more urgent than slice-1 thought.
Blast: Trivial after F11 lands. Status: Open / pre-v10.

**F73 — Mixin instance attrs lazy-initialized inside loop methods**
What: `_library_download_busy`, `_library_last_consumed_ts`, `_library_failure_counts` initialized via `if not hasattr(self, ...)` at top of `_library_loop`. Producer does the same with `_producer_count_today`, `_producer_day`.
Evidence: `library_loop.py:60–66`, `producer_loop.py:39–41`.
Fix: Validates F8. When F1 (composition over mixins) lands, each subsystem owns its state.
Blast: N/A — fixed by F1.
Status: Open / **v10**.

**F74 — Both v8 (full-library) and v9 (knowledge-surfaced) planner paths live in parallel**
What: Code branches on `if v9_merged and len(v9_merged) >= 5` to pick which prompt builder + which prompt template to use. Both kept alive.
Evidence: `planner_loop.py:218–260` (and continues).
Fix: Once v9 stabilizes, delete v8 path. Until then, log which path was taken on every tick so we can see rollover progress.
Blast: Low. Status: Open / pre-v10. **Document rollout commit when v8 is dropped.**

**F75 — `_surface_v9_candidates` (113 LOC) is a knowledge↔DB↔playlist bridge with no unit boundary**
What: Pulls knowledge candidates, joins to local library, applies played-filter, merges. All in one method on the planner mixin.
Evidence: `planner_loop.py:564–676`.
Fix: Move to `agent/knowledge/candidate_surface.py` as `surface_candidates(current_meta, played_list, knowledge_client) -> list[dict]`. Pure function, testable in isolation.
Blast: Low. Status: Open / pre-v10.

**F76 — `idle_needs_load` is emitted by both planner-post-replan AND heartbeat-post-transition**
What: `planner_loop.py:155–156` sets it after a replan if idle is stale. `agent/transitions.py:147` sets it after every transition. Multiple emitters of one boolean signal — coalesces fine, but ownership is unclear.
Evidence: `planner_loop.py:144–157`, `transitions.py:147`.
Fix: Single emitter (post-transition only). Planner observes; doesn't emit.
Blast: Trivial. Status: Open / pre-v10.

**F77 — `observability.tick(role)` consistently called at top of every loop**
What: Each loop calls `_obs_tick("planner")` / `"library"` / `"producer"`. Centralised health/heartbeat tracking.
Evidence: passim.
Fix: None — this is good. **Keep.**
Status: Resolved-positive.

**F78 — Planner loop sleeps fixed 15s with no error backoff**
What: After a parser failure or LLM timeout, immediately sleeps 15s and tries again. With Flash dropping ~46% on niche prompts, this can mean 50% of planner ticks are wasted retries with no backoff.
Evidence: `planner_loop.py:149`.
Fix: Exponential backoff after consecutive failures (capped). Reset on success.
Blast: Trivial. Status: Open / pre-v10.

**F79 — Defensive `getattr(self.session, "field", None)` for declared Session fields**
What: All three loops use `getattr` even for fields explicitly declared in `_FIELD_DEFAULTS`. Session sets defaults at init; the field is always present. Defensive read is leftover from pre-Session code.
Evidence: `planner_loop.py:114, 117, 122` etc; `library_loop.py:71`; `producer_loop.py:48`.
Fix: Direct attribute access (`self.session.replan_requested`). Trip on AttributeError if field really missing — that's a real bug, not something to swallow.
Blast: Trivial. Status: Open / pre-v10.

**F80 — `_library_failure_counts` accumulates forever**
What: `{video_id: int}` instance dict. Never pruned. After a long-running session, every failed video_id stays in memory.
Evidence: `library_loop.py:65, then `_library_handle_targeted` which writes to it (not shown but implied).
Fix: TTL-based prune, or move to SQLite with a `last_failure_at` column.
Blast: Trivial. Status: Open / pre-v10.

**F81 — Producer prompt built via f-string concatenation**
What: 11 lines of f-string with embedded `{kb_context}` and conditionals like `{vibe or '(use your judgement)'}`. Hard to template, hard to test.
Evidence: `producer_loop.py:89–100`.
Fix: Use `prompts.py` (already exists) — add `build_producer_brief(...)`. Same shape as the planner/DJ prompts.
Blast: Trivial. Status: Open / pre-v10.

**F82 — `_producer_count_today` is process-local; restart resets the daily cap**
What: Producer's daily-cap counter lives only in the instance. Process restart at 23:50 → fresh count, so the cap can be exceeded inside one calendar day.
Evidence: `producer_loop.py:36–47`.
Fix: Persist count in Session (`producer_count_today: int`, `producer_count_day: str`). Restart picks up where it left off.
Blast: Trivial. Status: Open / pre-v10.

### What we'd KEEP

- **Three independent loops keyed off Session signals** — the right shape; only the duplication (F67) needs collapsing.
- **`observability.tick(role)`** — clean centralised health surface (F77).
- **`PlaylistV1` JSON schema with validation + last-good fallback** — planner's structured output is one of the cleanest contracts in the codebase.
- **Daily cap + min_cycle throttle on producer/library** — right primitives, just need to be persisted (F82) and unified (F67).
- **K5 targeted-download flow** — added precision is a feature, not bloat. Keep, just disambiguate via explicit `kind` (F70).

---

## Deep-dive #6 — Remaining mixins (2026-05-04)

Files: `commands.py` (224), `evolution.py` (249), `sets.py` (131), `being_heartbeat.py` (264), `ws_server.py` (379) = 1,247 LOC across 5 mixins.

### End-to-end map
**Commands** is the operator surface — TUI/MCP write `command.json`, this mixin polls each main-loop tick, dispatches `talk`/`skip`/`stop`/`change_mood`/`feedback`/`change_sources` via if/elif. **Evolution** is the self-improvement surface — collects perf data, asks Being to reflect, optionally triggers `evolve(...)` from `tools/evolve.py`. **Sets** owns set lifecycle (start/end/duration-check/auto-rotate) plus recording + broadcast control. **BeingHeartbeat** runs the consciousness LoopAgent (separate ADK runner — `being_runner`), loading SOUL/GOALS/HEARTBEAT.md as the prompt. **WSServerMixin** runs a websockets server on :7779 in its own asyncio loop+thread, plus a Mixxx proxy loop in another thread.

### Findings

**F83 — `_pick_up_directives` is documented dead code, called every main-loop tick**
What: Method body is a `return` plus a docstring saying "retained as a no-op for backward compat with the main loop." Main loop calls it at line 575.
Evidence: `commands.py:23–34`; `main.py:575`.
Fix: Delete the method and the call.
Blast: Trivial. Status: Open / **pre-v10, fix in next PR**.

**F84 — `change_sources` only rebuilds 3 of 5 agents — library + producer regress**
What: After source flip, command does `being_agent, dj_agent, planner_agent = create_agents(self.config)` (3-tuple unpack). But `agents.py` now returns 5 agents (boot path uses `being, dj, planner, library, producer`). After `change_sources`, library+producer agents are stale.
Evidence: `commands.py:142–151`. Compare `main.py:436`.
Fix: Update unpack to 5 elements; rebuild all 5 runners + sessions.
Blast: Trivial. Status: Open / **pre-v10, latent bug**.

**F85 — `change_sources` uses `EventsCompactionConfig` that main.py explicitly forbids**
What: Boot path in `main.py:444` says "No events_compaction: ADK compaction can drop tool results while assistant messages still reference tool_call_ids → 'Missing tool results' API errors." Then `change_sources` re-creates the same Apps WITH compaction enabled, contradicting the documented invariant.
Evidence: `commands.py:148–151`. Compare `main.py:444–449`.
Fix: Remove `events_compaction_config` here. Pair with F84 fix.
Blast: Trivial. Status: Open / **pre-v10, contradicts a documented "don't"**.

**F86 — `_evolution_reflect` calls `evolve` with `max_budget_usd` arg the function doesn't accept**
What: `evolve(goal, scope="agent/", max_budget_usd=...)`. But `tools/evolve.py:34` signature is `evolve(goal, scope="agent/", run_tests=True)`. No max_budget arg. Call would TypeError.
Evidence: `evolution.py:51–55`. Compare `tools/evolve.py:34`.
Fix: Either drop the arg or add it to the tool. The auto-evolve code path may have never actually fired in production — verify.
Blast: Trivial (latent bug). Status: Open / **pre-v10**.

**F87 — Evolution counts transitions by string-grepping `thinking.log`**
What: `data["transition_quality"]["agent"] = content.count("[CALL:dj_treta] schedule_transition")`. Format change in thinking-log emission silently breaks the metric.
Evidence: `evolution.py:80–84`.
Fix: Move counts to Session counters or DB. Same metric pattern as F17 (heartbeat error counter).
Blast: Low. Status: Open / pre-v10.

**F88 — `sets._start_set` calls `litellm.completion` directly, bypassing ADK**
What: To name the set, sets.py imports `litellm.completion` and calls the model directly (no ADK runner, no session, no billing tracking, no _process_event). A fourth LLM call path (alongside DJ runner / Planner runner / Being runner / Producer runner / Library runner).
Evidence: `sets.py:23–34`.
Fix: One-shot LLM helper using the same LiteLlm config + adding to billing. Or — even simpler — pre-generate set name via planner agent and pass through.
Blast: Trivial. Status: Open / pre-v10.

**F89 — `_check_set_duration` rotates sets silently with no listener notification**
What: Hits target_duration → `_end_set()` → `_start_set()`. The relay/WS may eventually catch up, but there's no explicit `set_rotated` event broadcast.
Evidence: `sets.py:71–77`.
Fix: After auto-rotate, emit `_ws_broadcast("set_rotated", {...})` and write to relay.
Blast: Trivial. Status: Open / pre-v10.

**F90 — `_load_heartbeat_prompt` reads SOUL/GOALS/HEARTBEAT every tick**
What: Three file reads per consciousness heartbeat. No cache, no mtime check.
Evidence: `being_heartbeat.py:21–63`.
Fix: Cache + mtime invalidate. Or load once at startup and rebuild on `write_file` to those paths.
Blast: Trivial. Status: Open / pre-v10.

**F91 — Consciousness prompt is a 50+ line Python string literal**
What: The whole "You are Treta's inner consciousness..." rules block is hardcoded in Python source, mixed with code that loads SOUL/GOALS/HEARTBEAT. The Being can't `evolve` her own consciousness prompt without editing Python.
Evidence: `being_heartbeat.py:33–62`.
Fix: Move to `templates/being_heartbeat_base.md`. Treat like SOUL.md — Being can edit it via write_file.
Blast: Trivial. Status: Open / pre-v10. **High symbolic value: the prompt becomes editable identity.**

**F92 — WSServerMixin runs its own asyncio loop+thread (3rd loop in the process)**
What: Process now has: Being's `self._loop` (ADK calls), `spawn._spawn_loop` (subagents), `WSServerMixin`'s loop (server). Three asyncio runtimes in one Python process.
Evidence: `ws_server.py:67–84`. F52 already noted spawn's loop.
Fix: Merge spawn + WS into Being's loop; or — simpler — keep WS separate (it has different lifetime semantics) and merge spawn into WS's loop. Either way kill one of the three.
Blast: Medium. Status: Open / **v10**.

**F93 — Bare `/` WS endpoint kept "for back-compat" — no migration plan**
What: Three endpoints: `/ws/state`, `/ws/command`, and `/` (legacy combined). Comment says it's for old TUI builds and the web listener.
Evidence: `ws_server.py:14–16`.
Fix: Audit who still uses `/`. If only the public listener page, decide: migrate it to `/ws/state`, or rename `/` to `/ws/listen` so the role is explicit.
Blast: Low. Status: Open / pre-v10.

**F94 — Two ws client sets with semantics that drift in comments**
What: `_ws_clients` and `_ws_command_clients`. Comment: "kept separate for clarity but currently also receives push events so a single /ws/command socket can both write commands and observe immediate state echoes." Either they're separate (and one only writes) or they're not — current state is "both, sometimes."
Evidence: `ws_server.py:48–55`.
Fix: Pick one model. Default: `_ws_state_clients` for state subscribers, `_ws_command_clients` for command issuers (no push). Or unify.
Blast: Low. Status: Open / pre-v10.

**F95 — Mixxx proxy loop is yet another daemon thread**
What: `_start_mixxx_proxy_loop` spawned from inside `_start_ws_server`. So WS bring-up adds two threads: the asyncio thread + the Mixxx proxy thread.
Evidence: `ws_server.py:60–62, 322–354`.
Fix: Run the proxy as a coroutine on the WS asyncio loop; one thread total.
Blast: Trivial. Status: Open / pre-v10.

**F96 — `_chat_history = self._chat_history[-10:]` replaces the entire ObservedList on every truncate**
What: `commands.py:188–189`. Each truncate creates a fresh list, runs through Session.__setattr__, deep_equals the entire list, wraps in ObservedList, fires callbacks, flushes.
Evidence: `commands.py:188–189`.
Fix: `del self._chat_history[:-10]` mutates in place, fires single dirty.
Blast: Trivial. Status: Open / pre-v10.

### What we'd KEEP

- **Command-as-file (`command.json`) → polled-and-deleted** — simple, robust, easy to debug. Single in, single out. Keep.
- **`feedback` command writes to DB immediately** — right level of durability for listener intent.
- **Set lifecycle (start/end/check/auto-rotate)** — the abstraction is clean, only minor polish needed (F89).
- **Consciousness loop reads SOUL+GOALS+HEARTBEAT** — the right composition; just cache it (F90) and template it (F91).
- **WS server with three roles (state push / command / listener)** — right shape for both local TUI and remote dj.treta.life relay. Keep, just sharpen role boundaries (F94).

---

## Deep-dive #7 — Knowledge layer (2026-05-04)

Files: `agent/knowledge/__init__.py` (53), `client.py` (308), `models.py` (132), `queries.py` (614), `merge.py` (310). Plus `reference/discogs_genres.json` (one static file).

### End-to-end map
`KnowledgeClient` is a thread-safe singleton that lazy-loads two backends: a polars `LazyFrame` over `~/Music/DJTreta/knowledge/dj_treta_library.parquet` (3.5M rows of metadata) and a LanceDB table `tracks.lance` (384-dim Matryoshka text-embedding-005 vectors, joined to parquet via `mbid`). `queries.discover_candidates` / `similar_to` / `similar_to_text` / `genre_context` / `gap_analysis` are typed entry points the planner calls. `merge.py` joins knowledge candidates against the local SQLite `tracks` table (DJ's downloaded library) and produces `MergedCandidate` records (`{canonical, knowledge_track, local_track, downloaded: bool}`). Everything is gated by `config.knowledge.enabled` — when off, queries return `[]` + `record_degraded(...)`.

### Findings

**F97 — `_normalize_schema` adapts v3↔v6 column names inline at every load**
What: Adapter renames `bpm→tempo`, `genre→dvi_styles` and synthesizes 7 nullable cols if missing. Runs on `pl.scan_parquet` lazyframe (cheap), but it means two schema versions still coexist in the codebase and queries.py was written for v3 names while the dataset ships v6.
Evidence: `client.py:34–73`, `queries.py:74–103` (uses `dvi_styles`, `dvi_labels`, `tempo`).
Fix: Pick one schema (v6 is current), update queries.py to match, drop the adapter. v3 dataset is gone per memory log.
Blast: Low. Status: Open / pre-v10.

**F98 — Vector index build is synchronous, blocks first query for ~5 min**
What: `_try_build_vectors_from_parquet` says "one-time, ~5 min" and runs inside `_try_load_vectors`, which is called from `ensure_loaded`, which the planner calls on first knowledge query. If the daemon starts with `knowledge.enabled = true` and no LanceDB yet, the planner blocks for 5 min on its first tick.
Evidence: `client.py:228–280`. Build path triggered by `if "vector" not in schema.names()` returning False (i.e. column present).
Fix: Build in a background thread on `ensure_loaded`. First queries return empty + degraded health until built. Or: ship the LanceDB index as a separate artifact / build at install time.
Blast: Low. Status: Open / pre-v10. **Boot-latency footgun**.

**F99 — `KnowledgeClient` singleton: `reset()` exists but `data_dir` change isn't switchable**
What: The singleton caches `_data_dir` from first `ensure_loaded` call. Subsequent calls with different `data_dir` are ignored (because `_lf is not None` short-circuits at line 134). `reset()` exists for tests but production has no clean switch.
Evidence: `client.py:122–135, 91–101`.
Fix: If `data_dir` changes, reset and re-load. Or: data_dir is a process-level config, document as "set once per process."
Blast: Trivial. Status: Open / pre-v10. Probably a non-issue in practice.

**F100 — `_enabled()` re-loads config per call**
What: Same pattern as F50, F42, F44. `queries._enabled` reloads config on every `_ensure` invocation.
Evidence: `queries.py:49–57`.
Fix: Module-level cached resolver; pair with the broader config-cache work.
Blast: Trivial. Status: Open / pre-v10.

**F101 — `_EMBED_MODEL` is module-level lazy singleton, never refreshed on credential rotation**
What: Vertex AI embedding model client cached forever once instantiated. Token rotation, region change, credentials reload — none trigger refresh.
Evidence: `queries.py:41`, `_get_embed_model` at 380.
Fix: TTL refresh, or refresh on auth-error caught in `similar_to_text`.
Blast: Low. Status: Open / pre-v10.

**F102 — `KnowledgeHealth` is a single-snapshot object — no degradation history**
What: `record_query` / `record_degraded` overwrite the whole `health` field. Past degradations are invisible to TUI / observability.
Evidence: `client.py:294–308`. `KnowledgeHealth` dataclass at `models.py:122`.
Fix: Add ring-buffer of last N events (similar to `_thinking_history` in WS). Optional: emit each degradation as a WS event.
Blast: Low. Status: Open / pre-v10.

**F103 — `KnowledgeHealth.offline("disabled")` constructed with positional arg, no test**
What: The `offline` classmethod (assumed — not read) is called with single arg, fields filled by class. If signature ever shifts (5 fields per `models.py:122`), every call site would break silently.
Evidence: `client.py:89, 131`. `models.py:122` (need to verify but the call shape suggests a classmethod).
Fix: Pin model contract with a unit test. Standard for typed dataclass APIs.
Blast: Trivial. Status: Open / pre-v10.

**F104 — `merge.find_local_by_mbid` / `find_local_matches` open their own DB connections, bypassing `db.get_db`**
What: Both open `sqlite3.connect(...)` directly with whatever path the module computes — separate from `db._resolve_db_path()`. Drift risk if the resolver logic changes.
Evidence: `merge.py:174–290`.
Fix: Route through `db.get_db()` (which already does WAL + row factory). Validates F41.
Blast: Trivial. Status: Open / pre-v10.

**F105 — `queries.py` is 614 LOC, mixes embedding-model lifecycle + filter builders + 3 query types**
What: One module owns: enabled check, ensure-loaded, row-to-track mapping, mood/exclude filter builders, embedding-model lazy init, and 5 distinct query entrypoints.
Evidence: `queries.py` whole file — 5 imports, 14 functions.
Fix: Split into `queries/_filters.py`, `queries/_embed.py`, `queries/discover.py`, `queries/similar.py`, `queries/genre.py`. Or — pragmatic — let it be; the module is internally well-organized despite size.
Blast: Low. Status: Open / pre-v10. Lower priority than other splits.

### What we'd KEEP

- **Two-backend split (polars metadata + LanceDB vectors)** — exactly the right shape: cheap metadata filters + expensive ANN only when needed.
- **Graceful degradation via `KnowledgeHealth`** — `record_degraded(...)` instead of silent empty returns is the right contract.
- **Typed return models** (`CanonicalRef`, `KnowledgeTrack`, `MergedCandidate`, `GapReport`) — best-typed surface in the codebase. Templates for the rest.
- **Lazy `ensure_loaded` so daemon starts fast when knowledge is disabled** — operator-friendly.
- **`merge.merge_candidates_against_local`** — the join-against-local-SQLite is the right place for "is this downloaded?" logic. (Pairs with F11 unification.)

---

## Deep-dive #8 — Transport + UI (2026-05-04)

Files: `tui.py` (2,433), `cli.py` (748), `agent/tui_state_source.py` (804), `agent/relay.py` (631) = 4,616 LOC.

### End-to-end map
**`tui.py`** is the Textual-based DJ console — 6 widget classes (DeckWidget, PlaylistWidget, AgentActivityWidget, MixerWidget, BrainWidget, plus DJTretaApp container at 1,320 LOC). State arrives via `StateSource` abstraction (local file polling OR remote WS). **`agent/tui_state_source.py`** = abstract `StateSource` + `WebSocketRemoteStateSource` for `--remote`. **`cli.py`** is a Rich-based CLI with one-shot commands + interactive mode + daemon control (`start_brain`, `stop_brain`, `kill_all`, `reset`). **`agent/relay.py`** is the public-listener relay — pushes DJ state to `wss://dj.treta.life/ws/state`, runs in a daemon thread with its own `asyncio.run()` (4th asyncio loop in the process). Has its own `PerceptionEngine` and Camelot mapping inline.

### Findings

**F106 — `tui.py` is 2,433 LOC in one file**
What: 6 widget classes + `DJTretaApp` (1,320-LOC class) + 14 top-level helpers.
Evidence: `tui.py:1–2433`, `class DJTretaApp` at line 1059 runs to ~2380.
Fix: Split per widget into `tui/widgets/{deck,playlist,agent_activity,mixer,brain}.py` and `tui/app.py`. Each widget stays under 400 LOC.
Blast: Medium. Status: Open / pre-v10. **Highest readability win in the UI surface.**

**F107 — `cli.py` is a 4th Mixxx client (after `agent/tools/helpers`, `tui.py`, MCP)**
What: Local `mixxx_get` / `mixxx_post` re-implemented at `cli.py:44–55`. Identical shape to `tools/helpers._mixxx_*` but in a different module. Drift risk and divergent error handling.
Evidence: `cli.py:44–55`. Compare `tools/helpers.py:60–80`, `tui.py:127`.
Fix: One shared `httpx_mixxx` module. CLI/TUI/tools all import from it.
Blast: Low. Status: Open / pre-v10.

**F108 — Camelot key mapping defined four times in the codebase**
What: `relay.py:18–32` has `MIXXX_KEY_TO_MUSICAL` + `KEY_TO_CAMELOT`. The DB schema (`db.py`) has a `key_camelot` column populated by the analyzer. Heartbeat reads `key_camelot` from DB. TUI also has its own format helper. Four places that know key↔camelot.
Evidence: `relay.py:18–32`; `db.py` schema; `heartbeat.py:336`; `tui.py:181` (`fmt_key`).
Fix: One `agent/music_theory.py` module owning Camelot conversions. Everywhere else imports from there.
Blast: Trivial. Status: Open / pre-v10.

**F109 — `relay.py` PerceptionEngine overlaps `tools/perception.py`**
What: Relay defines its own `PerceptionEngine` class. The tool module `tools/perception.py` has `hear_music` / `analyze_track` / `preview_track`. Relationship unclear without deeper read; likely partially-overlapping.
Evidence: `relay.py:64+` (class start); `tools/perception.py` whole file.
Fix: Audit. If relay's perception is for the public state derivation only (energy estimate from current Mixxx state), name it `LiveStatePerception` and put it next to relay's other helpers. If it duplicates analysis, dedup against tools.
Blast: Low. Status: Open / pre-v10.

**F110 — `tui.py:mixxx_get(path, *, allow_blocking=False)` bypasses StateSource for some calls**
What: TUI has a direct Mixxx HTTP path (default non-blocking) that lives outside the `StateSource` abstraction. The "non-blocking" name signals past pain (F-mention in auto-memory: "TUI render thread MUST NOT block on WS"). But this means render-time Mixxx access has *two* paths — StateSource + direct.
Evidence: `tui.py:127–152`.
Fix: Make StateSource's `read_mixxx_*` the single render-time path. Direct mixxx HTTP only on user-initiated control commands (where blocking is OK).
Blast: Medium. Status: Open / pre-v10.

**F111 — No `LocalStateSource` class — local TUI reads files directly**
What: `StateSource` abstract base + `WebSocketRemoteStateSource`. Local mode uses module-level `read_state()` reading `state.json` directly (`tui.py:164`). The abstraction is half-done.
Evidence: `tui_state_source.py:63–135`; `tui.py:164–168`.
Fix: Implement `LocalFileStateSource` that mirrors the WS shape. TUI app holds a `StateSource` instance regardless of mode.
Blast: Low. Status: Open / pre-v10. Closes the abstraction.

**F112 — `DJTretaApp` is a 1,320-LOC class**
What: Inside the 2,433-LOC `tui.py`, the `DJTretaApp` class itself runs ~1,320 lines.
Evidence: `tui.py:1059–2380`.
Fix: Pulls out naturally with F106 split. Each tab pane becomes its own composer; the App class becomes a thin shell.
Blast: Medium (subset of F106). Status: Open / pre-v10.

**F113 — `_synthesize_mixxx_from_state` — schema-coupled fallback when WS is down**
What: TUI synthesizes a Mixxx-status-shaped dict from the state.json snapshot when the WS connection drops, so widgets keep showing something. Tightly couples TUI to state.json's exact field layout.
Evidence: `tui.py:310–367`.
Fix: Document the contract OR make state.json a typed snapshot model imported from a shared module (`agent/state_snapshot.py`). Both TUI and writer import that model. Reinforces F36 (schema versioning).
Blast: Low. Status: Open / pre-v10.

**F114 — Three time-format helpers in TUI**
What: `fmt_time`, `fmt_time_precise`, plus `format_time` in cli.py. Three formatters for "render seconds as M:SS or M:SS.t".
Evidence: `tui.py:170, 176`; `cli.py:75`.
Fix: One shared `agent/format_time.py`. Cosmetic but worth noting.
Blast: Trivial. Status: Open / pre-v10.

**F115 — `cli.py` hardcodes `MIXXX_URL = "http://localhost:7778"`**
What: Doesn't read `config.mixxx.url`. If the user changes the port, CLI breaks silently.
Evidence: `cli.py:31`.
Fix: Load config in CLI. Same fix as the other config-skipping spots.
Blast: Trivial. Status: Open / pre-v10.

**F116 — `relay.py` runs `asyncio.run(...)` inside a daemon thread (4th asyncio loop)**
What: Process now hosts: Being's `_loop` (ADK), `_spawn_loop`, WS server loop, relay loop. Each has its own thread + event loop.
Evidence: `main.py:608–613` (`_relay_loop` thread).
Fix: Merge relay into the WS server's loop (they're closely related — both push state out). Reduces 4 loops to 3 (or 2 after F92).
Blast: Medium. Status: Open / **v10**.

**F117 — Relay imports `httpx` for `wss://` outbound but uses `websockets` API style**
What: Quick scan suggests both libs imported. If both are used, two HTTP/WS dependencies for the same surface.
Evidence: `relay.py:11`. (Need full read to confirm — flag for follow-up.)
Fix: Standardize on `websockets` for WS, `httpx` for REST.
Blast: Trivial. Status: **Open / verify before fix**.

### What we'd KEEP

- **`StateSource` abstraction** — right shape for local-vs-remote TUI; just complete it (F111).
- **Textual TUI as the ops console** — fast, keyboard-first, fits the "Being on a screen" feel. Keep, just split.
- **Rich-based CLI for one-shots + daemon control** — `djtreta start/stop/status/logs` is the right operator surface.
- **Relay as merged-into-Being** — comment says "ported from dj-treta-live, no separate process needed" — that's correct, fewer moving parts.
- **`--remote` mode with WS proxy of Mixxx** — operator can drive a remote daemon from a laptop. Good architecture.

---

## Deep-dive #9 — MCP server (2026-05-04)

Files: `mcp_server/__init__.py` (8), `auth.py` (51), `server.py` (339), `tools.py` (966), `session_writer.py` (93) = 1,457 LOC. Standalone process — FastMCP SSE on `127.0.0.1:8765`, fronted by nginx at `mcp.dj.treta.life` with bearer token. **35+ tools registered.**

### End-to-end map
Server boots `FastMCP("dj-treta", ...)` with DNS-rebinding protection + bearer middleware, registers ~35 tools each via `mcp.add_tool(...)`. Tools span: read-only (status / playlist / session_state / search_library / similar_to / get_thinking / read_chat), write-via-command-file (talk / set_mood / skip / request_track / feedback / set_sources), write-direct-to-Mixxx (play / pause / volume / crossfader / EQ / filter / load_track), recording (record_set / stop_recording / announce / read_chat), and co-being deck ownership (take_deck / release_deck / load_on_deck). `session_writer.py` defines the safe write channel (command file with `wait_for_command_result`). Auth = bearer token from `DJTRETA_MCP_TOKEN` env.

### Findings

**F118 — `SESSION_JSON = Path("/mnt/data/dj-treta/.beings/session.json")` is a HARDCODED Linux path**
What: Doesn't exist on Mac dev. The MCP server can't read session state on a Mac; on the VM it depends on the data-disk mount being there. Compare to `runtime_path()` used everywhere else in the codebase. Compare to actual agent path: `Path(__file__).parent.parent / ".beings" / "session.json"`.
Evidence: `mcp_server/session_writer.py:32`.
Fix: Resolve via env or relative-to-repo, just like `db._resolve_db_path`. Probably caused the MCP server's `dj_session_state` to silently fail on Mac.
Blast: Trivial. Status: Open / **probable bug, fix in next PR**.

**F119 — `tools.py:MIXXX_URL = "http://localhost:7778"` hardcoded**
What: Same as F115. Doesn't read `config.mixxx.url`. Direct-to-Mixxx tools break if user changes port.
Evidence: `mcp_server/tools.py:60`.
Fix: Centralize in a config-loader or accept env override.
Blast: Trivial. Status: Open / pre-v10.

**F120 — `tools.py` is 966 LOC, 35+ tools in one file**
What: Same shape as F65 (tools/__init__.py overcrowding). Adding a tool requires editing this file plus `server.py` add_tool block.
Evidence: `mcp_server/tools.py` whole file; `server.py:67–339+` (assumed continuation).
Fix: Per-category module (`mcp_server/tools/{read,write,deck,record,co_being}.py`). Server iterates a registry instead of explicit `add_tool`.
Blast: Medium. Status: Open / pre-v10.

**F121 — `dj_load_track` (MCP) writes directly to Mixxx, can race with daemon**
What: MCP tool POSTs `/api/load` to Mixxx without going through the deck-ownership signal. If daemon hasn't seen the `dj_take_deck` first, it'll auto-load on top of the MCP-loaded track on next P3.5 tick.
Evidence: `tools.py:402–430` (signature shows `dj_load_track(deck_num, path)` — no ownership check). Compare `dj_load_on_deck` at line 586 which presumably does check.
Fix: Either gate `dj_load_track` behind `dj_take_deck`, or make it imply take_deck atomically. Audit external callers (only `dj_load_on_deck` should be the public surface for co-being takeovers).
Blast: Low. Status: Open / pre-v10. Concurrency-relevant.

**F122 — `DECK_OWNERSHIP_FILE = Path("/tmp/dj-treta-deck-ownership.json")` bypasses `runtime_path`**
What: Heartbeat reads via `runtime_path("deck-ownership.json")`. MCP writes the literal `/tmp/dj-treta-deck-ownership.json`. They agree on Mac/Linux because runtime_path defaults to `/tmp` — but if `DJTRETA_RUNTIME_DIR` is set, the daemon reads one file and MCP writes another, silently breaking ownership.
Evidence: `tools.py:61`. Compare `heartbeat.py:110` (uses `runtime_path`).
Fix: MCP also imports `from agent.runtime_paths import runtime_path`.
Blast: Trivial. Status: Open / pre-v10. **Latent bug under any non-default runtime dir.**

**F123 — MCP reads session.json + state.json directly (vs going through Session API)**
What: `dj_status` reads `state.json`; `dj_session_state` reads `session.json` (broken on Mac per F118). Both bypass the in-process Session — but they have to, because MCP is a *separate process*. The cost: MCP sees stale data (up to 2s for state.json, up to 500ms for session.json).
Evidence: `session_writer.py:18–22` documents this explicitly as the safe channel.
Fix: For v10 — add a read-side IPC (Unix socket / shared memory). For now, the staleness is documented and acceptable.
Blast: N/A (acceptable). Status: Resolved-as-documented. Reinforces F2 (process split) — MCP IS already separate.

**F124 — `dj_record_set` is a host-side ffmpeg recording, separate from agent's `_start_recording`**
What: Two recording implementations: (a) `sets.py:_start_recording` (in-daemon, called on set start); (b) `mcp_server/tools.py:dj_record_set` (host-side ffmpeg captures Icecast). Different file paths, different lifecycles, different metadata.
Evidence: `tools.py:765–831`. Compare `sets.py:_start_recording`.
Fix: Pick one. If MCP's host-side capture is for ad-hoc operator recordings (not auto-per-set), name it differently (`dj_capture_stream`) so the role is clear.
Blast: Low. Status: Open / pre-v10.

**F125 — `_resolve_mbid_to_path` is a 4th path-resolver**
What: Resolvers we already have: `playback_applier.resolve_track_path`, `db.find_track_by_path` / `find_track_by_canonical` / `find_track_by_source_url`. Now MCP adds `_resolve_mbid_to_path`. Each takes a different identifier shape and walks the local filesystem in slightly different ways.
Evidence: `tools.py:563–585`.
Fix: One `agent/track_resolver.py` accepts a `TrackKey` (path | mbid | canonical | source_url) and returns absolute path or None. Pairs with F12.
Blast: Low. Status: Open / pre-v10.

**F126 — `tools.py:_mixxx_post` is a 4th Mixxx HTTP client (after agent/, tui, cli)**
What: F107 noted three; this is the fourth.
Evidence: `tools.py:302–319`.
Fix: Single shared `mixxx_http.py` module imported by everyone.
Blast: Trivial. Status: Open / pre-v10.

**F127 — Bearer auth refuses to start without token (good); allowed-hosts list expands DNS-rebinding scope**
What: Auth is reasonably done — `DJTRETA_MCP_TOKEN` env var is required. But `allowed_hosts` list is comma-delimited env-driven and includes wildcarded ports (`127.0.0.1:*`). Wildcard ports + DNS rebinding means risk surface is wider than necessary. Documented (`server.py:34–46`) but worth pinning.
Evidence: `server.py:38–53`.
Fix: Audit. The wildcard ports are because clients use random ports — that's correct. The mcp.dj.treta.life entry is the public hook. Keep, document the threat model.
Blast: N/A. Status: **Resolved-as-noted. Document threat model in `docs/`.**

### What we'd KEEP

- **Bearer-token auth + DNS-rebinding protection** — security baseline is right; explicit token requirement (F127) is the right default-deny.
- **`session_writer.py`'s explicit "we never write session.json directly" doctrine** — exactly the right contract for a separate-process writer. (F123 is fine because of this.)
- **Tool categorization (read / write-via-command / write-direct-Mixxx / co-being)** — the surface is well-organized even if the file is too big.
- **`dj_take_deck` / `dj_release_deck` co-being primitive** — gives Himani / Serra a clean way to co-DJ. Architectural primitive worth keeping.
- **Standalone process model for MCP** — already validates F2 (split processes). MCP is the proof-of-concept that process-level separation works.

---

## Deep-dive #10 — Install + ops + tests + scripts + docs (2026-05-04)

Files: `install.sh` (883), `bin/djtreta` (4), `bin/djtreta-daemon` (72), `bin/install-vm.sh` (112), `bin/deploy-vm.sh` (82), `bin/cleanup-vm.sh` (82), `bin/systemd/*.template` (7 unit templates + ezstream + logrotate + pulse), `scripts/*.py` (4 files, 949 LOC), `tests/` (18 unit + 15 eval), `docs/*.md` (10 files, 3,193 lines).

### End-to-end map
**`install.sh`** is a 883-LOC bash script handling 3 platforms (mac arm64, mac x64, linux x64) plus operator-mode (systemd units + Icecast + Xvfb + relay). Layout: `~/.local/share/djclaw/{venv,mixxx/<ver>,db,runtime}` + `~/.config/djclaw/{config.yaml,secrets.env,litellm.yaml,token}` + `~/.local/bin/djclaw`. Operator-mode adds `/opt/djclaw` + `/var/lib/djclaw` + `/etc/djclaw`. **`bin/djtreta`** is a 4-line wrapper exec'ing `~/beings/dj-treta/.venv/bin/python3 cli.py`. **`bin/djtreta-daemon`** is the dev launcher (start/stop/restart/status/logs). **`bin/install-vm.sh`/`deploy-vm.sh`/`cleanup-vm.sh`** are pre-install.sh-era VM scripts. **Tests**: 18 unit + 15 eval, but no visible CI. **Docs**: mix of current (V10_PLAN, RELAY_SPEC, LITELLM_*, MIXXX_BUILD) and historical (ADK_MIGRATION, ROCK_SOLID_IMPLEMENTATION, TRANSITION_EVAL_PLAN).

### Findings

**F128 — `install.sh` is 883 LOC monolithic bash**
What: One file does platform detect, venv create, Mixxx download/extract, deb extraction, repo fetch, config seed, systemd unit install, ezstream, Xvfb, relay token write. Hard to read, hard to test.
Evidence: `install.sh` whole file.
Fix: Split into `install/lib/{platform,venv,mixxx,operator,systemd}.sh`. Top-level `install.sh` orchestrates with explicit phase comments. Each lib piece is testable in isolation.
Blast: Medium. Status: Open / pre-v10. Closely tied to the "single-command install" UX, so split carefully.

**F129 — `bin/djtreta-daemon` hardcodes `/tmp/dj-treta.pid` and `/tmp/dj-treta-daemon.log`**
What: Bypasses `runtime_paths.runtime_dir()` indirection — same anti-pattern as F122. If user sets `DJTRETA_RUNTIME_DIR=...` and starts the daemon via this wrapper, the wrapper's PID file is in `/tmp` while the daemon writes its own PID to the runtime dir.
Evidence: `bin/djtreta-daemon:7–8`.
Fix: Source a small bash helper that resolves runtime dir consistently with Python.
Blast: Trivial. Status: Open / pre-v10.

**F130 — `bin/djtreta-daemon` and `bin/djtreta` hardcode `$HOME/beings/dj-treta`**
What: But `install.sh` installs to `~/.local/share/djclaw/`. The dev-mode wrappers and the install.sh-managed wrappers refer to different locations. Either depending on which the user launches, behaviour differs.
Evidence: `bin/djtreta:3`, `bin/djtreta-daemon:5`.
Fix: install.sh writes its own `djclaw` shim that points to the installer-managed venv + repo. Repo `bin/djtreta*` are dev-only and explicitly named `djtreta-dev` to avoid collision.
Blast: Low. Status: Open / pre-v10.

**F131 — 7 systemd unit templates, no operator runbook in docs**
What: Templates: agent / hls / litellm / mcp / mixxx / stream / xvfb. Plus ezstream.xml + logrotate + pulse. Comprehensive coverage, but `docs/` has no `OPERATOR_RUNBOOK.md` or equivalent.
Evidence: `bin/systemd/*.template`. `docs/` listing.
Fix: Add `docs/OPERATOR_RUNBOOK.md` documenting: which units depend on which, how to enable/disable, where logs go, how to rotate the relay token, how to swap Mixxx versions.
Blast: Trivial (writing). Status: Open / pre-v10.

**F132 — 18 unit + 15 eval tests, no visible CI configuration**
What: Test surface is healthy but I see no `.github/workflows/` or `.circleci/` or pre-commit hook. Tests run on the dev's machine when they remember.
Evidence: Repo top-level — none of those configs visible. (Verify with `ls .github/`.)
Fix: Add `.github/workflows/test.yml` (unit on every PR; eval on schedule). Even one workflow turns the existing tests from "exists" to "validated."
Blast: Low. Status: Open / pre-v10. **High ROI for a few hours of work.**

**F133 — `scripts/` has 4 operational scripts, no README**
What: `canonicalize_library`, `ingest_tracks_to_fixture`, `migrate_paths_to_relative`, `seed_library_batch`. Each ~200–340 LOC. Zero docs about when to run, in what order, what they touch.
Evidence: `ls scripts/`.
Fix: `scripts/README.md` with one-paragraph-per-script: purpose, when to run, idempotency, side effects, rollback. Migrations especially need this.
Blast: Trivial. Status: Open / pre-v10.

**F134 — `docs/` mixes current and historical/aspirational without status tags**
What: `ADK_MIGRATION.md` (history of v6→v8 ADK shift), `ROCK_SOLID_IMPLEMENTATION.md`, `TRANSITION_EVAL_PLAN.md` — written at various times. No "STATUS: current/superseded/historical" front-matter.
Evidence: `docs/` ls + sizes.
Fix: Add a single `docs/INDEX.md` listing each doc with status. Or add YAML front-matter `status: current` / `status: history`. Cheapest: rename historical to `docs/history/`.
Blast: Trivial. Status: Open / pre-v10.

**F135 — `REFACTOR_PLAN.md` (root) is the v8 plan, marked "not implementing yet"**
What: Root-level doc says "Started: 2026-04-18, Status: Planning — not implementing yet." Most of v8 has shipped (Phases 1–7 visible in code comments). The doc hasn't been updated.
Evidence: `REFACTOR_PLAN.md:1–10` (read in slice 0).
Fix: Either update REFACTOR_PLAN to mark phases as DONE/SHIPPED, or move it to `docs/history/` and supersede with `V10_PLAN.md`.
Blast: Trivial. Status: Open / pre-v10.

**F136 — `install.sh` supports 3 platforms + operator-mode = 4 paths to keep aligned**
What: macOS arm64 / macOS x64 / Linux x64 / Linux+operator-mode. Each touches different Mixxx download URLs, different deb-extract logic, different systemd path. Quad-test surface.
Evidence: `install.sh` env vars + branching (read top-only).
Fix: Reduce surface. macOS x64 is dying — drop it after a deprecation cycle. Treat operator-mode as Linux-only with explicit error on Mac.
Blast: Low (deletion). Status: Open / pre-v10.

**F137 — `bin/install-vm.sh` + `deploy-vm.sh` + `cleanup-vm.sh` overlap with install.sh operator-mode**
What: install.sh's `--operator` mode supersedes these per the auto-memory ("install.sh operator-mode migration Apr 29"). But the three older scripts are still in `bin/`.
Evidence: `bin/install-vm.sh`, `deploy-vm.sh`, `cleanup-vm.sh`. Auto-memory `project_djtreta_install_migration_apr29.md`.
Fix: Move to `bin/legacy/` or delete. Add a one-line replacement note.
Blast: Trivial. Status: Open / pre-v10.

**F138 — Eval suites exist but no `eval_score.py` aggregation visible in CI**
What: `tests/eval_score.py` + `EVAL_CASES.md` + `EVAL_SPEC.md` suggest an eval harness. No automated runner that collects scores over time.
Evidence: `tests/scores/` dir exists; aggregation strategy not documented.
Fix: Document how to run the eval suite, what `tests/scores/` shape looks like, and where the score history lives. v10 evolution needs this baseline (otherwise we can't say "v10 is better than v9.3").
Blast: Trivial (doc). Status: Open / **v10 prerequisite**.

### What we'd KEEP

- **Single-command install** — `curl … | sh` UX is correct. The complexity is hidden, that's the point.
- **XDG layout** — `~/.local/share/djclaw/` + `~/.config/djclaw/` follows OS conventions.
- **`/opt/djclaw` operator-mode** — full systemd + Icecast + relay is the right shape for production.
- **18 unit + 15 eval tests** — most projects this scrappy don't have this much. Just needs CI (F132) and aggregation (F138).
- **systemd unit templates** — let install.sh customize per machine without hand-editing. Right level of abstraction.

---

# v10 Evolution Plan (synthesis, 2026-05-04)

138 findings across 10 slices. This section turns them into a **plan we'd actually execute** when triggers fire — not all at once, in waves of decreasing reversibility.

## What v10 is, and what it isn't

**v10 IS:**
- Clean plumbing under the same Being thesis. "LLM decides, Python executes" stays.
- Process-level separation that makes the music-never-stops invariant *architectural*, not disciplinary.
- One source of truth per concern. One "did we play this?" predicate. One Mixxx HTTP client. One Camelot mapping.
- A typed message bus replacing 13 IPC files.
- Eval suite + CI = "v10 is measurably better than v9.3" is a claim we can defend.

**v10 is NOT:**
- A different agent framework (still Google ADK + LiteLLM).
- A different model (Gemini 3 Flash + Pro).
- A redesigned UI (Textual + Rich keep working).
- A different protocol with operators (XDG paths, systemd templates, install.sh shape preserved).
- A re-think of the 7-style transition library or the planner/DJ split.

The thesis is right. The plumbing isn't.

## Trigger conditions revisited

Recap from the top of this doc — v10 begins when at least one fires:
- (a) A class of bugs we can't fix without architectural change keeps recurring.
- (b) A new capability we want is blocked by current shape.
- (c) Onboarding a second contributor and the mixin god-class is in the way.

**Status as of 2026-05-04:**
- (a) **Yellow** — replay bugs, signal races, played-track-on-idle bugs keep recurring (F11, F33, F34, F72). Each has a band-aid; they form a class.
- (b) **Green** — no blocked capability today. Drop monitoring (Genesis Drop) is unblocked, co-being primitives work, dj.treta.life is live.
- (c) **Yellow** — we already have F8 (mixin invisible init) blocking honest review of new contributors. Hasn't been tested with a real second contributor yet.

**Verdict: don't start v10 today.** Pay down the *pre-v10* findings first (which are 75% of the list and don't commit to v10). Reassess monthly.

## Wave structure

### Wave 0 — Bug fixes + dead code (next 1–2 PRs, ~1 day total)
Pure correctness wins. Zero architectural commitment. Ship these *before* anyone else touches the code.

**Latent bugs:**
- **F6** — `_CorruptionDetector` defined twice in `adk_runner.py`. Delete one.
- **F45** — `pulse_interval_seconds: float = 5.0  # legacy` in DaemonConfig. Grep and delete.
- **F53** — `library.get_set_history` reads `state.json["tracks_played"]` which is a count, not a list. Switch to `Session.tracks_played`.
- **F84** — `change_sources` only rebuilds 3 of 5 agents. Fix tuple unpack to 5.
- **F85** — `change_sources` uses `EventsCompactionConfig` that `main.py` says NOT to use. Remove.
- **F86** — `_evolution_reflect` calls `evolve(max_budget_usd=...)` but `evolve()` doesn't accept it. Either drop or add.
- **F118** — MCP `SESSION_JSON = "/mnt/data/dj-treta/.beings/session.json"` is broken on Mac. Resolve via env.
- **F122** — MCP `DECK_OWNERSHIP_FILE = "/tmp/..."` bypasses `runtime_path`. Fix.
- **F129** — `bin/djtreta-daemon` hardcodes `/tmp/dj-treta.pid`. Source-share with Python.

**Dead code:**
- **F39** — `STATE_FILE`, `PERSIST_FILE` module-level dead in `session.py` and `main.py`.
- **F83** — `_pick_up_directives` is a documented no-op called every tick.
- **F137** — `bin/install-vm.sh`, `deploy-vm.sh`, `cleanup-vm.sh` superseded by `install.sh --operator`. Move to `bin/legacy/`.

### Wave 1 — Cheap consolidation (next 2–3 weeks, inside current arch)
Mechanical refactors. No architectural commitment. Each is a small PR. Run alongside feature work.

**Single-source-of-truth fixes:**
- **F11 + F72** — One `Session.has_played(track) -> bool`. Delete 4 implementations across heartbeat / planner / emergency / playlist.
- **F12** — Canonicalize track paths to relative-from-music_dir at write time. Drop basename-fallback in `resolve_track_path`. (Migration script needed → F12 is technically v10-shaped; do the path-canonicalization at-write side here, drop the fallback only after migration.)
- **F50, F42, F44, F100** — One config-cache. Stop re-loading config per Mixxx call.
- **F107, F115, F119, F126** — One shared `mixxx_http.py`. Four Mixxx clients → one.
- **F108** — One `agent/music_theory.py` for Camelot. Four mappings → one.
- **F125** — One `agent/track_resolver.py`. Four resolvers → one.
- **F114** — One `format_time`. Three impls → one.
- **F58** — Drop `_dj_get/_dj_post`; keep only `_mixxx_get/_mixxx_post`.

**Pre/post-flight + transitions:**
- **F18, F19** — `_preflight_or_abort` + `_postflight` helpers in transitions engine. 7 duplicates → 2 functions.
- **F20** — Scheduled-transition dispatch passes `**sched_kwargs` through, no longer drops `bpm_after`/`glide_duration`/`duration_bars`.
- **F22** — Bass-restore thread cancellable via Event.
- **F24** — `bpm_after` typed Union instead of string-encoded float.
- **F29** — Magic numbers → named constants block.
- **F30, F62** — Typed result protocol (`ok: bool` dict) for tools. LLM still gets stringified; Python callers branch.

**Heartbeat readability:**
- **F7** — Split `start()` into `_init_state` / `_ensure_external_processes` / `_build_agents` / `_install_callbacks` / `_spawn_loops` / `_run_forever`.
- **F10** — Split `_heartbeat()` into `_p1_silence` / `_p2_auto_transition` / `_p3_scheduled_exec` / `_p35_signals` / `_p4_creative_invoke` returning a verdict struct.
- **F17** — Counter + rate-limited stack-trace log on heartbeat exceptions.

**Persistence polish:**
- **F33 documentation** — Document "replace dicts in lists, don't mutate them in place" as a Session contract.
- **F34** — Snapshot to dict inside lock, write outside. Reduces lock-hold.
- **F35** — Identity-compare before deep_equal on writes.
- **F37, F38** — `unregister_callback`, `clear_session()`. Test-cleanup.
- **F40, F49** — Drop the 10s checkpoint and state.json regeneration when WS is connected.
- **F43** — `PRAGMA user_version` for migrations. Idempotent migrations only run once.
- **F47** — Move flush-thread start to end of `Session.__init__`.

**Tools / loops:**
- **F54, F55** — Single WritePolicy. Atomic write_file (tmp + os.replace).
- **F63** — `read_file(offset, limit)`. No more silent SOUL.md truncation.
- **F69** — Cache library snapshot in planner (5s TTL).
- **F76** — Single emitter for `idle_needs_load` (post-transition only). Planner observes.
- **F78** — Exponential backoff on planner failures.
- **F79** — Drop defensive `getattr(self.session, ...)` for declared fields.
- **F82** — Persist `_producer_count_today` in Session.
- **F89** — `_ws_broadcast("set_rotated", ...)` after auto-rotate.

**WS + UI:**
- **F90, F91** — Cache + template-ize the consciousness prompt. SOUL/GOALS/HEARTBEAT readable as templates.
- **F95** — Mixxx proxy as coroutine on WS loop.
- **F106, F112** — Split `tui.py` per widget into `tui/widgets/`.
- **F111** — Implement `LocalFileStateSource`. Close the abstraction.
- **F114** — One time-formatter.

**Tests + docs + ops:**
- **F132** — `.github/workflows/test.yml`. Unit on every PR, eval scheduled.
- **F133** — `scripts/README.md`.
- **F134, F135** — `docs/INDEX.md` with status tags. Move historical docs to `docs/history/`.
- **F138** — Document eval harness so v10 can claim measurable improvement.
- **F46** — Startup probe `/v1/models` against LiteLLM. Warn on missing model.

**Wave 1 expected effort:** ~3 focused weeks across 30+ small PRs. Realistic with normal interruption: 6–8 weeks. Total touched files: most of `agent/`, but each PR is small.

### Wave 2 — Process split (the v10 milestone, 1–2 weeks focused)

**Trigger to start:** Wave 1 ~80% done OR a recurring incident that Wave 0/1 can't fix.

**The single architectural commitment of v10 is process splitting (F2):**

```
┌─ djclaw-agent (HOT path) ─────┐    ┌─ djclaw-support ─┐
│  ADK Runners (DJ + Planner)   │    │  Library         │
│  Heartbeat                    │←──→│  Producer        │
│  Mixxx control (HTTP)         │    │  Knowledge       │
│  WS server (state push)       │    │  Evolution       │
│  Session (single writer)      │    │  Spawn           │
└───────────────────────────────┘    └──────────────────┘
            ↑                                   ↑
            │                                   │
     ┌──────┴────────────────┬──────────────────┘
     │                       │
┌─ djclaw-mcp ─┐    ┌─ TUI ─┐
│  (already    │    │       │
│   separate)  │    │       │
└──────────────┘    └───────┘
```

**IPC** = Unix domain socket carrying typed message bus (F3). Schema: `{kind, version, payload}` Union types via Pydantic. Replaces 13 filesystem-IPC files (F9).

**Why this milestone:**
- Library download stalls + producer crashes can't take down music (F2 motivation).
- 4 asyncio loops in one process (F52, F92, F116) collapse to 2 (agent + support).
- Test isolation becomes possible — each process has clear boundaries.

**Migration strategy: strangler-fig.**
1. Define IPC protocol + Unix-socket plumbing. Both sides work in same process.
2. Move Library to a subprocess. Keep music live.
3. Move Producer.
4. Move Knowledge ingestion (queries stay in-process — read-only fast path).
5. Decommission filesystem-IPC files one by one.

**At each step**, the music must keep playing. If it doesn't, roll back.

### Wave 3 — Composition over mixins (deferred until trigger c fires)

**F1 + F8 + F73** — only do this when onboarding a second contributor (or when a mixin refactor blocks a real feature). Until then, it's the highest-effort, lowest-immediate-ROI fix in this doc.

The plan:
- `Being` core class with named subsystems (`session`, `planner`, `mixer`, `library`, `producer`, `knowledge`, `transport`).
- Each subsystem is a plain class with explicit `__init__`, owning its own state.
- Mixins → `Subsystem.attach(being)`-style wiring.
- Tests can instantiate one subsystem at a time.

Estimated 2 weeks focused, 4 weeks realistic. Touches every file in `agent/`. Music stays live via strangler-fig: new Being class wraps old mixins, peel off one subsystem at a time.

### Wave 4 — Schema versioning + typed bus (concurrent with Wave 2)

**F36** Session schema version + migration table.
**F61** Directives become an append-only typed queue with explicit clear.
**F70** Library `library_need` typed by explicit `kind` field.

These tighten Wave 2's IPC into a contract. Required *before* Wave 3 can land cleanly.

## Dependency graph

```
Wave 0 (bug fixes + dead code)
  ↓
Wave 1 (consolidation — pure mechanical)
  ↓
  ├─ Wave 2 (process split) — needs Wave 1's mixxx_http / track_resolver / config-cache
  │      ↓
  │    Wave 4 (typed bus) — runs concurrent with Wave 2
  │      ↓
  └→ Wave 3 (composition) — only when trigger c fires
```

## What we'd KEEP across all waves (the "DON'T break this" list)

Drawn from each slice's KEEP section:
1. **Two-agent split (DJ + Planner)** — slow strategic vs fast reactive is the right axis.
2. **Session as in-memory single-source-of-truth** — shape is right, only polish needed.
3. **MCP server mirroring tool surface as separate process** — proof of concept for Wave 2.
4. **Operator-mode install (`/opt/djclaw` + systemd templates)** — production shape.
5. **Evolution as a first-class subsystem** — most projects bolt this on later.
6. **PlaylistV1 JSON schema** — cleanest contract in the codebase.
7. **Knowledge layer's typed return models** — template for the rest of the codebase.
8. **The 7-style transition library** — real DJ techniques, validated by listening.
9. **Patch A/B/C safety nets** — battle-earned. Move to `safety/` module with unit tests.
10. **`schedule_transition` LLM-only entry point** — Python handles timing, LLM picks moment + technique. Right boundary.
11. **Bearer auth + DNS-rebinding protection on MCP** — security baseline.
12. **`StateSource` abstraction** — right shape for local-vs-remote TUI.
13. **Single-command install + XDG layout** — UX is right, complexity hidden.
14. **18 unit + 15 eval tests** — already-paid investment.

## Cost / value summary

| Wave | Effort (focused) | Realistic | ROI | Risk if skipped |
|------|----:|----:|------|--------|
| 0 — Bug fixes + dead code | 1 day | 2–3 days | Trivial cost, removes latent bugs + clutter | Low (bugs don't fire often) |
| 1 — Consolidation | 3 weeks | 6–8 weeks | High readability, kills 50% of finding count | Medium (drift accelerates) |
| 2 — Process split | 1–2 weeks | 3–4 weeks | Reliability is now architectural, not disciplinary | High (replay/silence incidents recur) |
| 3 — Composition | 2 weeks | 4+ weeks | Onboarding + testability | Low until 2nd contributor |
| 4 — Schema + bus | 1 week | 2 weeks | Contracts replace conventions | Low until Wave 3 |

**Recommendation:** start Wave 0 immediately (one PR, ~1 day). Run Wave 1 as background work alongside features for a quarter. Reassess Wave 2 trigger monthly. Defer Waves 3 + 4 until the triggers fire.

## What this doc becomes after v10 ships

- Findings flagged `pre-v10` either land via Waves 0–1 or get deleted as "won't fix."
- Findings flagged `v10` either land via Wave 2/3/4 or get deleted as "punted past v10."
- The KEEP list becomes part of `docs/ARCHITECTURE.md`.
- This file moves to `docs/history/V10_PLAN.md` with status `superseded by v10` once Wave 2 ships.












---

## Review plan (2026-05-04)

Full codebase walk, 10 slices. Each slice = one Deep-dive section appended below. Findings use F-N format. Once all slices land, the **v10 Evolution Plan** section (bottom of file) is written from synthesis.

Slices:
1. ✅ **Boot + hot path** — `main.py`, `heartbeat.py`, `adk_runner.py` (done 2026-05-03, F6–F17)
2. **Transitions engine** — `tools/transitions.py` (1.3k), `transitions.py` mixin
3. **State + persistence** — `session.py`, `session_state.py`, `db.py`, `runtime_paths.py`, `config.py`
4. **Tools surface** — `tools/__init__.py` + 11 tool modules
5. **Planner + Library + Producer loops** — three sibling agent loops
6. **Remaining mixins** — `commands.py`, `evolution.py`, `sets.py`, `being_heartbeat.py`, `ws_server.py`
7. **Knowledge layer** — `agent/knowledge/*`
8. **Transport + UI** — `relay.py`, `ws_server.py`, `tui.py`, `cli.py`, `tui_state_source.py`
9. **MCP server** — `mcp_server/*`
10. **Install + deploy + scripts + tests + docs** — `bin/`, `install.sh`, `scripts/`, `tests/`, `docs/`

Execution: Wave A = slices 2,3,4,5 (parallel). Wave B = 6,7,8. Wave C = 9,10. Then v10 plan.

---

## Deep-dive #1 — Boot + hot path (2026-05-03)

Files read: `agent/main.py` (632 lines), `agent/heartbeat.py` (813), `agent/adk_runner.py` (308), top of `agent/planner_loop.py` (920).

### F6 — `_CorruptionDetector` is defined twice, attached twice
**What:** `agent/adk_runner.py` lines 16–35 and 38–57 are byte-identical class definitions, each attaching its own handler to the root logger.
**Evidence:** Direct read. Two `_corruption_detector = _CorruptionDetector()` + `addHandler` blocks. Both fire on every WARNING.
**Fix:** Delete one of them. Cheap (5 min).
**Blast radius:** Trivial. Single-file edit.
**Status:** Open. **Doesn't need v10 — fix in next PR.**

### F7 — `start()` is a 170-line god-method
**What:** `DJTretaBeing.start()` does signal install, log lines, ~6 file resets, DB init, library scan, LiteLLM autostart, Mixxx autostart, deck rate reset, session restore, agent creation, 5x App/Runner/Session setup, mood callback, mood-profile race guard, idle-load callback, startup mood file, WS server start, being-heartbeat start, broadcast start, set start, relay start, main loop. All sequential, all in one method.
**Evidence:** `agent/main.py:391–582`.
**Fix:** Split into `_init_state()`, `_ensure_external_processes()` (LiteLLM + Mixxx), `_build_agents()`, `_install_callbacks()`, `_spawn_loops()`, `_run_forever()`. Composes with F1.
**Blast radius:** Low (refactor inside one method). High readability gain.
**Status:** Open. Pre-v10 candidate.

### F8 — Mixin `__init__` choreography is invisible
**What:** None of the 13 mixins have an `__init__`. `DJTretaBeing.__init__` populates every shared attribute (`_agent_lock`, `_loop`, `_loop_thread`, `_session_service`, `_dj_runner`, `_planner_runner`, `_library_runner`, `_producer_runner`, all the `_*_session` attrs, `_emergency_running`, `_transition_pending`, `_idle_needs_load_set_at`, `_deck_start_time`, `_deck_track`, etc.). Mixins reference these and assume they exist. Adding a new mixin requires editing the parent `__init__` — invisible coupling.
**Evidence:** `agent/main.py:249–306`. Heartbeat references `self._agent_busy` / `self._transition_pending` / `self._emergency_running` / `self._idle_needs_load_set_at`, none owned by the mixin.
**Fix:** When F1 lands, each subsystem owns its own state on its own object. Until then, a per-mixin `init_<name>(self)` method called from `__init__` documents the dependency.
**Blast radius:** Medium. Refactor of every mixin.
**Status:** Open. Reinforces F1.

### F9 — Filesystem-as-IPC is alive and well, despite the "single Session truth" claim
**What:** Comment in `main.py:482` says "Replaces the old file-IPC polling." Reality, in startup alone, the Being touches: `state.json`, `command.json`, `playlist.json`, `scheduled-transition.json`, `transition-pending.lock`, `directives.json`, `mood-change.json`, `deck-ownership.json`, `mood.txt`, `thinking.log`, `billing.json`, `dj-treta.pid`, `session.json` — 13 files. Heartbeat reads/writes most of them on every tick.
**Evidence:** `runtime_path(...)` calls grep across `main.py`, `heartbeat.py`, `adk_runner.py`, `planner_loop.py`. Six are `unlink(missing_ok=True)`-ed at startup as cleanup.
**Fix:** Inventory which of these is genuinely cross-process (MCP server writes `deck-ownership.json`, scheduled-transition is daemon→executor) vs accidental (mood.txt, mood-change.json which were IPC in earlier versions and never got cleaned up). Promote cross-process ones to a typed bus (F3). Demote intra-process ones to in-memory.
**Blast radius:** Medium. Touches every consumer.
**Status:** Open. Pairs with F3.

### F10 — `_heartbeat()` is a 320-line method with five priority gates and ~15 inline `getattr(self.session, ...)` reads
**What:** `heartbeat.py:139–470`. Single function does P1 silence → P2 auto-transition → P3 scheduled exec → P3.5 signal executors → P4 DJ creative invoke → dynamic sleep, with deck ownership sync, status fetch, metadata lookup, prompt building, thread spawn, all inline.
**Evidence:** Direct read. Contains 4 distinct closures (`_auto`, `_run`, `_resolve` in main.py, `_meta_get` in heartbeat).
**Fix:** Each priority becomes a `_p1_silence(...)`, `_p2_auto_transition(...)`, etc., returning a verdict struct (`Continue` / `Handled` / `NextSleep(n)`). Heartbeat becomes ~30 lines of orchestration. Tests can hit each gate in isolation.
**Blast radius:** Medium. Internal to heartbeat mixin. Self-contained.
**Status:** Open. **Highest readability win for the hot path.** Pre-v10 candidate.

### F11 — Three different "did we play this track?" implementations
**What:**
- `_idle_was_played(idle_path, tracks_played)` in heartbeat.py — exact + suffix + basename match.
- Inline path-set + basename loop at heartbeat.py:351–368, building `_played_paths_check` from scratch (computes the same thing).
- `_is_played` closure inside `_emergency_play` (heartbeat.py:721–737) — path-set + title-fuzzy `_sig` with boilerplate-strip + 0.8 overlap threshold.

Three implementations of the same predicate, drifting independently. Comments cite BUG-2, BUG-11, BUG-14, BUG-15 as historical fixes that landed in different versions.
**Evidence:** Direct read.
**Fix:** Single `Session.has_played(path: str, *, title: str = "") -> bool` method. Every caller routes through it.
**Blast radius:** Low. ~3 call sites.
**Status:** Open. Cheap fix, prevents next replay bug.

### F12 — Path-form mismatch (absolute vs relative) is a recurring footgun, papered over by helpers
**What:** Mixxx reports `file_path` as absolute. `get_deck_paths(...)` normalizes to relative-from-music_dir. `tracks_played[].path` is whichever form was current when written. Every comparison must remember to use basename + suffix matching. Comments call this out as the source of BUG-A and BUG-2.
**Evidence:** `heartbeat.py:24–56`, comments at lines 575–578.
**Fix:** Canonicalize to one form at write time (relative — shorter, stable across machines). Add a `Track.path` value-object that always normalizes. Drop the basename-fallback once corpus is migrated.
**Blast radius:** Medium. Touches everything that writes `tracks_played` + DB schema for tracks. **Has migration cost.**
**Status:** Open. v10 work — needs schema versioning.

### F13 — Five ADK Runners, five sessions, only one with corruption recovery
**What:** Being / DJ / Planner / Library / Producer each get their own `Runner` + `Session` from a shared `InMemorySessionService`. `_recreate_dj_session()` exists for the DJ runner; planner/library/producer/being have no equivalent, so an ADK "Missing tool results" corruption on those silently degrades the loop until the process restarts. Also the global `_CorruptionDetector` flag (F6) is set but never checked anywhere visible — recovery isn't actually triggered by detection.
**Evidence:** `adk_runner.py:60–120`, no other `_recreate_*_session` exists in the file.
**Fix:** Generic `_recreate_session(role: str)` factory; each invoke wrapper checks the corruption flag post-call and recreates the relevant session.
**Blast radius:** Low. Internal to ADK mixin.
**Status:** Open.

### F14 — Subprocess auto-start (LiteLLM, Mixxx) leaks on shutdown
**What:** `_ensure_litellm` / `_ensure_mixxx` spawn child processes via `subprocess.Popen`. `stop()` doesn't terminate them. PID file is the Being's, not children's. Re-running `djtreta` after a crash leaves zombies.
**Evidence:** `main.py:138–143`, `178–181`, `585–603`.
**Fix:** Track child PIDs; on `stop()`, send SIGTERM, wait, SIGKILL fallback. Or — better — use `systemd` units (already templated in `bin/systemd/`) and let the OS supervise. Auto-start mode then becomes opt-in for dev only.
**Blast radius:** Low (code), Medium (operator UX shift).
**Status:** Open. Cheap reliability win.

### F15 — Magic 15-second sleep after Mixxx HTTP comes up
**What:** `main.py:187`. Comment says "audio engine needs time after HTTP API is ready." So we just sleep 15s on every cold start.
**Fix:** Add `/api/audio_ready` to the Mixxx fork (or poll an existing signal) and replace fixed sleep with bounded poll.
**Blast radius:** Low. Requires Mixxx-fork change.
**Status:** Open. Save 15s/boot when fixed. Track on Mixxx-fork roadmap.

### F16 — ~10 daemon threads sharing one `self.session` via property delegates
**What:** Counted: main loop thread, state writer, planner loop, library loop, producer loop, relay loop, WS server thread, being-heartbeat (LoopAgent), emergency_play thread (transient), auto-transition thread (transient), DJ-invoke thread (transient), mood-resolver thread (transient). All read/write the same `Session` object, with critical fields supposedly sync-flushing on mutation. Locking story is implicit — `_agent_lock` serializes ADK calls but `Session` itself relies on its own dirty-flag flush (assumed thread-safe by inspection, not proven).
**Evidence:** `main.py:466–567`, `heartbeat.py:169` (emergency thread), `:214` (auto thread), `:449` (DJ thread).
**Fix:** Document Session's concurrency contract explicitly (does `ObservedList.append` lock? Does `mood_profile = dict(...)` race with reads?). If not currently safe, add lock around mutations. v10 process split (F2) reduces total thread count; until then, a written contract is the cheapest defence.
**Blast radius:** Medium. Audit task, then small lock additions.
**Status:** Open. **Quietly highest-risk** of any finding — a Session race could explain replay bugs that resist hot-path fixes.

### F17 — Main loop's bare `except Exception` swallows everything
**What:** `main.py:578–579`. Any heartbeat exception logs at WARN and the loop continues. Good for liveness ("music never stops"), bad for diagnosis — the comment about Flash dropping signals (~46%) was tracked manually instead of via metric.
**Fix:** Add a counter (`session.heartbeat_errors`) and a rate-limited stack-trace log (every Nth error or once per minute). Liveness preserved, diagnosability restored.
**Blast radius:** Trivial.
**Status:** Open. Cheap.

### Boot + hot-path summary

The hot path is **functionally correct and battle-hardened** — every comment cites a real BUG-N from production. But it's a single 1.5k-LOC blob (main.py + heartbeat.py) with three layers of compensating logic: known-form path matching, multi-implementation predicates, and watchdog gates that each handle a slightly different failure mode.

The story this tells:
- v6→v9.3 has been **patch on patch on patch.** Each patch is correct in isolation; together they're an arch nobody can hold in their head.
- The "Being decides, Python executes" thesis is **right** but **incomplete** — Python ended up making lots of decisions (P3.5 signal executors, P2 auto-transition watchdog, emergency play track selection with title-fuzz dedup). That drift is fine, but should be acknowledged: Python is the **safety net**, the LLM is the **artist**, and the line is currently fuzzy.
- The biggest pre-v10 wins — F6, F7, F10, F11 — are all **mechanical refactors with no architectural commitment**. They make the code review-able without committing to v10. Worth doing before the next big feature.


