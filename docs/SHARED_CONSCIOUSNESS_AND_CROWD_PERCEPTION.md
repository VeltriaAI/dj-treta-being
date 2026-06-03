# DJ Treta — Shared Consciousness + Crowd Perception (design grounding)

**Status:** brainstorm grounding, NOT a spec. Produced 2026-05-31 by a 6-agent research fan-out
(current-arch map, existing-perception inventory, crowd-sensing tech, GWT/blackboard prior-art,
committed-threads pull, compute/audio budget) + synthesis. Branch context: `v11-rachna` off `v10`.
Full raw findings: workflow run `wf_4a20dfa4-134`.

> Origin: Manish's framing — *"all agents currently running in silos share one shared consciousness
> but work toward different goals. DJ DJs, planner plans, library manages… Treta the root
> consciousness can do all but as required."* Plus: give Treta **crowd perception** so the
> house-party lesson ("build, don't jump") becomes a sensed, closed feedback loop instead of a post-mortem.

---

## The unifying model — ONE LOOP, FOUR ROLES, ONE NOTEBOOK

The shared **set notebook** (already committed, 2026-05-31) **IS** the global workspace. This single
substrate unifies crowd perception + shared consciousness — they are not two features.

Mapping the consciousness prior-art onto roles Treta already has:

- **SUBSTRATE = the notebook as an append-only event log** `{ts, agent_id, type, payload, salience, confidence}`
  + a cheap projected **now-view**. (Event-sourcing pattern.) Fixes known pains for free:
  crash/restart recovery without stopping music (replay the log — directly addresses the `session.json`
  race we hit), set auditability (replay transitions), and **lock-free claims** (first "I'm loading next"
  event wins → DJ and planner never fight the deck).
- **COORDINATION = stigmergy.** DJ / planner / library / producer do NOT call each other. Each reads the
  now-view, acts when its own precondition is met, appends its action+reasoning as a trace the next
  specialist sees. Adding a new sense (crowd) or specialist (lighting) touches nobody else.
- **CONTROL / "consciousness" = Global Workspace Theory as event-driven broadcast.** Every appended
  event carries a **salience** score. The root being **subscribes** to the log and **wakes on
  salience-above-threshold** (not a busy loop). It arbitrates the single most important thing, appends a
  decision, broadcast re-aligns DJ + planner. *"Shared consciousness, different goals" = one notebook
  everyone sees; only the salient winner gets the root being's attention.*
