# STATE OF DJ TRETA — Founder's Checkpoint (2026-05-31)

*Branch `v10` @ `2439ae0` · from a 10-agent empirical audit (history + tests-actually-run + import/boot
health + dead-vs-wired + per-subsystem grading + known-bugs register → synthesis). Run `wf_ca0d784e-741`.*

## 1. One-paragraph verdict

DJ Treta is **healthy and shippable for a live, supervised set today** — but not yet "press play and walk
away," and **not ready to absorb a v11 rebuild on its current foundations**. The daemon imports cleanly
(16/16 boot modules, zero ImportError/syntax errors), the music-never-stop safety net is layered and intact,
persistence is genuinely production-grade (atomic, debounced, crash-safe, round-trip verified), and every
P0–P5/v10 fix is committed and present. The core live path (`heartbeat → transitions → playback_applier →
mixxx`) grades FUNCTIONAL. What holds it back: a cluster of **silent-degradation bugs** (a one-line
`energy` vs `energy_peak` typo that kills 1/3 of v10 track-ranking; a missing `pandas` dep that zeroes the
weekly intention loop; scheduled FX that silently downgrade to plain crossfades) and a **non-trustworthy
test gate** (non-deterministic from shared `/tmp` state, ~5 stale tests, zero coverage on the live transition
executor). Nothing is on fire; several things are quietly wrong. Fix ~6 small things before v11.

## 2. What we've built (461 commits, 29 tags, ~9 weeks since 2026-03-25)

- **v1–v2.5** (Mar 25–28): autonomous AI DJ, day-one "brain can't kill the music" rule, smolagents multi-agent,
  Textual TUI, Gemini audio perception, "Pure Software 3.0" philosophy, production hardening.
- **v3–v4.3** (Mar 28–30): planner + SQLite + smart heartbeat; sets/relay/recording; agentic
  `schedule_transition` ("agent schedules, Python executes"); Lyria-3 generation ("Treta becomes a musician").
- **v5.0** (Mar 30–31): smolagents → **Google ADK** migration (non-blocking sub-agents + LiteLLM).
- **v6.0** (Apr 3): the pivot — **Being as LLM orchestrator** directing agents via the directive system.
- **v6→v9** (Apr 4–26, ~201 commits, biggest window): root-Being capabilities (LanceDB memory + MiniLM
  embeddings + self-scheduling); v8 `Session`-as-single-source refactor + 17 surgical bug fixes; v9 knowledge
  planner (polars + LanceDB + Vertex embeddings over 18M-track HF dataset); WebSocket-only TUI.
