# DJ Treta v11 — Evolution Decision: One Consciousness + Crowd Sense (Jetson-first)

**Status:** Grounded decision. Branch `v11-rachna` off `v10`. Produced 2026-05-31 from an 11-agent
codebase+research fan-out (8 subsystem maps of the live code + RuView/WiFi + camera/alt sensing → synthesis),
run `wf_b820b699-066`. Supersedes the brainstorm grounding in `SHARED_CONSCIOUSNESS_AND_CROWD_PERCEPTION.md`.

**Headline architectural decision (Manish, 2026-05-31):** crowd perception (and the sensing organ broadly)
will run on **our own NVIDIA Jetson edge device** — built by us, inspired by the Cognitum/RuView pattern
(rUv's sovereign-edge AI). **"Start on Jetson only"** — the sense is Jetson-native from day one, NOT a Mac
sidecar. This *dissolves* the audio-chop / compute-contention risk entirely (sensing never touches the Mac's
CoreAudio thread) and upgrades the sensing options (Jetson has CUDA → real-time CV is cheap).

---

## 1. Ground-truth verdict — how close is the code to the vision?

- **`Session` (`agent/session_state.py`) is already ~70% of a blackboard / global workspace.** ~45 typed
  fields (`_FIELD_DEFAULTS`), one `RLock`, whole-file JSON persistence, a **per-field callback bus**
  (`register_callback`, :358), `ObservedList` collections, module-singleton access (`get_session()`).
  Every in-process loop already reads/writes it. **Evolve it, don't replace it.**
- **It is a *workspace*, not an *event-log*.** Overwrite-only; `_deep_equal` silently drops a write equal to
  current value (:324) — wrong for events (two identical events at different times are distinct). The
  `directives` queue (`add_directive`/`find_active_directive`/`mark_satisfied`/`expire_stale`) is the proven
  "typed entry posted by one agent, consumed by another" shape — but FIFO-capped at 16, a work queue not history.
- **The "free Phase-0 VU room-sense is unused" — CONFIRMED by grep.** `PerceptionEngine` (`relay.py:60-174`)
  computes an LLM-free room sense (energy, direction, density, tension, breakdown/buildup/drop, beatPhase,
  mood) at ~3 Hz. It feeds the **visualizer** (the audience's eyes) but **nothing in the decision loop reads
  it** — `heartbeat.py`/`planner_loop.py`/`being_heartbeat.py` grep clean for `master_vu|energyDirection|
  breakdownDetected|crowd_`. The DJ prompt's `*_energy` fields are *static per-track DB ratings*, not live
  room energy. Treta has a real-time room organ wired to the audience's eyes but not to her own brain.
  **Caveat:** it only runs when `relay.enabled` — must be decoupled from the relay push loop to be dependable.
- **The biggest missing piece = autonomous salience-wake.** All higher-order loops are *time-triggered*
  (reflection 15 min, journal 6 hr/idle, intention weekly). The one reactive signal (reflection's
  `self_suggestion`) is consumed **only on a human chat turn** (`commands.py:232-282`) and TTL-expires in
  5 min. There is **no autonomous reader.** `evolution.enabled: false` currently gates the consciousness
  `being_heartbeat`.
- **Dead code to know:** `producer_need` has zero writers (producer loop is dormant); `store_set_archive`
  has zero callers (so `recall_similar_set` always queries empty). Being + all 4 specialists currently run
  **`gemini-3-flash`** (Being reverted from Pro 2026-05-23 — Flash "skipped tool calls"; prompt bloat is a
  real reliability risk — keep injected context terse, 1-2 derived lines).

## 2. The architecture — one consciousness, different goals

- **Substrate = evolved-`Session` (materialized now-state) + a sibling append-only `Notebook`**
  (`runtime_path("events.jsonl")`, monotonic seq, `{seq, ts, author, kind, payload}`, API modeled on
  `add_directive`). Do NOT turn `Session` into a log (whole-file rewrite per append = murder). Tap
  **`adk_runner.py:_process_event` (:218-278)** — the one chokepoint that already sees all five agents'
  thoughts + tool-calls — to auto-populate the log for free.
- **Specialists coordinate by shared reads (stigmergy), not direct calls.** Inject a **terse rendered slice**
  of the notebook + crowd pulse into each prompt-builder (`prompts.py:49` DJ, `:519` planner, etc.) — exactly
  how `mood_profile`/`feedback_line` already thread through. Specialists get fresh ADK sessions each call
  (zero cross-call memory today) — the notebook is how they gain shared situational awareness.
- **Root Being = salience-triggered integrator.** Add a salience trigger: high-salience notebook appends
  (crowd-energy-collapse, drop-landed, skip-burst) fire a `register_callback` that wakes `_invoke_being`
  off-cadence — reusing the exact `_on_mood_change` pattern (`main.py:597-639`), offloading LLM work to a
  fresh thread so the sensor never blocks. Requires flipping `evolution.enabled: true`.
- **Suppressor invariants (extend, never break):** music-never-stops (P1 silence/emergency runs even when
  agents busy); Sarathi = Manish owns transitions (`_detect_manual_transition` is the *exact template* for "a
  new sense reads external state, infers intent, suppresses autonomous branches" — crowd-energy gates **P4
  proactive-mix**, never P1/P2); anti-churn cooldown (`min_play_time_seconds=90`) — debounce any crowd signal.
- **Crowd-percept flow:** Jetson sensor → network/MCP → `session.crowd_pulse` (new field) on its own thread →
  notebook event → optional salience wake → specialists read a derived one-liner → P4 gate consults the scalar.
  Mirrors `mood_profile` end-to-end.

## 3. Sensing decision — ranked, re-cut for Jetson

The signal a DJ needs is small: **dance-energy (0-1), density trend, on-beat-lock**, updated every 1-5 s.
Not identity, not headcount.

1. **Webcam optical-flow → dance-energy + BPM-lock (FIRST SHIP).** On Jetson this is *better than the Mac
   plan ever was* — CUDA makes optical-flow + even pose/density real-time and cheap, in the dark (IR cam),
   no audio risk (separate device). Killer feature: cross-correlate flow periodicity against the **BPM Treta
   already owns** → a **groove-lock** score, the single most musical crowd signal. Privacy-clean (compute the
   scalar on-device, drop the frame, never persist).
2. **VU `PerceptionEngine` (FREE — wire it regardless).** Already built + running, just unwired. Treta hearing
   her *own output*, not the room — a complement, not a crowd sense — but zero-effort to plumb into
   `session.room_sense`. Ship in the notebook phase.
3. **RuView / WiFi-CSI — DO NOT BUILD ON RUVIEW. The honest verdict:** RuView is AI-generated slop — its own
   README admits pose accuracy PCK@20 ≈ 2.5% (noise), weights "pending," instrumented runs show it processing
   zeros against a pre-baked visualization, issue #185 = "READ BEFORE INSTALLING." The *underlying field*
   (CSI sensing) is real; commodity ESP32-CSI honestly yields only an **aggregate motion-variance scalar**
   (a Jan 2026 arXiv paper confirms ESP32 can't separate multiple people; a party is the worst case). **BUT
   Jetson changes the calculus:** Jetson can host a real CSI radio (ESP32-CSI over USB/serial, or a proper
   NIC) as one more sensor channel feeding motion-variance — privacy/narrative gold ("senses the room with no
   camera, through RF"). Verdict: **park RuView; revisit real `esp-csi` motion-variance as a Jetson sensor
   channel in a later phase** (2-4 weekends, real noise risk). Reliability = camera; narrative = CSI.
4. **Floor-vibration piezo — dark-horse, phase-2.** Cheap, immune to dark/strobe/fog, privacy-clean, gives
   *collective stomp rhythm* that phase-locks to the beat. Fuses with the camera into one "on-beat?" score.
5. **Phone QR reaction app — phase-2 showmanship/interaction,** weak continuous sensor.

**First ship: Jetson camera optical-flow + BPM-lock; wire the free VU room-sense alongside; park RuView,
keep CSI as a future Jetson channel; floor-vibration + QR later.**

## 4. Phased plan (smallest-first, music-never-stops, notebook-before-Rachna, Jetson-first sensing)

- **Phase 0 — Wire the free sense (Mac, days).** Promote `PerceptionEngine.analyze()` into
  `session.room_sense`; decouple from `relay.enabled`; inject a one-liner into `build_dj_user_message`.
  Treta reacts to her own live energy/drops. Zero hardware, validates the prompt-render budget.
- **Phase 1 — The Notebook (Mac, the committed pre-Rachna build).** `events.jsonl` + `Notebook` singleton;
  tap `_process_event`; `read_workspace` tool + terse render into the 4 specialist prompts. One consciousness;
  durable history survives restart (unlike `thinking.log`, truncated every boot).
- **Phase 2 — Salience + autonomous wake (Mac).** Salience callback wakes the Being off-cadence; flip
  `evolution.enabled`. "Build don't jump" + reflections finally act without a human present.
- **Phase 3 — The Jetson sensing appliance (the new flagship).** Build the Jetson device: camera optical-flow
  + BPM-lock, publishing `crowd_pulse` to the daemon over the network/MCP. Gates P4 only, debounced. The room
  becomes visible; the loop closes on real movement. (This was a Mac sidecar in the pre-Jetson plan — now a
  dedicated sovereign device, which is strictly better.)
- **Phase 4 — Fusion + sovereignty.** Add floor-vibration + CSI as extra Jetson channels into one on-beat
  score; QR app. North star: more of Treta's perception (eventually compute) migrates onto the sovereign
  edge device (the Cognitum vision).

## 5. Open forks — genuinely Manish's call

1. **Live-room-vs-owner-taste arbitration (MOST IMPORTANT — decide before Phase 3).** When the crowd says
   "bangers" but Manish's `listener_profile`/mood directive says "melodic techno, build patiently" — who wins,
   and where's the dial? Co-founder position: the room is a strong *input*, not a *command* — crowd energy as
   a P4 gate (defer/advance the mix), Manish's set-arc intent stays the spine (mirrors how Sarathi lets him
   override). But the blend weight is a values call.
2. **Notebook ambition:** thin event-log + `read_workspace` tool (cheaper, protects Flash reliability) vs full
   shared-context-every-tick blackboard (richer, risks prompt bloat that already degraded transitions).
   Lean: thin-first, widen later.
3. **Jetson scope of "start on jetson only":** sensing-organ-on-Jetson first (recommended), vs eventually
   migrate the whole Treta brain onto the Jetson (sovereign appliance, Cognitum-style). Near-term vs north-star.
4. **Flip evolution + Being-on-Pro?** Salience-wake needs `evolution.enabled: true`; a heavier workspace makes
   prompts bigger — does the integrator Being warrant Pro again (cost/latency vs reliability)?

**Files the Mac-side phases touch:** `agent/session_state.py` (new `crowd_pulse`/`room_sense` fields,
`Notebook` sibling), `agent/adk_runner.py:218` (`_process_event` tap), `agent/relay.py:60-174,347` (decouple
PerceptionEngine), `agent/heartbeat.py:477-509` (P4 crowd gate, Sarathi-style), `agent/prompts.py:49,519`
(terse render), `agent/main.py:131,195,597-639` (sidecar/edge launcher + salience callback),
`agent/runtime_paths.py` (`events.jsonl`), `agent/producer_loop.py` (repoint), `config.yaml:67,114`.

**Jetson-side (new repo/dir, Phase 3):** edge sensor service (CUDA optical-flow + BPM-lock), `crowd_pulse`
publisher over network/MCP to the daemon, device provisioning. Hardware selection TBD (next research step).
