# DJ Treta v10 — Evolution PRD

**Branch:** `v10`
**Created:** 2026-05-25
**Source of requirements:** `pitch/competitive/deadmau5-gap-analysis.md` + full Autopilot transcript (`pitch/competitive/deadmau5-autopilot-transcript.md`)
**Relationship to `docs/V10_PLAN.md`:** That doc is the *architectural-debt* notebook (mixin god-object F1, process isolation F2, typed message bus F3, state fragmentation F4). This PRD is the *feature-evolution* track. The features here are deliberately **additive on the current architecture** so they can ship in parallel without waiting on the rebuild. Where an architectural fix would materially de-risk a feature, it's flagged.

---

## 1. Vision & thesis

deadmau5's "Autopilot" is, at its core, the **State System in "Arrange" mode**: he hand-authors grids, cues, section blocks, and "States" (mixer snapshots), drags them onto a timeline, then **renders the sequence to audio and stacks rendered segments into hour-long sets**. It is primarily an **offline authoring/rendering tool** for sample-perfect, reproducible mixes; live controller performance is a *secondary* mode. (Note: the literal "Autopilot" toggle in his settings is just a random track-shuffle *test* feature — the product's real engine is the State System, not that toggle.)

**DJ Treta's thesis: she does this in REAL TIME and AUTONOMOUSLY — generating the grids/cues/blocks he hand-authors AND constructing the State sequence live from musical goals, while reading the room. He composes mixes offline; she performs them, live, forever.**

> One-liner: _deadmau5 built an offline studio to author perfect sets. Treta authors them live, herself, on stage._

v10 closes the **craft gaps** (timing rigor, FX incl. VST host, library coverage, audio-over-IP) and builds the **leapfrog**: **dynamic real-time arrangement authoring** (construct the State sequence on the fly toward a goal) + autonomous authoring + generative visuals — all while she keeps flying herself.

---

## 2. Success criteria

- Transitions fire **bar-quantized** to a master clock; residual drift ≤1% on the playing deck, measured over a 2-hr set.
- Treta can apply **≥3 FX moves** in transitions (filter sweep, delay throw, side-chain duck) chosen autonomously.
- A Rekordbox/Serato library imports in **<60s** with cues→sections + grids, populating `mix_out`/`timeline` with **zero librosa** passes.
- Every track Treta plays has structural analysis (no `+30s` fallback) — analysis coverage **100%** of the playable crate.
- Treta records + archives each set as replayable **state sequences** + rendered audio.
- Treta drives **live reactive visuals** from the audio she's playing (Omni), no human/TouchDesigner in the loop.
- Music never stops during any of it; all deploys hot-swap (never restart Mixxx).

---

## 3. Epics

Each epic lists: **Goal · Scope · Key deliverables · Primary files · Dependencies · Parallel-safe? · Owner agent.**

### E1 — Master Clock & Bar-Quantized Transitions
- **Goal:** Rock-solid timing. Every transition aligns to the outgoing deck's downbeat; tempo/sync handled against one ground-truth clock.
- **Scope:** Read Mixxx per-deck beat phase (`bpm`, `beat_active`, beat distance) via `/api/status`; delay blend start until next bar boundary; run the fps blend beat-aligned; enable Mixxx `quantize`. Reuse/keep the v9.4 native-snap (`bpm_after="anchor"`).
- **Deliverables:** Bar-quantized transition executor; `quantize=1` invariant on load; a timing self-test (drift over N transitions).
- **Primary files:** `agent/tools/transitions.py`, `agent/heartbeat.py` (executor), Mixxx fork `/api/status` (beat phase if missing).
- **Dependencies:** none.
- **Parallel-safe?** ⚠️ Shares `transitions.py` with E2 → **same owner as E2.**
- **Owner:** Agent A (Mixing Engine).