- **SAFETY = Society-of-Mind suppressor.** Hard invariants ("music never stops", "never restart Mixxx
  mid-play", build-don't-jump energy-slope cap) are **veto agents** that can reject any appended decision
  — including the root being's — that would silence or whiplash the floor. Context-as-veto, not scattered
  `if/else`.

**Crowd perception is the new sense that closes the loop.** It is a **write-only knowledge source**:
appends salience-tagged percepts (`crowd_energy`, `energyDirection`, `crowd_reaction`/cheer-spike), never
reads-to-act. It makes **build-don't-jump a closed feedback loop for the first time**: "I escalated and
residual-RMS rose + they cheered" (confirm, push) vs "I escalated and the room went quieter than this
section predicts" (suppressor flags FALLING → root being wakes → pull the lane back / drop a crowd-pleaser).
The house→techno jump that lost the room becomes a **sensed, correctable event.**

---

## Workspace design — append-only log + projected now-view + bounded narrative

- **Log** = ordered, timestamped, append-only `{ts, agent_id, type, payload, salience, confidence}`.
  Types: `percept, decision, transition, claim, generated_track, directive`. Never overwrite — current
  state is a deterministic projection/replay.
- **Now-view** (cheap projection every specialist reads at prompt start):
  `now_playing, up_next[], crowd={energy 0-100, direction RISING/PEAK/PLATEAU/FALLING, last_reaction, confidence}, lane, planned_arc`.
  Note `lane` = the crowd-shape (house vs techno), **not** just BPM/key.
- **Narrative band** = human-readable last ~10 min (the committed shape):
  `[22:08] DJ → Loaded X (anchor house lane). [22:09] Crowd → energy 72 rising, cheer-spike on the drop (conf 0.7). [22:10] Planner → holding lane, ranked next 6 by lane-stickiness.`
  Every agent writes WHAT + WHY. Manish's directives append WITH their interpretation so all agents read one story.
- **Scoped writes**: each agent owns its event types (no mutable-shared-state races); claims serialize by log order.
- `crowd_energy` also flows into the existing `relay.py::_sample_energy_arc` → `current_set.energy_arc`,
  so post-set reflection can ask "which transitions made the room cheer."

Why the log over a plain mutable state dict: crash recovery + audit + lock-free coordination — none of
which a scratchpad gives.

---

## Crowd perception — the reframe that makes it tractable

**DO NOT attempt acoustic echo cancellation.** This is the single most important finding. In this case
(permanent double-talk, single mic, club SPL where music is 30–60 dB louder than crowd, loudspeaker
non-linearities) AEC3/Speex only suppress 20–40 dB + add artifacts — you NEVER recover clean crowd audio.
Building WebRTC-AEC v1 = most effort, least value.

**Reframe: "measure un-explained energy on top of my known music," not "hear the crowd."** Treta never
needs intelligible crowd audio — only an energy/event signal. Exploit that **Mixxx knows its exact output**:

**MVP (works at a real party, ~150–300 lines, librosa already a dep):**
1. Route via **BlackHole + an Aggregate Device** so ONE sample-synced Python stream gets `[mic | Mixxx-master]`.
2. One-time **calibrate**: ~20s empty-room music-only → per-band `room_gain` (EMA); + latency offset from a
   test-tone cross-correlation (~80–200 ms on Mac).
3. Every 0.5–1s: positive per-band residual `max(0, mic_band − room_gain·music_band)` → `crowd_energy` 0–100,
   EMA-smoothed; slope → RISING/PEAK/PLATEAU/FALLING — **guarded against musical breakdowns** by
   cross-referencing Mixxx's current section (quiet during a known breakdown ≠ dying floor).
4. **YAMNet TF-Lite on the RAW mic** (not residual) for Cheering/Applause/Shout SPIKE events
   (spike-over-baseline, ~100 ms/2 s on Mac CPU) → `crowd_reaction` right after a transition so Treta
   grades her own mix.

These drop into `perception.energy/energyDirection` (→ `_sample_energy_arc`) + into `get_listener_pulse()`
as `recent_crowd_energy` / `crowd_reaction`. Existing planner/heartbeat/dj_talk consume them with near-zero
new decision code.

**Phase 0 (free, ships today, zero audio risk):** the relay **already** derives VU-based
energy/density/tension at 3 Hz over HTTP (zero DSP, off the audio thread) and it is **unused in the
decision loop**. Wire it into the heartbeat context → "feels the room from the mix output" with no new code.

**Ambitious (v2):** aubio/librosa onset → Mixxx beat-phase **clap-alignment** (on-beat = engaged, gates
whether it's safe to escalate); sing-along vocal-band residual vs tagged vocal sections (weak bonus);
speexdsp to sharpen the residual *as an energy estimator* (not clean audio); low-res dense optical-flow
(cv2 Farneback) **dance-intensity** as a corroborating movement axis — only where lighting/placement/consent
are controlled, in-memory only, scalar-only, never persist frames, reject strobe frames. Multi-person
feedback aggregation beyond Manish-only.

---

## Salience executive + suppressor + kill-switch

- **Root being is salience-triggered, NOT a busy loop.** Subscribes to the log as a broadcast bus, wakes
  only on salience-above-threshold; otherwise the four specialists run autonomously (stigmergy).
  - Crowd "energy FALLING beyond section prediction, salience 0.9" → ignites root → arbitrates ONE action
    (drop crowd-pleaser / pull lane back / `dj_talk`) → broadcast re-aligns DJ + planner.
  - Cheer-spike, salience 0.6 → may wake, or just bias the planner's next ranking.
  - "next track ready, salience 0.3" → does NOT wake root; planner/DJ handle it.
  - Manish directive at high salience → always wakes, interpreted into the narrative.
- **Confidence-aware:** every reading carries confidence + a predicted-by-music baseline → act on SUSTAINED
  trends (3–5 s cheer, 45–60 s decline), never a single noisy frame; cross-check the music's section before
  "room is dying"; prefer mic over camera at night when they disagree.
- **Suppressor (Society-of-Mind veto, always-on):** "music never stops", "never restart Mixxx mid-play",
  build-don't-jump slope cap (≤1 level step up) can VETO any decision — including root's — that silences or
  whiplashes the floor.
- **Kill-switch (compute safety):** crowd sensing runs as a **separate low-priority child process** writing
  `/tmp` file-IPC snapshots; if CoreAudio reports xruns, the daemon SIGSTOPs/kills the child. Music never
  depends on the sense. Disabled on the VM (Mac-dev-only).

---

## Sequencing options

- **A — Notebook → crowd-MVP → close loop (recommended).** Ship the notebook (append-log + now-view +
  build-don't-jump), Phase-0 VU wire (free), then crowd-MVP as a write-only KS. Rachna after.
  *Pros:* matches committed "notebook before Rachna"; build the workspace once, every later sense/specialist
  plugs in free; build-don't-jump lands even before the mic; lowest rework.
  *Cons:* the headline "new sense" is gated behind ~1–2 days of notebook work.
- **B — Crowd-MVP first as standalone child process, notebook second.** `agent/crowd_sense.py` → `/tmp/dj-treta-crowd.json`
  → existing perception dict + `get_listener_pulse`, demo "she feels the room" at the next party. Notebook after.
  *Pros:* fastest path to the visceral demo; proves the residual approach at a real party; child-process isolation
  can't break music; genuinely low-code via existing plumbing.
  *Cons:* throwaway integration (re-home into notebook later); weaker closed loop without the narrative; two
  sources of truth during transition.
- **C — Phase 0 only, then decide.** Wire the existing relay VU energy into heartbeat today (free, zero risk).
  Run it at a party, measure lift from output-derived energy alone, then decide if mic-based human sensing is
  worth the BlackHole/calibration/clipping cost.
  *Pros:* ships today, free, no audio-chop risk, de-risks the whole feature.
  *Cons:* senses the MIX not the humans (can't tell "dancing hard to a mellow track" or "they cheered");
  won't fully close build-don't-jump.

---

## Biggest risks (with mitigations)

1. **Audio chop.** Enemy is memory-pressure + priority contention, not raw CPU. → mic+DSP in a SEPARATE child
   process at utility QoS/nice 10, separate input device, ~100 ms non-realtime buffer, pre-allocated numpy,
   file-IPC at 2 Hz, SIGSTOP/kill on xrun. VM-disabled.
2. **Echo-cancellation is a trap.** → don't separate, MEASURE the reference-aware residual; treat as a noisy
   relative energy delta.
3. **Agent contradiction / panic on noisy readings** (crowd says PUSH, owner-profile says skip; single quiet
   frame → genre change). → confidence + baseline, sustained-trend gating, section cross-check, suppressor veto.
   **Needs an explicit live-room-vs-owner-taste arbitration rule.**
4. **Calibration staleness + mic clipping** (bodies absorb sound; built-in mic clips at club SPL). → slow EMA
   re-adapt during quiet moments / recalibrate between sets; budget external mic or accept lower-SPL validation.
5. **False FALLING during breakdowns.** → only flag FALLING when quieter than the current MUSIC SECTION predicts.
6. **Two sources of truth** (esp. option B). → pick the notebook as single source of truth early; time-box any
   throwaway dict integration.
7. **Camera privacy/consent (v2).** → in-memory only, scalar-only, never persist frames; defer to a controlled venue.

---

## Open questions for Manish (the forks that change the design)

1. **Notebook substrate ambition:** true append-only event-log (crash recovery + lock-free claims, more work)
   or the simpler bounded-narrative the committed thread described?
2. **Is an energy/direction/cheer signal enough** for build-don't-jump, or do you expect semantic "what is the
   room *feeling*" reads (expensive Gemini path, gated ≥30 s / VM-routed)?
3. **Live-room vs owner-taste arbitration:** when crowd says PUSH but your lifetime profile says "this genre gets
   skipped" — who wins?
4. **Lane authorship:** does crowd perception *infer* the lane (crowd-shape), or only confirm/deny the lane the
   DJ/planner already chose?
5. **Mic reality:** budget an external mic with headroom, or accept dev-Mac/lower-SPL validation first?
6. **Multi-person:** consensus sensed only acoustically (cheer/residual), or add a per-person channel (request QR)?
7. **Rachna interplay:** should crowd perception feed Rachna stem-swap timing (cheer-spike → layer the lead), or
   strictly after notebook + Rachna land?