- **v9.0→v9.4** (Apr 26–May 25): operator-mode `install.sh`, pull-based VM auto-updater w/ health-gate +
  rollback, pro transitions, **typed directive system** (PR #94), **Sarathi Mode**.
- **v10** (May 25–31, current): 6 epics from the deadmau5-Autopilot gap analysis (E1 bar-quantize + E2 native
  FX, E3+E5 library ingestion + arrangement, E4 state-sequencing + set archive, E6 visuals gated off) + P0–P5
  decision-quality fixes.
- **VDJ + v11 plan** (May 30–31): standalone WebGL2 visual engine (`vdj/`); **v11 is plan-only — zero feature
  code** (`v11-rachna` has not diverged from v10).

## 3. What works (proven)

Clean boot · music-never-stop ladder (silence two-read → emergency play → end-of-track watchdog → cliff-guard)
· all 10 transition techniques with pre-flight aborts + uniform BPM-anchor · 4-tier path-resolver track
loading + mix_in groove-cue · band+genre+Camelot selection (verified: 8 picks, 0 off-genre leak on the 93-track
DB) · **Grade-A persistence** (17 sync-flush fields, atomic replace, round-trip verified, session-clear race
solved) · higher loops producing real output (reflection fired today 10:39; 9 journal LanceDB rows; semantic
memory wired — treta_thoughts=459, listener_interactions=113) · camelot/db/audio_analysis/mood_resolver all
test-covered · VDJ engine (Grade A−, smoke-tested end-to-end) · relay PerceptionEngine (unit-verified, in prod
frame) · agent-callable `hear_music`/`analyze_track`/`preview_track`.

## 4. What's degraded (works, compromised)

1. **v10 composite rank is 2/3 live** — `planner_loop.py:314` reads `tk.get("energy")` but the DB column is
   `energy_peak`; `energy_gap` is always 0.0. The energy third of the P2 fix is a permanent no-op.
2. **Download genre-gate only hard-enforced in Sarathi** — auto/v8 refill (`_library_fulfil`) relies on
   LLM-advisory text, not the Python `genre_matches` denylist. Same class as the crate-pollution P0 fixed.
3. **Weekly intention loop reads empty journal** — `memory.py:462` falls back to `to_pandas()`; **pandas not
   installed** → sees 0 of 9 journal entries.
4. **Scheduled transitions silently drop params** — `transitions.py:101-113` dispatches positionally:
   `bpm_after`/`glide_duration` dropped (always "anchor"); 3 E2 FX (`delay_throw`/`reverb_tail`/`sidechain_duck`)
   unwired in scheduler → silently degrade to plain crossfade despite being advertised.
5. **Listener skip telemetry unwired** — `record_skip()` exists (`db.py:913`) but is never called; skip counts
   permanently zero; `get_listener_pulse` DEGRADED.
6. **Perception computed but doesn't feed the brain** — `PerceptionEngine` output is write-only telemetry in
   this repo; `_build_context` feeds the brain raw Mixxx status only. *Exactly the gap v11 crowd-perception targets.*
7. **`knowledge.enabled: false` in prod** — v9 dataset planner + HARD_GENRE_GATE off the live path (graceful
   degrade to v8).

## 5. Broken / dormant

- **DEAD:** `recall_similar_set` reads a permanently-empty table (its only writer `store_set_archive` has zero
  callers) yet is registered to the DJ agent + called from the journal loop — live read, dead write.
  `producer_need` has no writer (loop dead two ways). `agent/_archive/` (6 v1.x files, zero imports).
  `library.get_set_history` reads an int as a list.
- **DORMANT (gated, safe):** E6 in-daemon visuals + Gemini Omni (`visuals/omni.enabled:false`; stale comment at
  `agents.py:993-994` falsely claims the engine is registered); consciousness LoopAgent + evolve tools
  (`evolution.enabled:false`). NOTE: reflection/journal/intention loops run **unconditionally** — only the
  consciousness LoopAgent + auto-evolve are gated.
- **UNFINISHED:** E1 bar-quantize not integrated (`state_sequence.py:293,322` TODO, falls back to BPM sleep);
  7+ `TODO live-validate` Mixxx control keys in `tools/transitions.py` ("biggest reliability foot-gun").

## 6. Test + boot health (empirical)

- **Boot: PASS** — Python 3.12.13 venv, 16/16 modules import, `compileall` exits 0. Cosmetic: smolagents
  (dead) wants old huggingface-hub; numpy used-but-undeclared (resolves via librosa).
- **Tests: structurally sound but NON-HERMETIC** — 499 collected, 0 import errors. 397 non-eval run offline
  ~35s, ~95% pass, no hangs. **Non-deterministic across identical re-runs** (7/8/12 failures) — root cause:
  tests share `/tmp/dj-treta-*.json` + `lru_cache`'d `runtime_dir()`; the `being` fixture's `Session.load()`
  resolves to the **real production session.json**. Setting `DJTRETA_RUNTIME_DIR=/tmp` makes them pass →
  environment artifacts, not code bugs. ~5 stale assertions (expect old behavior the v10 fixes changed).
- **Zero coverage on `agent/transitions.py`** (the live executor — the single most behavior-critical,
  hardware-facing module) and the entire relay/ws_server/tui IPC layer + background loops.

## 7. Top risks / open bugs (prioritized)

1. **Test gate not trustworthy** (non-deterministic + reads prod session.json + leaks Session singleton) —
   blocks any v11 regression gate. **P0**
2. **MCP hardcoded paths** — `session_writer.py:34` hardcodes a Linux VM path (empty state on Mac);
   `tools.py:60` hardcodes `/tmp/...deck-ownership.json` vs daemon's `runtime_path()` → deck ownership silently
   breaks if `DJTRETA_RUNTIME_DIR` set. **P0, trivial**
3. `dj_load_track` MCP↔daemon race (no ownership check).
4. `energy`/`energy_peak` typo (§4.1) — one-line.
5. Missing pandas (§4.3) — one-line.
6. Path-form not canonicalized at write — root of the recurring replay-track family (mitigated by basename
   matching, not root-fixed).
7. Session durability gaps (nested-dict mutations don't flush; sync-flush holds lock through fs write).
8. WS-server cross-loop `ws.send()` hazard (INFERENCE — verify live).
9. Bass-restore fire-and-forget thread can clobber next track's EQ (no cancel Event).
10. Double `_CorruptionDetector`; recovery only wired for DJ runner.

## 8. Recommended fix/finish list — BEFORE v11 (smallest-impactful-first)

**Tier 1 (one-line/trivial, high value):**
1. `planner_loop.py:314` `tk.get("energy")` → `tk.get("energy_peak")`.
2. `pip install pandas` (or rewrite `memory.py:462` recent-fetch to arrow/`to_list`).
3. Fix MCP hardcoded paths (`session_writer.py:34`, `tools.py:59,60`) → `runtime_path()` / `config.mixxx.url`.
4. Add `numpy` to deps; drop dead `smolagents` dep.
5. Delete false comment `agents.py:993-994`; delete `agent/_archive/`.

**Tier 2 (small, restores intended behavior):**
6. Make tests hermetic: conftest monkeypatch `runtime_dir`→`tmp_path` + `cache_clear()` + Session singleton
   teardown; update ~5 stale assertions. *Prerequisite for any v11 regression gate.*
7. Wire `bpm_after`/`glide_duration` through the scheduled executor; wire or de-advertise the 3 E2 FX.
8. Wire `store_set_archive` into set-finalization, or remove the orphaned `recall_similar_set`.
9. Wire `record_skip()` into the skip handler.
10. Cancel Event on bass-restore thread; dedup `_CorruptionDetector`.

**Tier 3 (finish v10 before stacking v11):**
11. Enforce genre-gate on the auto/v8 download path (Python, not LLM-advisory).
12. Add coverage for `agent/transitions.py` + the relay/ws IPC layer.
13. Schema-version + canonicalize `tracks_played[].path`; collapse the 4 "did we play this?" predicates.
14. Decide `knowledge.enabled`: rebuild LanceDB + flip true, or formally park v9.
15. Verify the WS cross-loop send path live.

**Defer to v11+ (parked, don't touch now):** V10_PLAN F1–F4 architecture rebuild; VST/AU host; audio-over-IP /
Ableton Link; offline render; the consciousness/crowd-sense Jetson work.

---

**One line:** a working, safety-netted, supervised-live AI DJ with a production-grade persistence + memory
spine — held back from autonomy by a handful of silent-degradation bugs and an untrustworthy test gate; finish
the ~6 Tier-1/2 fixes and harden the tests before building v11, not after.