### E2 — Native FX Transition Engine
- **Goal:** Richer, "produced" transitions using Mixxx's native effects (no VST needed — Mixxx ships Echo/Reverb/Filter/etc.).
- **Scope:** Extend Mixxx fork HTTP API with `/api/effect` (load effect onto a deck's EffectUnit, set wet/dry + params, enable/disable). Add transition FX routines: filter sweep, delay throw (post-fader tail), side-chain-style EQ duck, reverb tail. Expose as agent-selectable techniques.
- **Deliverables:** `/api/effect` endpoints (mixxx-treta); `do_filter_sweep`/`do_delay_throw`/`do_sidechain_duck` in transitions; technique docs for the agent prompt.
- **Primary files:** `agent/tools/transitions.py`, mixxx-treta `src/api/apiserver.cpp`, agent prompt in `agents.py`.
- **Dependencies:** E1 (timing) preferable but not blocking.
- **Parallel-safe?** ⚠️ Shares `transitions.py` + mixxx fork with E1 → **same owner.**
- **Owner:** Agent A (Mixing Engine).

### E3 — Library Ingestion & Analysis Coverage
- **Goal:** 100% analysis coverage with zero slow librosa passes where a DJ library already has the data.
- **Scope:** (a) Rekordbox `collection.xml` importer → track path, BPM, key, beatgrid, **cue points → section markers** (`mix_in`/`mix_out`/`timeline`) → `upsert_track`. (b) Serato crate importer. (c) Integrate the existing `ops/backfill_analysis.py` as the librosa fallback for un-imported tracks. (d) Map sections to the **START/BREAK/LOOP/DROP** vocabulary.
- **Deliverables:** `ops/import_rekordbox.py`, `ops/import_serato.py`, section-block mapping in `audio_analysis.py`, a one-command "ingest library" entrypoint.
- **Primary files:** `ops/`, `agent/db.py`, `agent/audio_analysis.py`.
- **Dependencies:** none (pure Python).
- **Parallel-safe?** ✅ Yes — disjoint from mixing/visuals.
- **Owner:** Agent B (Library & Analysis).

### E4 — State Sequencing & Set Archive
- **Goal:** Treta autonomously records mixer "States" and can replay/render a set (his Arrange/Autopilot, but she authors it).
- **Scope:** Define a `State` snapshot (`{deck vols, EQ, filter, xfader, tempo}`); log the state sequence Treta applies during a set; persist a **set archive** (state sequence + the recorded audio the stream already captures); a replay path that re-applies states to the clock.
- **Deliverables:** `agent/state_sequence.py` (snapshot/record/replay), set-archive writer, `session_state` fields, optional `get_set_archive`/`replay_set` tools.
- **Primary files:** new `agent/state_sequence.py`, `agent/session_state.py` (additive fields), `agent/sets.py`.
- **Dependencies:** none for record; replay benefits from E1 clock.
- **Parallel-safe?** ✅ Yes (touches `session_state` additively — coordinate field names).
- **Owner:** Agent C (State & Set).

### E5 — Autonomous Authoring (the leapfrog)
- **Goal:** Treta auto-generates the grids/cues/section-blocks deadmau5 sets by hand — and validates them.
- **Scope:** Auto-grid sanity check on load (detect/repair bad beatgrids), auto section-block authoring from analysis, confidence scoring. Largely *consumes* E3's analysis; this epic makes the planner/transitions *reason in blocks*.
- **Deliverables:** grid-validation pass, block-aware transition selection (mix out at BREAK/outro, in at START), confidence flags surfaced to the agent.
- **Primary files:** `agent/audio_analysis.py`, `agent/planner_loop.py`, `agent/tools/transitions.py` (block-aware mix points).
- **Dependencies:** **E3** (needs the analysis + block vocab).
- **Parallel-safe?** ⚠️ Touches transitions.py (E1/E2) + planner → **sequence after E3; coordinate with Agent A.** Recommend folding into Agent B after E3, integrate with A.
- **Owner:** Agent B (after E3) + integration with A.

### E6 — Generative Visual Layer (Gemini Omni)
- **Goal:** Treta drives her own live reactive visuals from the audio — the thing Autopilot delegates to a human's TouchDesigner via OSC.
- **Scope:** A visuals module that (a) streams audio/section/energy features to a generator, (b) prototypes **Gemini Omni** reactive video / a live "face", (c) keeps an **OSC-out** option (parity with his TouchDesigner hook) as fallback.
- **Deliverables:** `agent/visuals/` module, Omni prototype (audio→visual), OSC emitter, a toggle in config.
- **Primary files:** new `agent/visuals/`, `config.yaml` (visuals block), relay/stream glue.
- **Dependencies:** none (new surface). Omni access via gateway.
- **Parallel-safe?** ✅ Yes — fully isolated new module.
- **Owner:** Agent D (Visuals).

### E7 — (Foundational, optional) Architecture hardening
- **Goal:** Pull in the highest-ROI items from `docs/V10_PLAN.md` **only if** they block the above. Candidates: F2 (process isolation — keep library/visuals off the hot path) and F3 (typed message bus). 
- **Scope:** Decide per-trigger; not required for v10 features to ship.
- **Parallel-safe?** ❌ High blast radius — **not parallelized.** Sequenced deliberately if/when triggered.
- **Owner:** lead/integration, not a parallel agent.

---

## 4. Parallelization plan

Four parallel tracks, each in its own **git worktree** off `v10`, on a sub-branch, owned by a dedicated senior-engineer subagent. The tracks touch **mostly disjoint files**:

| Agent | Epics | Branch | Owns (files) |
|---|---|---|---|
| **A — Mixing Engine** | E1 + E2 | `v10/mixing-engine` | `agent/tools/transitions.py`, mixxx-treta `apiserver.cpp`, transition prompt |
| **B — Library & Analysis** | E3 (+E5 after) | `v10/library-analysis` | `ops/import_*.py`, `agent/db.py`, `agent/audio_analysis.py` |
| **C — State & Set** | E4 | `v10/state-set` | `agent/state_sequence.py`, `agent/sets.py`, `session_state` (additive) |
| **D — Visuals** | E6 | `v10/visuals` | `agent/visuals/`, `config.yaml` (visuals block) |

**Shared-hotspot rules (to avoid merge hell):**
- `agent/tools/transitions.py` → **only Agent A** writes it. E5's block-aware mix points land via A in integration.
- `agent/session_state.py` → additive fields only; each agent appends in a clearly-commented block; integrator merges.
- `agent/agents.py` (tool registration) + `config.yaml` → each agent registers its new tools/config in a scoped section; **integrator** does the final merge (don't let agents fight over the constructor).
- Mixxx fork (`mixxx-treta`) → only Agent A touches C++; build verified before merge.

**E5 sequencing:** starts after E3 lands (needs analysis + blocks), then integrates with A's transition code.

---

## 5. Integration plan

1. Each agent works in its worktree, commits to its sub-branch, opens a PR into `v10`.
2. **Integration order:** B (library/analysis — foundational data) → A (mixing engine) → C (state/set) → D (visuals). E5 after B+A.
3. Integrator (lead) merges sub-branches into `v10`, resolving the shared touchpoints (`agents.py` tool registration, `config.yaml`, `session_state`).
4. After each merge: build mixxx-treta (if touched), run import-gate + the transition/timing self-test, deploy to a **staging** profile (NOT live) or hot-swap to the VM only after the gate passes.
5. When all epics integrated + green on `v10`: tag a `v10.0.0` release → the auto-updater deploys it (per the new pipeline) with health-gate + rollback.

---

## 6. Risks & guardrails

- **transitions.py contention** → single owner (A). Non-negotiable.
- **Mixxx rebuild** (E2 C++ API) → build-verify in the worktree before any merge; keep the API additive.
- **Live stability** → nothing merges to the running VM until `v10` is green; deploys hot-swap, never restart Mixxx (the rescan-deadlock lesson).
- **Scope creep into the architecture rebuild** → E7 stays optional/triggered; v10 features must not *require* F1-F4.
- **Omni access/cost** (E6) → prototype behind a config flag; don't make the live set depend on it.

---

## 7. Out of scope for v10 (parked)
- Full mixin → composition rebuild (F1) — separate track.
- Multi-listener / co-DJ modes.
- The Beings-economy / drops integration.

---

## 8. Cross-review additions (gemini-2.5-pro on the FULL video, 2026-05-25)

A critical cross-review (full video + this PRD) caught real misses + one correction. Source: `pitch/competitive/deadmau5-crossreview-2.5pro.md`. Items below are folded into the epics; **the three large ones are flagged as explicit SCOPE DECISIONS — not auto-committed to v10.**

### Confirmed additions (clear wins — fold in)
- **E3 +cue colors & loop status:** Rekordbox/Serato import must parse cue **colors** + **loop cues** (is-loop + beat length) + full **playlist/folder structure**, and render a **color-coded waveform** (not monochrome). Sections already computed; add the color/loop metadata.
- **E5 +loop-cue reasoning & phantom cue:** planner reasons about loop cues ("16-bar vocal loop") for creative transitions; always-available "play from grid start" action separate from numbered cues.
- **E5 ⭐ DYNAMIC REAL-TIME ARRANGEMENT AUTHORING (the core leapfrog):** the planner constructs a *sequence of future States* live from a high-level goal (e.g. "build energy 16 bars → breakdown"), rather than picking one next track. This is the single biggest differentiator vs Autopilot's static, hand-dragged timeline. **Promote E5 from "nice-to-have" to a headline epic.**
- **E6 +context-driven palettes:** color palette chosen by genre/key/energy, feeding the Omni visual identity (auto, not manual like his 'Sunset'/'Laser' pick).
- **E2/E5 +auto-VST/FX-macro discovery (opportunity):** after loading an FX, Treta wiggles params, measures sonic impact (spectral flux / RMS delta), and auto-creates labelled macros ("Filter Sweep", "Delay Feedback") — automating what he does by hand.

### SCOPE DECISIONS (large — decide in/out before dispatch)
- **[E2.5 — VST/AU Plugin Host + Macro Engine] — BIG.** Autopilot is a full VST host (LFO Tool etc.) with 8 macros + save/load chains + **pre/post-fader** inserts. Matching this means integrating a VST/AU host (e.g. JUCE) into the Mixxx fork + `/api/vst` endpoints + a macro system. *High effort (C++/host integration).* **Decision: do we need real VSTs, or are Mixxx's native FX (E2) enough for v10?** Recommendation: **defer** — native FX covers DJ-style moves; VST host is a v11 reach.
- **[E6 — Ableton Link Audio / audio-over-IP] — MEDIUM-BIG.** Stream each deck as a separate named network channel (into Ableton/other apps). *Decision: is Treta a studio component, or a standalone performer?* Recommendation: **defer** unless we want the "plug Treta into a producer's rig" use case; her output is already a live stream.
- **Offline set rendering & stacking (E4 expansion).** His render-to-audio-then-stack workflow. For Treta (real-time), this is **lower priority** — her "render" is the live stream recording. Keep E4 as record/archive; skip offline composition for v10.

### Mischaracterization fixed
- "Autopilot" (the named mode) = random shuffle test, NOT the sequencer. The engine is the **State System in Arrange mode**, used primarily for **offline authoring/rendering**. Thesis updated in §1 accordingly (Treta = real-time/live; Autopilot = offline-composed). This *strengthens* our differentiation.
